"""CgiDriver testleri — ağ yerine httpx.MockTransport ile.

Auth akışı (Digest→Basic→AuthFailed) ve getDeviceAllInfo eşlemesi doğrulanır.
"""

from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from dahua_monitor.drivers import AuthFailed, CgiDriver, DriverError
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

# Gerçek DHI-NVR5832-EI çıktısı: tüm depolama tek bir /dev/md0 dizisi olarak
# raporlanır, fiziksel disk (/dev/sda...) listelenmez; dizinin 4 mantıksal
# bölümü (md00..md03) kapasiteyi taşır ve HealthDataFlag=0 gelir.
ARRAY_ONLY_BODY = (
    "list.info[0].Detail[0].IsError=false\r\n"
    "list.info[0].Detail[0].Path=/dev/md00\r\n"
    "list.info[0].Detail[0].TotalBytes=13998054440960.000000\r\n"
    "list.info[0].Detail[0].Type=ReadWrite\r\n"
    "list.info[0].Detail[0].UsedBytes=1445480890368.000000\r\n"
    "list.info[0].Detail[1].IsError=false\r\n"
    "list.info[0].Detail[1].Path=/dev/md01\r\n"
    "list.info[0].Detail[1].TotalBytes=13998054440960.000000\r\n"
    "list.info[0].Detail[1].Type=ReadWrite\r\n"
    "list.info[0].Detail[1].UsedBytes=0.000000\r\n"
    "list.info[0].Detail[2].IsError=false\r\n"
    "list.info[0].Detail[2].Path=/dev/md02\r\n"
    "list.info[0].Detail[2].TotalBytes=13998054440960.000000\r\n"
    "list.info[0].Detail[2].Type=ReadWrite\r\n"
    "list.info[0].Detail[2].UsedBytes=0.000000\r\n"
    "list.info[0].Detail[3].IsError=false\r\n"
    "list.info[0].Detail[3].Path=/dev/md03\r\n"
    "list.info[0].Detail[3].TotalBytes=13437958619136.000000\r\n"
    "list.info[0].Detail[3].Type=ReadWrite\r\n"
    "list.info[0].Detail[3].UsedBytes=0.000000\r\n"
    "list.info[0].HealthDataFlag=0\r\n"
    "list.info[0].Name=/dev/md0\r\n"
    "list.info[0].State=Success\r\n"
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


async def test_raid_only_device_capacity():
    """Fiziksel disk listelemeyen RAID cihazı (gerçek NVR5832): kapasite dizi
    kaydından alınmalı, dizi hem disk (kapasite) hem raid (sağlık) görünümünde
    yer almalı, per-disk SMART olmadığı için health_ok None kalmalı ve
    (State=Success) sahte disk/SMART alarmı üretilmemeli."""
    driver = make_driver(lambda r: httpx.Response(200, text=ARRAY_ONLY_BODY))
    disks = await driver.get_disks()
    raids = await driver.get_raids()
    await driver.close()

    # Fiziksel disk yok -> dizi kapasite birimi olarak dönmeli
    assert [d.name for d in disks] == ["/dev/md0"]
    assert disks[0].total_bytes == 13998054440960 * 3 + 13437958619136
    assert disks[0].used_bytes == 1445480890368
    assert disks[0].state is DiskState.OK
    assert disks[0].is_error is False
    assert disks[0].health_ok is None  # dizide per-disk SMART yok -> alarm yok

    # Aynı dizi RAID sağlık görünümünde de olmalı (State=Success -> ACTIVE)
    assert [r.name for r in raids] == ["/dev/md0"]
    assert raids[0].state is RaidState.ACTIVE


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


ACTIVE_USERS_BODY = (
    "users[0].ClientAddress=Yerel\r\n"
    "users[0].ClientType=GUI\r\n"
    "users[0].Group=user\r\n"
    "users[0].Id=1\r\n"
    "users[0].Name=default\r\n"
    "users[1].ClientAddress=10.20.56.30\r\n"
    "users[1].ClientType=Local\r\n"
    "users[1].Group=admin\r\n"
    "users[1].Id=556\r\n"
    "users[1].Name=admin\r\n"
)


async def test_active_users_parsing():
    driver = make_driver(lambda r: httpx.Response(200, text=ACTIVE_USERS_BODY))
    users = await driver.get_active_users()
    await driver.close()
    assert len(users) == 2
    assert users[0]["Name"] == "default" and users[0]["ClientAddress"] == "Yerel"
    assert users[1]["Group"] == "admin" and users[1]["ClientAddress"] == "10.20.56.30"


async def test_channel_count():
    body = "".join(
        f"table.ChannelTitle[{i}].Name=Kanal {i + 1}\r\n" for i in range(32)
    )
    driver = make_driver(lambda r: httpx.Response(200, text=body))
    n = await driver.get_channel_count()
    await driver.close()
    assert n == 32


async def test_channel_names():
    body = (
        "table.ChannelTitle[0].Name=XRAY KARA TARAFI\r\n"
        "table.ChannelTitle[1].Name=DIŞ ALAN SAĞ\r\n"
        "table.ChannelTitle[2].Name=\r\n"  # boş -> 'Kanal 3'
    )
    driver = make_driver(lambda r: httpx.Response(200, text=body))
    names = await driver.get_channel_names()
    await driver.close()
    assert names == [
        {"idx": 1, "name": "XRAY KARA TARAFI"},
        {"idx": 2, "name": "DIŞ ALAN SAĞ"},
        {"idx": 3, "name": "Kanal 3"},
    ]


async def test_encode_settings():
    """Yalnız MainFormat[0].Video alanları ayrıştırılır; ExtraFormat ve Audio
    yok sayılır. Kanal 0 tabanlı döner."""
    body = (
        "table.Encode[0].ExtraFormat[0].Video.Compression=H.264\r\n"  # yok sayılmalı
        "table.Encode[0].MainFormat[0].Video.Compression=H.265\r\n"
        "table.Encode[0].MainFormat[0].Video.BitRate=4096\r\n"
        "table.Encode[0].MainFormat[0].Video.BitRateControl=CBR\r\n"
        "table.Encode[0].MainFormat[0].Video.FPS=25\r\n"
        "table.Encode[0].MainFormat[0].Video.resolution=2592x1944\r\n"
        "table.Encode[1].MainFormat[0].Video.Compression=H.264H\r\n"
        "table.Encode[1].MainFormat[0].Video.BitRate=2048\r\n"
        "table.Encode[1].MainFormat[0].Video.FPS=15\r\n"
        "table.Encode[1].MainFormat[0].Video.Width=1920\r\n"
        "table.Encode[1].MainFormat[0].Video.Height=1080\r\n"
    )
    driver = make_driver(lambda r: httpx.Response(200, text=body))
    enc = await driver.get_encode_settings()
    await driver.close()
    assert enc[0] == {
        "codec": "H.265", "bitrate": 4096, "bitrate_control": "CBR",
        "fps": 25, "resolution": "2592x1944",
    }
    # resolution yoksa Width x Height'ten türetilir
    assert enc[1]["resolution"] == "1920x1080"
    assert enc[1]["codec"] == "H.264H" and enc[1]["fps"] == 15


class _Id:
    device_type, serial, software_version = "NVR616", "S1", "1.0"


async def test_probe_prefers_hint_then_defaults(monkeypatch):
    """probe_device önce kullanıcının verdiği (https,port) ipucunu, sonra
    standart 443/80'i dener; ilk kimlik-doğrulayan şema/portu döndürür — böylece
    özel port da bulunur, kayıtta http/https otomatik doğrulanır."""
    tried = []

    class FakeDriver:
        def __init__(self, base_url, u, p, **kw):
            self.base_url = base_url
            tried.append(base_url)

        async def get_identity(self):
            if self.base_url.endswith(":80"):  # yalnızca http:80 doğrular
                return _Id()
            raise DriverError("ulaşılamadı")

        async def get_channel_count(self):
            return 64

        async def close(self):
            pass

    monkeypatch.setattr("dahua_monitor.drivers.cgi.CgiDriver", FakeDriver)
    from dahua_monitor.drivers.cgi import probe_device

    res = await probe_device("1.2.3.4", "admin", "x", prefer=(True, 8443))
    assert tried[0].endswith(":8443"), "ipucu önce denenmeli"
    assert res["found"] and res["https"] is False and res["port"] == 80
    assert res["channels"] == 64 and res["device_type"] == "NVR616"


async def test_probe_auth_fail_stops(monkeypatch):
    """Yanlış parolada ilk ulaşılan şemada durur (lockout koruması) — ikinci
    şema DENENMEZ."""
    tried = []

    class FakeDriver:
        def __init__(self, base_url, u, p, **kw):
            tried.append(base_url)

        async def get_identity(self):
            raise AuthFailed("red")

        async def close(self):
            pass

    monkeypatch.setattr("dahua_monitor.drivers.cgi.CgiDriver", FakeDriver)
    from dahua_monitor.drivers.cgi import probe_device

    res = await probe_device("1.2.3.4", "admin", "wrong")
    assert res["found"] is False
    assert len(tried) == 1, "auth reddinde ikinci şema denenmemeli"
