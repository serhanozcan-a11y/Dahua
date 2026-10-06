from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from .alerts import AlertManager, build_notifiers, build_notifiers_from_db
from .config import ConfigError, load_config
from .scheduler import run_all
from .store import Store


async def _run(config_path: str) -> None:
    cfg = load_config(config_path)
    if not cfg.database_url:
        raise ConfigError("DATABASE_URL tanımlı değil")
    store = await Store.connect(cfg.database_url)

    # Cihaz kaynakları: devices.yaml + panel (device_config tablosu).
    # Aynı ad iki yerde varsa panel kaydı geçerlidir. Hiç cihaz yoksa çökmek
    # yerine bekle: taze kurulumda kullanıcı cihazları panelden ekleyecek.
    async def merged_devices() -> list:
        devices = {d.name: d for d in cfg.devices}
        secret = os.environ.get("SECRET_KEY", "")
        if secret:
            try:
                for d in await store.load_device_configs(secret):
                    devices[d.name] = d
            except Exception:
                logging.exception("panel cihazları yüklenemedi (device_config)")
        return list(devices.values())

    cfg.devices = await merged_devices()
    while not cfg.devices:
        logging.warning(
            "izlenecek cihaz yok — panelden (:8000) cihaz ekleyin; "
            "60 sn sonra yeniden denenecek"
        )
        await asyncio.sleep(60)
        cfg.devices = await merged_devices()
    # Bildirim kanalları panelden (app_settings) yapılandırılır; devices.yaml da
    # yedek olarak birleştirilir. Panelden yapılan değişiklik ~1 dk'da yansır.
    secret = os.environ.get("SECRET_KEY", "")
    notifiers = await build_notifiers_from_db(store._pool, secret)
    if cfg.alerting.email or cfg.alerting.telegram:
        notifiers += build_notifiers(cfg.alerting)[1:]  # yaml kanalları (LogNotifier hariç)
    alerts = AlertManager(cfg.alerting, notifiers, store)
    logging.info(
        "%d cihaz izleniyor, %d bildirim kanalı aktif",
        len(cfg.devices),
        len(notifiers),
    )

    async def _refresh_notifiers() -> None:
        """Panelden değişen bildirim kanallarını restart'sız devreye alır."""
        while True:
            await asyncio.sleep(60)
            try:
                fresh = await build_notifiers_from_db(store._pool, secret)
                if cfg.alerting.email or cfg.alerting.telegram:
                    fresh += build_notifiers(cfg.alerting)[1:]
                alerts.notifiers = fresh
            except Exception:
                logging.exception("bildirim kanalları yenilenemedi")

    async def _self_monitor_loop() -> None:
        """Öz-izleme: enabled bir cihazdan uzun süre metrik gelmiyorsa (döngü
        durmuş / panelden eklenip devralınmamış) alarm — sessiz arızayı yakalar."""
        while True:
            await asyncio.sleep(300)  # 5 dk
            try:
                rows = await store._pool.fetch(
                    """SELECT d.name, d.updated_at,
                              (SELECT max(m.ts) FROM nvr_metrics m
                               JOIN nvr n ON n.id=m.nvr_id WHERE n.name=d.name) AS last_metric
                       FROM device_config d WHERE d.enabled"""
                )
                now = datetime.now(timezone.utc)
                for r in rows:
                    lm = r["last_metric"]
                    stale = lm is None or (now - lm).total_seconds() > 900  # 15 dk
                    # yeni eklenmiş ve henüz metrik yoksa ~5 dk tolerans
                    if lm is None and (now - r["updated_at"]).total_seconds() < 300:
                        stale = False
                    await alerts.monitor_gap(r["name"], stale, lm is not None)
            except Exception:
                logging.exception("öz-izleme döngüsü hatası")

    refresh = asyncio.create_task(_refresh_notifiers())
    monitor = asyncio.create_task(_self_monitor_loop())
    try:
        # reload_fn: panelden (device_config) eklenen/kaldırılan/değişen cihazları
        # restart olmadan periyodik olarak devreye alır.
        await run_all(cfg.devices, store, alerts, reload_fn=merged_devices)
    finally:
        refresh.cancel()
        monitor.cancel()
        await store.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Dahua NVR disk/RAID izleyici")
    parser.add_argument("--config", default="devices.yaml", help="devices.yaml yolu")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx/httpcore her HTTP isteğini INFO seviyesinde logluyor; bu, toplayıcının
    # kendi anlamlı loglarını (poll özeti, uyarılar) boğuyor. Yalnız uyarı ve
    # üstünü bırak.
    for _noisy in ("httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(_run(args.config))
    except ConfigError as exc:
        logging.error("Yapılandırma hatası: %s", exc)
        sys.exit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
