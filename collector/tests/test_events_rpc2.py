"""Olay akışı ve RPC2 istemcisi testleri (ağ yerine MockTransport)."""

import json

import httpx

from dahua_monitor.alerts import AlertManager
from dahua_monitor.config import AlertingConfig, DeviceConfig
from dahua_monitor.drivers import CgiDriver, Rpc2Client
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
        if payload["method"] == "RAID.getDevices":
            assert payload["session"] == "S1"
            return httpx.Response(200, json={
                "id": payload["id"], "result": True,
                "params": {"raids": [{
                    "Name": "/dev/md0", "State": "Degrade",
                    "RebuildProgress": 37, "Members": ["/dev/sda", "/dev/sdb"],
                }]},
            })
        return httpx.Response(200, json={"id": payload["id"], "result": True})

    client = Rpc2Client("http://nvr.test", "monitor", "secret")
    client._client = httpx.AsyncClient(
        base_url="http://nvr.test", transport=httpx.MockTransport(handler)
    )
    details = await client.get_raid_details()
    await client.close()

    assert details["/dev/md0"]["rebuild_pct"] == 37
    assert details["/dev/md0"]["members"] == ["/dev/sda", "/dev/sdb"]
    # 2 login çağrısında parola alanı: önce boş (challenge), sonra MD5 digest
    logins = [c for c in calls if c["method"] == "global.login"]
    assert logins[0]["params"]["password"] == ""
    assert len(logins[1]["params"]["password"]) == 32  # MD5 hex
