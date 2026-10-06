"""AlertManager kuralları: geçiş bazlı tetikleme, tekilleştirme, düzelme."""

from dahua_monitor.alerts import AlertManager
from dahua_monitor.config import AlertingConfig, DeviceConfig
from dahua_monitor.models import (
    DiskInfo,
    DiskState,
    PollResult,
    RaidInfo,
    RaidState,
)


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


def make_manager():
    notifier = FakeNotifier()
    return AlertManager(AlertingConfig(), [notifier]), notifier


def make_dev(**kw) -> DeviceConfig:
    return DeviceConfig(name="nvr-1", host="h", username="u", password="p", **kw)


def ok_poll(**kw) -> PollResult:
    return PollResult(reachable=True, **kw)


async def test_down_after_three_failures_and_recovery():
    mgr, notifier = make_manager()
    dev = make_dev()
    down = PollResult(reachable=False, error="timeout")
    await mgr.evaluate_poll(dev, down)
    await mgr.evaluate_poll(dev, down)
    assert notifier.sent == [], "3'ten önce alarm yok"
    await mgr.evaluate_poll(dev, down)
    assert len(notifier.sent) == 1 and "DeviceDown" in notifier.sent[0][0]
    await mgr.evaluate_poll(dev, down)
    assert len(notifier.sent) == 1, "tekrar bildirim yok (dedup)"
    await mgr.evaluate_poll(dev, ok_poll())
    assert len(notifier.sent) == 2 and "Recovered" in notifier.sent[1][0]


async def test_disk_error_fires_once_and_clears():
    mgr, notifier = make_manager()
    dev = make_dev()
    bad = ok_poll(disks=[DiskInfo("/dev/sda", DiskState.ERROR, is_error=True)])
    await mgr.evaluate_poll(dev, bad)
    await mgr.evaluate_poll(dev, bad)
    assert len(notifier.sent) == 1 and "StorageFailure" in notifier.sent[0][0]
    await mgr.evaluate_poll(dev, ok_poll(disks=[DiskInfo("/dev/sda", DiskState.OK)]))
    assert len(notifier.sent) == 2 and "Recovered" in notifier.sent[1][0]


async def test_absent_only_on_transition():
    mgr, notifier = make_manager()
    dev = make_dev()
    # İlk görüşte absent: hiç takılmamış slot — alarm üretilmemeli
    await mgr.evaluate_poll(dev, ok_poll(disks=[DiskInfo("/dev/sdb", DiskState.ABSENT)]))
    assert notifier.sent == []
    # OK gördükten sonra kaybolursa alarm üretilmeli
    await mgr.evaluate_poll(dev, ok_poll(disks=[DiskInfo("/dev/sdb", DiskState.OK)]))
    await mgr.evaluate_poll(dev, ok_poll(disks=[DiskInfo("/dev/sdb", DiskState.ABSENT)]))
    assert len(notifier.sent) == 1 and "StorageNotExist" in notifier.sent[0][0]


async def test_raid_degraded_and_rebuild():
    mgr, notifier = make_manager()
    dev = make_dev()
    await mgr.evaluate_poll(
        dev, ok_poll(raids=[RaidInfo("/dev/md0", "Raid5", RaidState.DEGRADED)])
    )
    assert "RaidDegraded" in notifier.sent[0][0]
    await mgr.evaluate_poll(
        dev, ok_poll(raids=[RaidInfo("/dev/md0", "Raid5", RaidState.ACTIVE)])
    )
    assert "Recovered" in notifier.sent[-1][0]


async def test_capacity_respects_overwrite_flag():
    dev_overwrite = make_dev()  # overwrite_recording=True (varsayılan)
    dev_no_overwrite = make_dev(overwrite_recording=False)
    full = ok_poll(disks=[DiskInfo("/dev/sda", DiskState.OK, 1000, 960)])

    mgr, notifier = make_manager()
    await mgr.evaluate_poll(dev_overwrite, full)
    assert notifier.sent == [], "döngüsel kayıtta doluluk alarmı yok"

    mgr, notifier = make_manager()
    await mgr.evaluate_poll(dev_no_overwrite, full)
    assert len(notifier.sent) == 1 and "CRITICAL" in notifier.sent[0][0]


async def test_retention_below_minimum():
    mgr, notifier = make_manager()
    dev = make_dev(min_retention_days=30)
    await mgr.evaluate_retention(dev, 12.0)
    assert len(notifier.sent) == 1 and "RetentionLow" in notifier.sent[0][0]
    await mgr.evaluate_retention(dev, 12.0)
    assert len(notifier.sent) == 1, "dedup"
    await mgr.evaluate_retention(dev, 45.0)
    assert "Recovered" in notifier.sent[-1][0]


async def test_smart_warning():
    mgr, notifier = make_manager()
    dev = make_dev()
    await mgr.evaluate_poll(
        dev, ok_poll(disks=[DiskInfo("/dev/sda", DiskState.OK, health_ok=False)])
    )
    assert len(notifier.sent) == 1 and "SmartAbnormal" in notifier.sent[0][0]
