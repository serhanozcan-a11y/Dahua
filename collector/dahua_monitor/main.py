from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .alerts import AlertManager, build_notifiers
from .config import ConfigError, load_config
from .scheduler import run_all
from .store import Store


async def _run(config_path: str) -> None:
    cfg = load_config(config_path)
    if not cfg.database_url:
        raise ConfigError("DATABASE_URL tanımlı değil")
    store = await Store.connect(cfg.database_url)

    # Cihaz kaynakları: devices.yaml + panel (device_config tablosu).
    # Aynı ad iki yerde varsa panel kaydı geçerlidir.
    devices = {d.name: d for d in cfg.devices}
    secret = os.environ.get("SECRET_KEY", "")
    if secret:
        try:
            for d in await store.load_device_configs(secret):
                devices[d.name] = d
        except Exception:
            logging.exception("panel cihazları yüklenemedi (device_config)")
    cfg.devices = list(devices.values())
    if not cfg.devices:
        await store.close()
        raise ConfigError(
            "izlenecek cihaz yok: devices.yaml doldurun veya panelden ekleyin"
        )
    notifiers = build_notifiers(cfg.alerting)
    alerts = AlertManager(cfg.alerting, notifiers, store)
    logging.info(
        "%d cihaz izleniyor, %d bildirim kanalı aktif",
        len(cfg.devices),
        len(notifiers),
    )
    try:
        await run_all(cfg.devices, store, alerts)
    finally:
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
    try:
        asyncio.run(_run(args.config))
    except ConfigError as exc:
        logging.error("Yapılandırma hatası: %s", exc)
        sys.exit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
