"""devices.yaml + ortam değişkenlerinden yapılandırma yükleme.

Parolalar YAML'a yazılmaz; her cihaz `password_env` ile bir ortam değişkenine
işaret eder. Örnek için depo kökündeki devices.example.yaml dosyasına bakın.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DeviceConfig:
    name: str
    host: str
    username: str
    password: str
    port: int | None = None
    https: bool = False
    verify_tls: bool = False
    poll_interval_s: int = 300
    reachability_interval_s: int = 60
    overwrite_recording: bool = True   # döngüsel kayıt: doluluk alarmı kapalı

    # En eski kayıt tarihi (saklama derinliği) — günde bir sorgulanır.
    # Cihazı yormamak için polling'den ayrı, seyrek bir döngüde koşar.
    retention_check: bool = True
    retention_interval_s: int = 86400
    max_channels: int = 32          # taranacak kanal sayısı
    first_channel: int = 1          # bazı firmware'ler 0 tabanlı; Faz 0'da doğrulanır
    min_retention_days: int | None = None  # altına düşünce uyarı loglanır

    @property
    def channels(self) -> list[int]:
        return list(range(self.first_channel, self.first_channel + self.max_channels))

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        port = self.port or (443 if self.https else 80)
        return f"{scheme}://{self.host}:{port}"


@dataclass
class AppConfig:
    devices: list[DeviceConfig] = field(default_factory=list)
    database_url: str = ""


class ConfigError(Exception):
    pass


def load_config(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults", {})
    devices: list[DeviceConfig] = []
    for entry in data.get("devices", []):
        merged = {**defaults, **entry}
        password_env = merged.pop("password_env", None)
        if password_env:
            password = os.environ.get(password_env, "")
            if not password:
                raise ConfigError(
                    f"{merged.get('name', merged.get('host'))}: "
                    f"{password_env} ortam değişkeni tanımlı değil"
                )
            merged["password"] = password
        if "password" not in merged:
            raise ConfigError(
                f"{merged.get('name', merged.get('host'))}: password_env eksik"
            )
        devices.append(DeviceConfig(**merged))

    names = [d.name for d in devices]
    if len(names) != len(set(names)):
        raise ConfigError("Cihaz adları benzersiz olmalı")

    return AppConfig(
        devices=devices,
        database_url=os.environ.get("DATABASE_URL", data.get("database_url", "")),
    )
