"""Alarm motoru: durum GEÇİŞLERİNDE bildirim üretir, tekrarları bastırır.

Kurallar:
- NVR down: 3 ardışık başarısız sorgu -> kritik; erişilince düzelme bildirimi
- Disk Error / SMART sağlıksız: anında kritik/yüksek
- Disk Absent: yalnız OK->Absent geçişinde (hiç takılmamış slot alarm üretmez)
- RAID degraded/failed -> kritik, rebuilding -> uyarı; düzelince kapanış
- Doluluk (yalnız overwrite_recording=false cihazlarda): %85 uyarı, %95 kritik
- Saklama derinliği min_retention_days altında -> uyarı
- Kimlik doğrulama reddi -> kritik (cihaz duraklatıldı)

Aktif kalan kritik alarmlar reminder_hours'ta bir hatırlatılır. Tüm alarmlar
event tablosuna da yazılır; bildirim kanalı hatası izlemeyi durdurmaz.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import StrEnum
from typing import Protocol

import httpx

from .config import AlertingConfig, DeviceConfig
from .models import DiskState, PollResult, RaidState
from .store import Store

log = logging.getLogger(__name__)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


# Anlık olay kanalından dinlenen kodlar ve önem eşlemesi
EVENT_CODE_SEVERITY = {
    "StorageFailure": Severity.CRITICAL,
    "StorageNotExist": Severity.HIGH,
    "StorageAbnormal": Severity.HIGH,
    "StorageLowSpace": Severity.WARNING,
}


class Notifier(Protocol):
    async def send(self, subject: str, body: str) -> None: ...


class LogNotifier:
    async def send(self, subject: str, body: str) -> None:
        log.warning("ALARM | %s | %s", subject, body)


class EmailNotifier:
    def __init__(self, cfg) -> None:
        self._cfg = cfg

    def _send_sync(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._cfg.from_addr
        msg["To"] = ", ".join(self._cfg.to)
        msg.set_content(body)
        with smtplib.SMTP(self._cfg.smtp_host, self._cfg.smtp_port, timeout=15) as s:
            if self._cfg.starttls:
                s.starttls()
            if self._cfg.username:
                s.login(self._cfg.username, self._cfg.password)
            s.send_message(msg)

    async def send(self, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send_sync, subject, body)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, subject: str, body: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self._url, json={"chat_id": self._chat_id, "text": f"{subject}\n{body}"}
            )
            resp.raise_for_status()


@dataclass
class _Active:
    severity: Severity
    message: str
    since: float
    last_sent: float


@dataclass
class AlertManager:
    cfg: AlertingConfig
    notifiers: list[Notifier]
    store: Store | None = None
    _active: dict[str, _Active] = field(default_factory=dict)
    _fail_counts: dict[str, int] = field(default_factory=dict)
    _last_disk_state: dict[str, DiskState] = field(default_factory=dict)

    # ------------------------------------------------------------------ giriş

    async def evaluate_poll(self, dev: DeviceConfig, result: PollResult) -> None:
        await self._rule_down(dev, result)
        if not result.reachable:
            return
        for d in result.disks:
            await self._rule_disk(dev, d)
        for r in result.raids:
            await self._rule_raid(dev, r)

    async def evaluate_retention(
        self, dev: DeviceConfig, retention_days: float | None
    ) -> None:
        key = f"{dev.name}/retention"
        if dev.min_retention_days is None:
            return
        if retention_days is not None and retention_days < dev.min_retention_days:
            await self._raise(
                dev, key, Severity.WARNING, "RetentionLow",
                f"saklama derinliği {retention_days:.1f} gün — alt sınır "
                f"{dev.min_retention_days} günün altında",
            )
        else:
            await self._clear(dev, key, "saklama derinliği normale döndü")

    async def device_event(
        self, dev: DeviceConfig, code: str, action: str, index: str = ""
    ) -> None:
        """NVR'ın kendi olay kanalından (eventManager attach) gelen anlık olay."""
        severity = EVENT_CODE_SEVERITY.get(code, Severity.WARNING)
        key = f"{dev.name}/event/{code}/{index}"
        if action == "Start":
            await self._raise(
                dev, key, severity, code, f"cihaz olayı: {code} (index={index})"
            )
        elif action == "Stop":
            await self._clear(dev, key, f"cihaz olayı sona erdi: {code}")

    async def auth_failed(self, dev: DeviceConfig) -> None:
        await self._raise(
            dev, f"{dev.name}/auth", Severity.CRITICAL, "AuthFailed",
            "kimlik doğrulama reddedildi; lockout koruması nedeniyle cihaz "
            "izlemesi DURAKLATILDI. Parolayı düzeltip servisi yeniden başlatın.",
        )

    # ---------------------------------------------------------------- kurallar

    async def _rule_down(self, dev: DeviceConfig, result: PollResult) -> None:
        key = f"{dev.name}/down"
        if result.reachable:
            self._fail_counts[key] = 0
            await self._clear(dev, key, "cihaz yeniden erişilebilir")
            return
        n = self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
        if n >= 3:
            await self._raise(
                dev, key, Severity.CRITICAL, "DeviceDown",
                f"{n} ardışık sorguda erişilemedi (son hata: {result.error})",
            )

    async def _rule_disk(self, dev: DeviceConfig, disk) -> None:
        dkey = f"{dev.name}/disk/{disk.name}"
        prev = self._last_disk_state.get(dkey)
        self._last_disk_state[dkey] = disk.state

        if disk.state is DiskState.ERROR:
            await self._raise(
                dev, dkey, Severity.CRITICAL, "StorageFailure",
                f"{disk.name} disk arızalı (State=Error)",
            )
        elif disk.state is DiskState.ABSENT and prev is DiskState.OK:
            await self._raise(
                dev, dkey, Severity.HIGH, "StorageNotExist",
                f"{disk.name} diski kayboldu (OK -> Absent)",
            )
        elif disk.state is DiskState.OK:
            await self._clear(dev, dkey, f"{disk.name} normale döndü")

        hkey = f"{dkey}/smart"
        if disk.health_ok is False:
            await self._raise(
                dev, hkey, Severity.HIGH, "SmartAbnormal",
                f"{disk.name} SMART sağlık bayrağı olumsuz — disk değişimi planlayın",
            )
        elif disk.health_ok is True:
            await self._clear(dev, hkey, f"{disk.name} SMART normale döndü")

        if not dev.overwrite_recording and disk.total_bytes > 0:
            pct = 100.0 * disk.used_bytes / disk.total_bytes
            ckey = f"{dkey}/capacity"
            if pct >= 95:
                await self._raise(
                    dev, ckey, Severity.CRITICAL, "StorageLowSpace",
                    f"{disk.name} %{pct:.0f} dolu",
                )
            elif pct >= 85:
                await self._raise(
                    dev, ckey, Severity.WARNING, "StorageLowSpace",
                    f"{disk.name} %{pct:.0f} dolu",
                )
            else:
                await self._clear(dev, ckey, f"{disk.name} doluluk normale döndü")

    async def _rule_raid(self, dev: DeviceConfig, raid) -> None:
        key = f"{dev.name}/raid/{raid.name}"
        if raid.state in (RaidState.DEGRADED, RaidState.FAILED):
            await self._raise(
                dev, key, Severity.CRITICAL, "RaidDegraded",
                f"{raid.name} ({raid.level}) durumu: {raid.state.value}",
            )
        elif raid.state is RaidState.REBUILDING:
            await self._raise(
                dev, key, Severity.WARNING, "RaidRebuilding",
                f"{raid.name} ({raid.level}) yeniden yapılandırılıyor",
            )
        elif raid.state is RaidState.ACTIVE:
            await self._clear(dev, key, f"{raid.name} normale döndü")

    # ------------------------------------------------------------ iç mekanizma

    async def _raise(
        self, dev: DeviceConfig, key: str, severity: Severity, code: str, message: str
    ) -> None:
        now = time.time()
        active = self._active.get(key)
        if active is not None:
            remind_after = self.cfg.reminder_hours * 3600
            if (
                active.severity is Severity.CRITICAL
                and now - active.last_sent >= remind_after
            ):
                active.last_sent = now
                hours = (now - active.since) / 3600
                await self._notify(
                    severity, dev.name,
                    f"HÂLÂ AKTİF ({hours:.0f} saattir): {message}", code,
                )
            return
        self._active[key] = _Active(severity, message, now, now)
        await self._notify(severity, dev.name, message, code)

    async def _clear(self, dev: DeviceConfig, key: str, message: str) -> None:
        if self._active.pop(key, None) is None:
            return
        await self._notify(Severity.INFO, dev.name, f"DÜZELDİ: {message}", "Recovered")

    async def _notify(
        self, severity: Severity, device: str, message: str, code: str
    ) -> None:
        subject = f"[DAHUA-IZLEME] {severity.value.upper()} {device}: {code}"
        if self.store is not None:
            try:
                await self.store.write_event(device, "collector", code,
                                             severity.value, message)
            except Exception:
                log.exception("event tablosuna yazılamadı")
        for notifier in self.notifiers:
            try:
                await notifier.send(subject, message)
            except Exception:
                log.exception("bildirim gönderilemedi (%s)", type(notifier).__name__)


def build_notifiers(cfg: AlertingConfig) -> list[Notifier]:
    notifiers: list[Notifier] = [LogNotifier()]
    if cfg.email is not None and cfg.email.enabled:
        notifiers.append(EmailNotifier(cfg.email))
    if cfg.telegram is not None and cfg.telegram.enabled:
        notifiers.append(TelegramNotifier(cfg.telegram.bot_token, cfg.telegram.chat_id))
    return notifiers
