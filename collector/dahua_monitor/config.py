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
class EmailConfig:
    smtp_host: str
    from_addr: str
    to: list[str]
    smtp_port: int = 587
    starttls: bool = True
    username: str = ""
    password: str = ""
    enabled: bool = True


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    enabled: bool = True


@dataclass
class AlertingConfig:
    email: EmailConfig | None = None
    telegram: TelegramConfig | None = None
    reminder_hours: int = 24


@dataclass
class AppConfig:
    devices: list[DeviceConfig] = field(default_factory=list)
    database_url: str = ""
    alerting: AlertingConfig = field(default_factory=AlertingConfig)


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
        alerting=_load_alerting(data.get("alerting", {})),
    )


def _resolve_env(section: dict, env_key: str, target: str, required: bool) -> None:
    """`xxx_env: VAR` alanını ortamdan okuyup `target` alanına çevirir."""
    var = section.pop(env_key, None)
    if var:
        value = os.environ.get(var, "")
        if not value and required:
            raise ConfigError(f"{var} ortam değişkeni tanımlı değil")
        section[target] = value


def _load_alerting(data: dict) -> AlertingConfig:
    email = None
    if data.get("email"):
        section = dict(data["email"])
        section["from_addr"] = section.pop("from", section.pop("from_addr", ""))
        _resolve_env(section, "password_env", "password",
                     required=bool(section.get("username")))
        email = EmailConfig(**section)
        if not email.smtp_host or not email.to:
            raise ConfigError("alerting.email: smtp_host ve to zorunlu")
    telegram = None
    if data.get("telegram"):
        section = dict(data["telegram"])
        _resolve_env(section, "bot_token_env", "bot_token", required=True)
        telegram = TelegramConfig(**section)
    return AlertingConfig(
        email=email,
        telegram=telegram,
        reminder_hours=int(data.get("reminder_hours", 24)),
    )
