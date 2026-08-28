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


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run_all(devices: list[DeviceConfig], store: Store) -> None:
    stop = asyncio.Event()
    tasks = [asyncio.create_task(device_loop(d, store, stop)) for d in devices]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
