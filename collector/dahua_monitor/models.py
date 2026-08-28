"""Sürücülerden bağımsız ortak veri modelleri."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DiskState(StrEnum):
    OK = "ok"
    ERROR = "error"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class RaidState(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    REBUILDING = "rebuilding"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class DiskInfo:
    name: str                      # ör. /dev/sda
    state: DiskState
    total_bytes: int = 0
    used_bytes: int = 0
    is_error: bool = False
    health_ok: bool | None = None  # SMART sağlık bayrağı; sürüm desteklemiyorsa None
    temperature_c: int | None = None
    type: str = ""                 # SATA / RAID üyesi vb.
    raw: dict = field(default_factory=dict)


@dataclass
class RaidInfo:
    name: str                      # ör. /dev/md0
    level: str                     # Raid5 vb.
    state: RaidState
    rebuild_pct: float | None = None
    members: list[str] = field(default_factory=list)
    hot_spares: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class DeviceIdentity:
    device_type: str = ""
    serial: str = ""
    software_version: str = ""


@dataclass
class PollResult:
    reachable: bool
    latency_ms: float | None = None
    identity: DeviceIdentity | None = None
    disks: list[DiskInfo] = field(default_factory=list)
    raids: list[RaidInfo] = field(default_factory=list)
    error: str | None = None
