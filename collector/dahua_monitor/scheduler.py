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

from .config import DeviceConfig
from .drivers import AuthFailed, CgiDriver, DriverError
from .models import PollResult
from .store import Store

log = logging.getLogger(__name__)


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


async def device_loop(cfg: DeviceConfig, store: Store, stop: asyncio.Event) -> None:
    driver = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
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
                return
            await store.write_poll(nvr_id, result)
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


async def retention_loop(cfg: DeviceConfig, store: Store, stop: asyncio.Event) -> None:
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
            await _sleep(stop, cfg.retention_interval_s)
    finally:
        await driver.close()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_all(devices: list[DeviceConfig], store: Store) -> None:
    stop = asyncio.Event()
    tasks = [asyncio.create_task(device_loop(d, store, stop)) for d in devices]
    tasks += [asyncio.create_task(retention_loop(d, store, stop)) for d in devices]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
