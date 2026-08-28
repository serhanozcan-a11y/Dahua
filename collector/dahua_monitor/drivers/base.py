"""Sürücü arayüzü: tüm erişim yolları (CGI, RPC2, SNMP, NetSDK) bunu uygular.

Zamanlayıcı ve depolama katmanı yalnızca bu arayüzü tanır; firmware/sürüm
farkları sürücülerin içinde kalır.
"""

from __future__ import annotations

from typing import Protocol

from ..models import DeviceIdentity, DiskInfo, RaidInfo


class DriverError(Exception):
    """Cihaza ulaşıldı ama istek başarısız (geçici kabul edilir, retry edilir)."""


class AuthFailed(DriverError):
    """Kimlik doğrulama reddedildi.

    Yeni firmware'ler art arda hatalı denemede IP'yi kilitlediği için
    zamanlayıcı bu hatada cihazı DURAKLATIR ve yönetici alarmı üretir;
    asla otomatik retry yapılmaz.
    """


class Driver(Protocol):
    async def get_identity(self) -> DeviceIdentity: ...
    async def get_disks(self) -> list[DiskInfo]: ...
    async def get_raids(self) -> list[RaidInfo]: ...
    async def close(self) -> None: ...
