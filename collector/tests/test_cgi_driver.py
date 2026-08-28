"""CgiDriver testleri — ağ yerine httpx.MockTransport ile.

Auth akışı (Digest→Basic→AuthFailed) ve getDeviceAllInfo eşlemesi doğrulanır.
"""

from datetime import datetime
from urllib.parse import parse_qs, urlparse

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


async def test_oldest_recording():
    """mediaFileFind akışı: create -> findFile -> findNextFile -> close+destroy.

    Kanal 1'de eski kayıt, kanal 2'de daha da eski kayıt, kanal 3'te kayıt yok
    (findFile 400 döner) — en eski olan seçilmeli, finder'lar temizlenmeli.
    """
    destroyed = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        q = parse_qs(url.query)
        action = q.get("action", [""])[0]
        if url.path != "/cgi-bin/mediaFileFind.cgi":
            return httpx.Response(400)
        if action == "factory.create":
            return httpx.Response(200, text="result=77\r\n")
        if action == "findFile":
            handler.channel = int(q["condition.Channel"][0])
            if handler.channel == 3:
                return httpx.Response(400, text="Error\r\n")
            return httpx.Response(200, text="OK\r\n")
        if action == "findNextFile":
            start = {1: "2024-05-10 09:00:00", 2: "2024-03-01 00:30:00"}[
                handler.channel
            ]
            return httpx.Response(
                200,
                text=f"found=1\r\nitems[0].Channel={handler.channel}\r\n"
                f"items[0].StartTime={start}\r\n",
            )
        if action in ("close", "destroy"):
            if action == "destroy":
                destroyed.append(q["object"][0])
            return httpx.Response(200, text="OK\r\n")
        return httpx.Response(400)

    driver = make_driver(handler)
    oldest = await driver.get_oldest_recording([1, 2, 3])
    await driver.close()

    assert oldest == datetime(2024, 3, 1, 0, 30, 0)
    assert len(destroyed) == 3, "her kanal için finder destroy edilmeli"


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
