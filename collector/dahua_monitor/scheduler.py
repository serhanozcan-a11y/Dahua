"""Cihaz başına eşzamanlı (asyncio) sorgulama döngüsü.

Her cihaz kendi görevinde döner; bir cihazın yavaşlığı diğerlerini etkilemez.
AuthFailed durumunda cihaz DURAKLATILIR (lockout koruması) ve log'a kritik
kayıt düşülür — Faz 2'de buradan alarm motoruna olay gidecek.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from .alerts import EVENT_CODE_SEVERITY, AlertManager, Severity
from .config import DeviceConfig
from .drivers import AuthFailed, CgiDriver, DriverError, Rpc2Client
from .models import PollResult
from .store import Store

log = logging.getLogger(__name__)

EVENT_CODES = list(EVENT_CODE_SEVERITY)


async def poll_once(driver: CgiDriver) -> PollResult:
    start = time.monotonic()
    try:
        identity = await driver.get_identity()
        disks = await driver.get_disks()
        raids = await driver.get_raids()
    except AuthFailed:
        raise
    except DriverError as exc:
        return PollResult(reachable=False, error=str(exc))
    latency = (time.monotonic() - start) * 1000
    return PollResult(
        reachable=True, latency_ms=latency, identity=identity, disks=disks, raids=raids
    )


async def device_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    driver = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    rpc2 = (
        Rpc2Client(cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls)
        if cfg.rpc2
        else None
    )
    nvr_id = await store.upsert_nvr(cfg.name, cfg.host)
    # Cihazların hepsinin aynı anda sorgulanmaması için başlangıç jitter'ı
    await _sleep(stop, random.uniform(0, min(10, cfg.poll_interval_s)))
    try:
        while not stop.is_set():
            try:
                result = await poll_once(driver)
            except AuthFailed as exc:
                log.critical(
                    "%s: kimlik doğrulama reddedildi, cihaz duraklatıldı "
                    "(lockout koruması). Parolayı düzeltip servisi yeniden "
                    "başlatın. Hata: %s",
                    cfg.name,
                    exc,
                )
                await store.write_poll(
                    nvr_id, PollResult(reachable=False, error=f"auth: {exc}")
                )
                if alerts is not None:
                    await alerts.auth_failed(cfg)
                return
            if result.reachable and result.raids and rpc2 is not None:
                await _enrich_raid(cfg, rpc2, result)
            await store.write_poll(nvr_id, result)
            if alerts is not None:
                await alerts.evaluate_poll(cfg, result)
            if not result.reachable:
                log.warning("%s: erişilemedi: %s", cfg.name, result.error)
            else:
                log.info(
                    "%s: %d disk, %d raid, %.0f ms",
                    cfg.name,
                    len(result.disks),
                    len(result.raids),
                    result.latency_ms or 0,
                )
            interval = (
                cfg.poll_interval_s if result.reachable else cfg.reachability_interval_s
            )
            await _sleep(stop, interval)
    finally:
        await driver.close()
        if rpc2 is not None:
            await rpc2.close()


async def _enrich_raid(cfg: DeviceConfig, rpc2: Rpc2Client, result: PollResult) -> None:
    """RAID kayıtlarını RPC2 detayıyla zenginleştirir; hata izlemeyi bozmaz."""
    try:
        details = await rpc2.get_raid_details()
    except (DriverError, AuthFailed) as exc:
        log.debug("%s: RPC2 RAID detayı alınamadı: %s", cfg.name, exc)
        return
    for raid in result.raids:
        detail = details.get(raid.name)
        if not detail:
            continue
        if detail.get("rebuild_pct") is not None:
            raid.rebuild_pct = float(detail["rebuild_pct"])
        if detail.get("members"):
            raid.members = list(detail["members"])
        if detail.get("hot_spares"):
            raid.hot_spares = list(detail["hot_spares"])


async def event_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    """Anlık olay aboneliği: arıza polling'i beklemeden saniyeler içinde işlenir.

    Bağlantı koptuğunda üstel backoff ile yeniden bağlanır (en fazla 5 dk).
    Polling her zaman güvence katmanı olarak ayrıca çalışır.
    """
    if not cfg.event_stream:
        return
    backoff = 5.0
    while not stop.is_set():
        driver = CgiDriver(
            cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
        )
        try:
            async for code, action, index in driver.stream_events(EVENT_CODES):
                backoff = 5.0
                severity = EVENT_CODE_SEVERITY.get(code, Severity.WARNING)
                log.info("%s: anlık olay %s %s index=%s", cfg.name, code, action, index)
                try:
                    await store.write_event(
                        cfg.name, "event-stream", code, severity.value,
                        f"{code} {action} (index={index})",
                    )
                except Exception:
                    log.exception("%s: olay veritabanına yazılamadı", cfg.name)
                if alerts is not None:
                    await alerts.device_event(cfg, code, action, index)
        except AuthFailed:
            log.critical("%s: olay akışı auth reddi, akış durduruldu", cfg.name)
            return
        except DriverError as exc:
            log.warning("%s: olay akışı koptu: %s", cfg.name, exc)
        finally:
            await driver.close()
        await _sleep(stop, backoff)
        backoff = min(backoff * 2, 300.0)


async def retention_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    """Günde bir: cihazdaki en eski kaydın tarihi (fiilî saklama derinliği).

    Kanal kanal mediaFileFind taraması cihaz için polling'den daha maliyetli
    olduğundan ayrı ve seyrek bir döngüdür; hata durumunda 1 saat sonra
    yeniden dener.
    """
    if not cfg.retention_check:
        return
    driver = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    nvr_id = await store.upsert_nvr(cfg.name, cfg.host)
    await _sleep(stop, random.uniform(30, 90))  # açılış polling'iyle çakışmasın
    try:
        while not stop.is_set():
            try:
                oldest = await driver.get_oldest_recording(cfg.channels)
            except AuthFailed:
                log.critical("%s: retention sorgusu auth reddi, durduruldu", cfg.name)
                return
            except DriverError as exc:
                log.warning("%s: retention sorgusu başarısız: %s", cfg.name, exc)
                await _sleep(stop, 3600)
                continue
            retention_days = None
            if oldest is not None:
                retention_days = (time.time() - oldest.timestamp()) / 86400
                log.info(
                    "%s: en eski kayıt %s (%.1f gün)",
                    cfg.name,
                    oldest.isoformat(sep=" "),
                    retention_days,
                )
                if (
                    cfg.min_retention_days is not None
                    and retention_days < cfg.min_retention_days
                ):
                    log.warning(
                        "%s: saklama derinliği %.1f gün, alt sınır %d günün ALTINDA",
                        cfg.name,
                        retention_days,
                        cfg.min_retention_days,
                    )
            else:
                log.warning("%s: hiçbir kanalda kayıt bulunamadı", cfg.name)
            await store.write_retention(nvr_id, oldest, retention_days)
            if alerts is not None:
                await alerts.evaluate_retention(cfg, retention_days)
            await _sleep(stop, cfg.retention_interval_s)
    finally:
        await driver.close()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_all(
    devices: list[DeviceConfig], store: Store, alerts: AlertManager | None = None
) -> None:
    stop = asyncio.Event()
    tasks = [asyncio.create_task(device_loop(d, store, stop, alerts)) for d in devices]
    tasks += [
        asyncio.create_task(retention_loop(d, store, stop, alerts)) for d in devices
    ]
    tasks += [asyncio.create_task(event_loop(d, store, stop, alerts)) for d in devices]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
