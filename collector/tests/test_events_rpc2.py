"""Olay akışı ve RPC2 istemcisi testleri (ağ yerine MockTransport)."""

import json

import httpx

from dahua_monitor.alerts import AlertManager
from dahua_monitor.config import AlertingConfig, DeviceConfig
from dahua_monitor.drivers import CgiDriver, Rpc2Client
from dahua_monitor.models import RaidState
from tests.test_alerts import FakeNotifier


def make_cgi(handler) -> CgiDriver:
    driver = CgiDriver("http://nvr.test", "monitor", "secret")
    driver._client = httpx.AsyncClient(
        base_url="http://nvr.test", transport=httpx.MockTransport(handler)
    )
    return driver


async def test_stream_events_parsing():
    body = (
        b"--myboundary\r\nContent-Type: text/plain\r\n\r\n"
        b"Code=StorageFailure;action=Start;index=1\r\n"
        b"Heartbeat\r\n"
        b"Code=StorageFailure;action=Stop;index=1\r\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "eventManager.cgi" in str(request.url)
        return httpx.Response(200, content=body)

    driver = make_cgi(handler)
    events = [e async for e in driver.stream_events(["StorageFailure"])]
    await driver.close()
    assert events == [
        ("StorageFailure", "Start", "1"),
        ("StorageFailure", "Stop", "1"),
    ]


async def test_device_event_alert_start_stop():
    notifier = FakeNotifier()
    mgr = AlertManager(AlertingConfig(), [notifier])
    dev = DeviceConfig(name="nvr-1", host="h", username="u", password="p")
    await mgr.device_event(dev, "StorageFailure", "Start", "1")
    await mgr.device_event(dev, "StorageFailure", "Start", "1")  # dedup
    assert len(notifier.sent) == 1 and "CRITICAL" in notifier.sent[0][0]
    await mgr.device_event(dev, "StorageFailure", "Stop", "1")
    assert len(notifier.sent) == 2 and "Recovered" in notifier.sent[1][0]


async def test_rpc2_login_and_raid_details():
    """StorageDeviceManager.getDeviceInfos (factory nesne deseni) ile RAID
    üye/arıza detayı; bir disk arızası DEGRADED'e türetilmeli, RAID olmayan
    (tekil disk) kayıtları atlanmalı."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if str(request.url.path) == "/RPC2_Login":
            if not payload["params"]["password"]:
                return httpx.Response(200, json={
                    "id": payload["id"], "session": "S1", "result": False,
                    "params": {"realm": "r", "random": "42"},
                })
            return httpx.Response(
                200, json={"id": payload["id"], "session": "S1", "result": True}
            )
        method = payload["method"]
        if method == "StorageDeviceManager.factory.instance":
            return httpx.Response(200, json={"id": payload["id"], "result": 4242})
        if method == "StorageDeviceManager.getDeviceInfos":
            assert payload.get("object") == 4242
            assert payload["session"] == "S1"
            return httpx.Response(200, json={
                "id": payload["id"], "result": True,
                "params": {"device": [
                    {
                        "Name": "/dev/md0",
                        "Raid": {
                            "Level": 5, "State": ["Degraded"],
                            "FailedDevices": 1, "WorkingDevices": 7,
                            "TotalDevices": 8, "SpareDevices": 0, "Sync": 0,
                            "Members": ["/dev/sda", "/dev/sdb"],
                        },
                    },
                    {"Name": "/dev/sdz"},  # RAID olmayan -> atlanmalı
                ]},
            })
        return httpx.Response(200, json={"id": payload["id"], "result": True})

    client = Rpc2Client("http://nvr.test", "monitor", "secret")
    client._client = httpx.AsyncClient(
        base_url="http://nvr.test", transport=httpx.MockTransport(handler)
    )
    details = await client.get_raid_details()
    await client.close()

    d = details["/dev/md0"]
    assert d["level"] == "RAID5"
    assert d["state"] is RaidState.DEGRADED
    assert d["failed_devices"] == 1
    assert d["working_devices"] == 7
    assert d["total_devices"] == 8
    assert d["members"] == ["/dev/sda", "/dev/sdb"]
    assert "/dev/sdz" not in details  # RAID olmayan kayıt atlandı
    # 2 login çağrısında parola alanı: önce boş (challenge), sonra MD5 digest
    logins = [c for c in calls if c["method"] == "global.login"]
    assert logins[0]["params"]["password"] == ""
    assert len(logins[1]["params"]["password"]) == 32  # MD5 hex


def test_rpc2_raid_state_derivation():
    f = Rpc2Client._derive_raid_state
    assert f("active", 0, 0) is RaidState.ACTIVE
    assert f("clean", 0, 0) is RaidState.ACTIVE
    assert f("active", 1, 0) is RaidState.DEGRADED     # arızalı üye -> degraded
    assert f("degraded", 0, 0) is RaidState.DEGRADED
    assert f("recovering", 1, 50) is RaidState.REBUILDING
    assert f("active", 0, 40) is RaidState.REBUILDING  # sync sürüyor
    assert f("inactive", 2, 0) is RaidState.FAILED


async def test_rpc2_reauth_on_session_expiry():
    """Uzun ömürlü istemcide oturum düşerse (ilk getDeviceInfos hata döner)
    get_raid_details yeniden giriş yapıp bir kez daha denemeli — aksi halde
    ilk poll'dan sonra RAID zenginleştirmesi sessizce durur."""
    state = {"logins": 0, "infos": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if str(request.url.path) == "/RPC2_Login":
            if not payload["params"]["password"]:
                return httpx.Response(200, json={
                    "id": payload["id"], "session": "S", "result": False,
                    "params": {"realm": "r", "random": "1"}})
            state["logins"] += 1
            return httpx.Response(200, json={
                "id": payload["id"], "session": "S%d" % state["logins"], "result": True})
        method = payload["method"]
        if method == "StorageDeviceManager.factory.instance":
            return httpx.Response(200, json={"id": payload["id"], "result": 99})
        if method == "StorageDeviceManager.getDeviceInfos":
            state["infos"] += 1
            if state["infos"] == 1:  # ilk çağrı: oturum düşmüş gibi hata
                return httpx.Response(200, json={
                    "id": payload["id"], "result": False,
                    "error": {"code": 287637505, "message": ""}})
            return httpx.Response(200, json={
                "id": payload["id"], "result": True,
                "params": {"device": [{"Name": "/dev/md0", "Raid": {
                    "Level": 5, "State": ["Active"], "FailedDevices": 0,
                    "WorkingDevices": 8, "TotalDevices": 8, "Members": ["/dev/sda"]}}]}})
        return httpx.Response(200, json={"id": payload["id"], "result": True})

    client = Rpc2Client("http://nvr.test", "monitor", "secret")
    client._client = httpx.AsyncClient(
        base_url="http://nvr.test", transport=httpx.MockTransport(handler))
    details = await client.get_raid_details()
    await client.close()

    assert details["/dev/md0"]["level"] == "RAID5"
    assert details["/dev/md0"]["working_devices"] == 8
    assert state["logins"] == 2   # ilk giriş + oturum düşünce yeniden giriş
    assert state["infos"] == 2    # ilk (hata) + retry (başarılı)


def test_login_watch_helpers():
    from dahua_monitor.scheduler import _is_local, _is_privileged
    assert _is_local("Yerel") and _is_local("127.0.0.1") and _is_local("")
    assert not _is_local("10.20.56.30")
    assert _is_privileged("admin") and _is_privileged("Administrator")
    assert not _is_privileged("user")


async def test_unauthorized_login_alarm():
    """Allowlist dışı yönetici girişi HIGH UnauthorizedLogin alarmı üretmeli;
    aynı oturum tekrar bastırılmalı (dedup)."""
    notifier = FakeNotifier()
    mgr = AlertManager(AlertingConfig(), [notifier])
    dev = DeviceConfig(name="nvr-1", host="h", username="u", password="p")
    u = {"Name": "admin", "ClientAddress": "10.20.56.30", "ClientType": "Local",
         "Group": "admin", "LoginTime": "2026-08-26 13:08"}
    await mgr.unauthorized_login(dev, u)
    await mgr.unauthorized_login(dev, u)  # aynı -> dedup
    assert len(notifier.sent) == 1
    subj, body = notifier.sent[0]
    assert "HIGH" in subj and "UnauthorizedLogin" in subj
    assert "10.20.56.30" in body and "admin" in body


def test_device_sig_stable_and_sensitive():
    from dahua_monitor.scheduler import _device_sig
    d1 = DeviceConfig(name="x", host="h", username="u", password="p", port=443)
    d2 = DeviceConfig(name="x", host="h", username="u", password="p", port=443)
    assert _device_sig(d1) == _device_sig(d2)         # aynı config -> aynı imza
    d3 = DeviceConfig(name="x", host="h", username="u", password="PPP", port=443)
    assert _device_sig(d1) != _device_sig(d3)         # parola değişti -> imza değişti
    d4 = DeviceConfig(name="x", host="h", username="u", password="p", port=80)
    assert _device_sig(d1) != _device_sig(d4)         # port değişti -> imza değişti


def test_plan_reload_add_change_remove():
    """Süpervizör: panelden eklenen/kaldırılan/değişen cihazları doğru planlamalı."""
    from dahua_monitor.scheduler import _device_sig, _plan_reload
    a = DeviceConfig(name="a", host="10.0.0.1", username="u", password="p")
    b = DeviceConfig(name="b", host="10.0.0.2", username="u", password="p")
    running = {"a": _device_sig(a), "b": _device_sig(b)}
    a2 = DeviceConfig(name="a", host="10.0.0.1", username="u", password="p",
                      port=443, https=True)           # a: ayar değişti
    c = DeviceConfig(name="c", host="10.0.0.3", username="u", password="p")  # yeni
    add, change, remove = _plan_reload(running, [a2, c])   # b kaldırıldı
    assert add == ["c"]
    assert change == ["a"]
    assert remove == ["b"]
