"""CgiDriver testleri — ağ yerine httpx.MockTransport ile.

Auth akışı (Digest→Basic→AuthFailed) ve getDeviceAllInfo eşlemesi doğrulanır.
"""

import httpx
import pytest

from dahua_monitor.drivers import AuthFailed, CgiDriver
from dahua_monitor.models import DiskState, RaidState

STORAGE_BODY = (
    "list.info[0].Name=/dev/sda\r\n"
    "list.info[0].State=Success\r\n"
    "list.info[0].HealthDataFlag=true\r\n"
    "list.info[0].Detail[0].Type=ReadWrite\r\n"
    "list.info[0].Detail[0].TotalBytes=1000\r\n"
    "list.info[0].Detail[0].UsedBytes=400\r\n"
    "list.info[0].Detail[0].IsError=false\r\n"
    "list.info[1].Name=/dev/sdb\r\n"
    "list.info[1].State=Failure\r\n"
    "list.info[1].Detail[0].TotalBytes=1000\r\n"
    "list.info[1].Detail[0].UsedBytes=0\r\n"
    "list.info[1].Detail[0].IsError=true\r\n"
    "list.info[2].Name=/dev/md0\r\n"
    "list.info[2].State=Degraded\r\n"
    "list.info[2].Type=Raid5\r\n"
    "list.info[2].Detail[0].IsError=false\r\n"
)


def make_driver(handler) -> CgiDriver:
    driver = CgiDriver("http://nvr.test", "monitor", "secret")
    driver._client = httpx.AsyncClient(
        base_url="http://nvr.test", transport=httpx.MockTransport(handler)
    )
    return driver


async def test_disks_and_raid_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=STORAGE_BODY)

    driver = make_driver(handler)
    disks = await driver.get_disks()
    raids = await driver.get_raids()
    await driver.close()

    assert [d.name for d in disks] == ["/dev/sda", "/dev/sdb"]
    assert disks[0].state is DiskState.OK
    assert disks[0].health_ok is True
    assert disks[0].used_bytes == 400
    assert disks[1].state is DiskState.ERROR
    assert disks[1].is_error is True

    assert len(raids) == 1
    assert raids[0].name == "/dev/md0"
    assert raids[0].level == "Raid5"
    assert raids[0].state is RaidState.DEGRADED


async def test_auth_fallback_to_basic():
    """Digest reddedilir, Basic kabul edilir (eski firmware davranışı)."""

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            return httpx.Response(200, text=STORAGE_BODY)
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'},
        )

    driver = make_driver(handler)
    disks = await driver.get_disks()
    await driver.close()
    assert disks, "Basic fallback çalışmalı"


async def test_auth_failed_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'}
        )

    driver = make_driver(handler)
    with pytest.raises(AuthFailed):
        await driver.get_disks()
    await driver.close()
    # Digest (challenge + cevap) ve Basic denemeleri dışında tekrar YOK —
    # lockout koruması
    assert calls["n"] <= 3
