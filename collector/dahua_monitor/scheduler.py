"""Cihaz başına eşzamanlı (asyncio) sorgulama döngüsü.

Her cihaz kendi görevinde döner; bir cihazın yavaşlığı diğerlerini etkilemez.
AuthFailed durumunda cihaz DURAKLATILIR (lockout koruması) ve log'a kritik
kayıt düşülür — Faz 2'de buradan alarm motoruna olay gidecek.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from .alerts import EVENT_CODE_SEVERITY, AlertManager, Severity
from .config import DeviceConfig
from .drivers import AuthFailed, CgiDriver, DriverError, Rpc2Client
from .models import PollResult
from .store import Store

log = logging.getLogger(__name__)

EVENT_CODES = list(EVENT_CODE_SEVERITY)

# Yetkisiz giriş tespiti: yerel konsol ve yönetici grubu tanımları
_LOCAL_ADDR = {"yerel", "local", "localhost", "127.0.0.1", ""}
_PRIVILEGED_GROUPS = {"admin", "administrator"}


def _is_local(addr) -> bool:
    return str(addr).strip().lower() in _LOCAL_ADDR


def _is_privileged(group) -> bool:
    return str(group).strip().lower() in _PRIVILEGED_GROUPS


async def poll_once(driver: CgiDriver) -> PollResult:
    start = time.monotonic()
    try:
        identity = await driver.get_identity()
        disks = await driver.get_disks()
        raids = await driver.get_raids()
    except AuthFailed:
        raise
    except DriverError as exc:
        return PollResult(reachable=False, error=str(exc))
    latency = (time.monotonic() - start) * 1000
    return PollResult(
        reachable=True, latency_ms=latency, identity=identity, disks=disks, raids=raids
    )


async def device_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    driver = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    rpc2 = (
        Rpc2Client(cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls)
        if cfg.rpc2
        else None
    )
    nvr_id = await store.upsert_nvr(cfg.name, cfg.host)
    # Cihazların hepsinin aynı anda sorgulanmaması için başlangıç jitter'ı
    await _sleep(stop, random.uniform(0, min(10, cfg.poll_interval_s)))
    try:
        while not stop.is_set():
            try:
                result = await poll_once(driver)
            except AuthFailed as exc:
                log.critical(
                    "%s: kimlik doğrulama reddedildi, cihaz duraklatıldı "
                    "(lockout koruması). Parolayı düzeltip servisi yeniden "
                    "başlatın. Hata: %s",
                    cfg.name,
                    exc,
                )
                await store.write_poll(
                    nvr_id, PollResult(reachable=False, error=f"auth: {exc}")
                )
                if alerts is not None:
                    await alerts.auth_failed(cfg)
                return
            if result.reachable and result.raids and rpc2 is not None:
                await _enrich_raid(cfg, rpc2, result)
            await store.write_poll(nvr_id, result)
            if alerts is not None:
                await alerts.evaluate_poll(cfg, result)
            if not result.reachable:
                log.warning("%s: erişilemedi: %s", cfg.name, result.error)
            else:
                log.info(
                    "%s: %d disk, %d raid, %.0f ms",
                    cfg.name,
                    len(result.disks),
                    len(result.raids),
                    result.latency_ms or 0,
                )
            interval = (
                cfg.poll_interval_s if result.reachable else cfg.reachability_interval_s
            )
            await _sleep(stop, interval)
    finally:
        await driver.close()
        if rpc2 is not None:
            await rpc2.close()


async def _enrich_raid(cfg: DeviceConfig, rpc2: Rpc2Client, result: PollResult) -> None:
    """RAID kayıtlarını RPC2 detayıyla zenginleştirir; hata izlemeyi bozmaz."""
    try:
        details = await rpc2.get_raid_details()
    except (DriverError, AuthFailed) as exc:
        log.debug("%s: RPC2 RAID detayı alınamadı: %s", cfg.name, exc)
        return
    for raid in result.raids:
        detail = details.get(raid.name)
        if not detail:
            continue
        # RPC2, CGI'da bulunmayan gerçek RAID seviyesini, üye/arıza sayısını ve
        # (arıza varsa) DEGRADED durumunu verir; state override edilerek bir disk
        # arızası alarm motorunda RaidDegraded kritiğine dönüşür.
        if detail.get("level"):
            raid.level = detail["level"]
        if detail.get("state") is not None:
            raid.state = detail["state"]
        if detail.get("rebuild_pct") is not None:
            raid.rebuild_pct = float(detail["rebuild_pct"])
        if detail.get("members"):
            raid.members = list(detail["members"])
        # Arıza/çalışan disk sayıları geçmiş ve panel için raw'a (JSONB) yazılır
        raid.raw = {
            **(raid.raw or {}),
            "rpc2": {
                **{
                    k: detail.get(k)
                    for k in (
                        "failed_devices",
                        "working_devices",
                        "active_devices",
                        "total_devices",
                        "spare_devices",
                        "level_num",
                    )
                },
                # Üye fiziksel disk adları + yuva/yedek detayı (disk sağlığı ızgarası)
                "members": list(detail.get("members") or []),
                "members_detail": list(detail.get("members_detail") or []),
            },
        }


async def event_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    """Anlık olay aboneliği: arıza polling'i beklemeden saniyeler içinde işlenir.

    Bağlantı koptuğunda üstel backoff ile yeniden bağlanır (en fazla 5 dk).
    Polling her zaman güvence katmanı olarak ayrıca çalışır.
    """
    if not cfg.event_stream:
        return
    backoff = 5.0
    while not stop.is_set():
        driver = CgiDriver(
            cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
        )
        try:
            async for code, action, index in driver.stream_events(EVENT_CODES):
                backoff = 5.0
                severity = EVENT_CODE_SEVERITY.get(code, Severity.WARNING)
                log.info("%s: anlık olay %s %s index=%s", cfg.name, code, action, index)
                try:
                    await store.write_event(
                        cfg.name, "event-stream", code, severity.value,
                        f"{code} {action} (index={index})",
                    )
                except Exception:
                    log.exception("%s: olay veritabanına yazılamadı", cfg.name)
                if alerts is not None:
                    await alerts.device_event(cfg, code, action, index)
        except AuthFailed:
            log.critical("%s: olay akışı auth reddi, akış durduruldu", cfg.name)
            return
        except DriverError as exc:
            log.warning("%s: olay akışı koptu: %s", cfg.name, exc)
        finally:
            await driver.close()
        await _sleep(stop, backoff)
        backoff = min(backoff * 2, 300.0)


async def retention_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    """Günde bir: cihazdaki en eski kaydın tarihi (fiilî saklama derinliği).

    Kanal kanal mediaFileFind taraması cihaz için polling'den daha maliyetli
    olduğundan ayrı ve seyrek bir döngüdür; hata durumunda 1 saat sonra
    yeniden dener.
    """
    if not cfg.retention_check:
        return
    driver = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    nvr_id = await store.upsert_nvr(cfg.name, cfg.host)
    await _sleep(stop, random.uniform(30, 90))  # açılış polling'iyle çakışmasın
    try:
        while not stop.is_set():
            try:
                oldest = await driver.get_oldest_recording(cfg.channels)
            except AuthFailed:
                log.critical("%s: retention sorgusu auth reddi, durduruldu", cfg.name)
                return
            except DriverError as exc:
                log.warning("%s: retention sorgusu başarısız: %s", cfg.name, exc)
                await _sleep(stop, 3600)
                continue
            retention_days = None
            if oldest is not None:
                retention_days = (time.time() - oldest.timestamp()) / 86400
                log.info(
                    "%s: en eski kayıt %s (%.1f gün)",
                    cfg.name,
                    oldest.isoformat(sep=" "),
                    retention_days,
                )
                if (
                    cfg.min_retention_days is not None
                    and retention_days < cfg.min_retention_days
                ):
                    log.warning(
                        "%s: saklama derinliği %.1f gün, alt sınır %d günün ALTINDA",
                        cfg.name,
                        retention_days,
                        cfg.min_retention_days,
                    )
            else:
                log.warning("%s: hiçbir kanalda kayıt bulunamadı", cfg.name)
            await store.write_retention(nvr_id, oldest, retention_days)
            if alerts is not None:
                await alerts.evaluate_retention(cfg, retention_days)
                # Saat sapması denetimi: cihaz saati gerçek zamandan çok saparsa
                # (2000'e düşmüş vb.) kayıt tarihleri/retention anlamsızlaşır.
                try:
                    dev_time = await driver.get_device_time()
                except (DriverError, AuthFailed):
                    dev_time = None
                await alerts.clock_drift(cfg, dev_time)
            await _sleep(stop, cfg.retention_interval_s)
    finally:
        await driver.close()


async def login_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    """Yetkisiz giriş tespiti: cihaza bağlı kullanıcıları periyodik çekip
    allowlist dışı bir kaynaktan YÖNETİCİ (admin) girişi görülürse alarm üretir.

    Yerel konsol (Yerel/Local) ve login_allowlist'teki kaynak IP'ler muaftır.
    İzlemeyi bozmaz; hata halinde bir sonraki turda yeniden dener. Alarm anahtarı
    (cihaz+adres+kullanıcı) olduğundan aynı oturum tekrar tekrar spam üretmez.
    """
    if not cfg.login_watch:
        return
    driver = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    allow = {str(a).strip() for a in cfg.login_allowlist}
    await _sleep(stop, random.uniform(20, 60))  # açılışta diğer döngülerle çakışmasın
    try:
        while not stop.is_set():
            try:
                users = await driver.get_active_users()
            except AuthFailed:
                log.critical(
                    "%s: bağlı kullanıcı sorgusu auth reddi, giriş izleme durduruldu",
                    cfg.name,
                )
                return
            except DriverError as exc:
                log.warning("%s: bağlı kullanıcı sorgusu başarısız: %s", cfg.name, exc)
                await _sleep(stop, min(cfg.login_interval_s, 300))
                continue
            flagged = 0
            for u in users:
                addr = str(u.get("ClientAddress", ""))
                if (
                    _is_privileged(u.get("Group", ""))
                    and not _is_local(addr)
                    and addr not in allow
                ):
                    flagged += 1
                    log.warning(
                        "%s: yetkisiz yönetici girişi: %s@%s (%s)",
                        cfg.name, u.get("Name"), addr, u.get("ClientType"),
                    )
                    if alerts is not None:
                        await alerts.unauthorized_login(cfg, u)
            log.info(
                "%s: %d bağlı kullanıcı, %d allowlist-dışı yönetici",
                cfg.name, len(users), flagged,
            )
            await _sleep(stop, cfg.login_interval_s)
    finally:
        await driver.close()


async def camera_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    """Kamera (kanal) bağlantı izleme: RPC2 getCameraState ile kopan kameraları
    tespit eder, cihaz başına anlık özet (toplam/bağlı/kopuk) yazar ve kopan
    kameraya alarm üretir; yeniden bağlanınca alarmı kapatır.

    RPC2 gerektirir (varsayılan açık). İzlemeyi bozmaz; hata halinde yeniden dener.
    """
    if not cfg.rpc2:
        return
    driver = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    rpc2 = Rpc2Client(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    nvr_id = await store.upsert_nvr(cfg.name, cfg.host)
    names: dict[int, str] = {}
    await _sleep(stop, random.uniform(25, 70))  # diğer döngülerle çakışmasın
    try:
        while not stop.is_set():
            try:
                states = await rpc2.get_camera_states()
            except AuthFailed:
                log.critical("%s: kamera durumu auth reddi, izleme durduruldu", cfg.name)
                return
            except DriverError as exc:
                log.warning("%s: kamera durumu alınamadı: %s", cfg.name, exc)
                await _sleep(stop, min(cfg.camera_interval_s, 300))
                continue
            # 'Empty' = kamera atanmamış boş slot -> sayılmaz. Diğer her durum bir
            # kameradır: 'Connected' bağlı, aksi (Disconnect/ConnectFailed...) kopuk.
            real = {
                ch: st for ch, st in states.items() if str(st).lower() != "empty"
            }
            if real:
                if not names:  # kanal adları nadiren değişir, bir kez çek
                    try:
                        names = {
                            c["idx"] - 1: c["name"]
                            for c in await driver.get_channel_names()
                        }
                    except DriverError:
                        names = {}
                offline_list, online = [], 0
                for ch, st in sorted(real.items()):
                    connected = str(st).lower() == "connected"
                    label = names.get(ch, f"Kanal {ch + 1}")
                    if connected:
                        online += 1
                    else:
                        offline_list.append(
                            {"channel": ch + 1, "name": label, "state": st}
                        )
                    if alerts is not None:
                        await alerts.camera_state(cfg, ch + 1, label, connected)
                await store.write_camera_state(
                    nvr_id, len(real), online, len(offline_list), offline_list
                )
                log.info(
                    "%s: %d kamera, %d bağlı, %d kopuk",
                    cfg.name, len(real), online, len(offline_list),
                )
            await _sleep(stop, cfg.camera_interval_s)
    finally:
        await driver.close()
        await rpc2.close()


def _smart_message(d: dict) -> str:
    """SMART değerlendirmesinden okunur alarm mesajı."""
    parts = []
    if d.get("predict"):
        parts.append("üretici arıza öngörüsü")
    if d.get("pending"):
        parts.append(f"{d['pending']} bekleyen sektör")
    if d.get("uncorrectable"):
        parts.append(f"{d['uncorrectable']} düzeltilemeyen sektör")
    if d.get("reallocated"):
        parts.append(f"{d['reallocated']} yeniden atanmış sektör")
    if d.get("temperature_c") is not None and d["temperature_c"] >= 55:
        parts.append(f"{d['temperature_c']}°C sıcaklık")
    detail = ", ".join(parts) if parts else "SMART eşiği aşıldı"
    return f"{d['disk']} disk SMART uyarısı: {detail} — arıza öncesi kontrol edin"


async def smart_loop(
    cfg: DeviceConfig, store: Store, stop: asyncio.Event, alerts: AlertManager | None
) -> None:
    """Fiziksel disk SMART izleme (öngörücü bakım): her diskin SMART tablosunu
    periyodik çeker, değerlendirir, cihaz başına yazar ve bozulma BAŞLADIĞINDA
    (bekleyen/yeniden-atanmış sektör, sıcaklık, üretici öngörüsü) alarm üretir —
    disk arızalanmadan önce. RPC2 gerektirir (varsayılan açık)."""
    if not cfg.rpc2:
        return
    rpc2 = Rpc2Client(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    # CGI factory.getCollect: disk adları — eski firmware'de RPC2 getDeviceInfos
    # çalışmadığında SMART için fiziksel disk listesi yedeği.
    cgi = CgiDriver(
        cfg.base_url, cfg.username, cfg.password, verify_tls=cfg.verify_tls
    )
    nvr_id = await store.upsert_nvr(cfg.name, cfg.host)
    await _sleep(stop, random.uniform(40, 100))  # diğer döngülerle çakışmasın
    try:
        while not stop.is_set():
            try:
                names = await cgi.get_storage_device_names()
            except (DriverError, AuthFailed):
                names = []
            try:
                disks = await rpc2.get_disk_smart(names)
            except AuthFailed:
                log.critical("%s: SMART sorgusu auth reddi, izleme durduruldu", cfg.name)
                return
            except DriverError as exc:
                log.warning("%s: SMART alınamadı: %s", cfg.name, exc)
                await _sleep(stop, min(cfg.smart_interval_s, 600))
                continue
            crit = warn = 0
            for d in disks:
                await store.write_disk_smart(nvr_id, d)
                await store.write_disk_smart_history(nvr_id, d)  # trend/öngörü geçmişi
                if d["health"] == "crit":
                    crit += 1
                elif d["health"] == "warn":
                    warn += 1
                if alerts is not None:
                    if d["health"] == "ok":
                        await alerts.disk_smart(cfg, d["disk"], "ok", "")
                    else:
                        await alerts.disk_smart(
                            cfg, d["disk"], d["health"], _smart_message(d)
                        )
            if disks:
                log.info(
                    "%s: %d disk SMART, %d kritik, %d uyarı",
                    cfg.name, len(disks), crit, warn,
                )
            await _sleep(stop, cfg.smart_interval_s)
    finally:
        await rpc2.close()
        await cgi.close()


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def _device_sig(cfg: DeviceConfig) -> tuple:
    """Cihazın döngülerini etkileyen ayarlarının imzası; değişince yeniden
    başlatmayı tetikler (ör. panelden port/parola düzeltme)."""
    return (
        cfg.host, cfg.port, cfg.username, cfg.password, cfg.https, cfg.verify_tls,
        cfg.poll_interval_s, cfg.reachability_interval_s, cfg.overwrite_recording,
        cfg.event_stream, cfg.rpc2, cfg.retention_check, cfg.max_channels,
        cfg.first_channel, cfg.min_retention_days, cfg.login_watch,
        tuple(cfg.login_allowlist or ()), cfg.login_interval_s,
    )


def _plan_reload(running_sigs: dict, current: list[DeviceConfig]) -> tuple:
    """(eklenecek, degisecek, kaldirilacak) cihaz adlarını hesaplar."""
    cur = {c.name: _device_sig(c) for c in current}
    add = [n for n in cur if n not in running_sigs]
    change = [n for n in cur if n in running_sigs and running_sigs[n] != cur[n]]
    remove = [n for n in running_sigs if n not in cur]
    return add, change, remove


async def run_all(
    devices: list[DeviceConfig],
    store: Store,
    alerts: AlertManager | None = None,
    reload_fn=None,
    reload_interval_s: int = 60,
) -> None:
    """Cihaz başına döngüleri yönetir. `reload_fn` verilirse (async, güncel
    cihaz listesini döndürür) her `reload_interval_s` saniyede bir çağrılır:
    panelden EKLENEN cihaz restart'sız izlemeye alınır, KALDIRILAN durdurulur,
    AYARI DEĞİŞEN (ör. yanlış port/parola düzeltilmiş) yeniden başlatılır."""
    stop = asyncio.Event()
    running: dict[str, dict] = {}

    def _start(cfg: DeviceConfig) -> None:
        ds = asyncio.Event()
        tasks = [
            asyncio.create_task(f(cfg, store, ds, alerts))
            for f in (device_loop, retention_loop, event_loop, login_loop,
                      camera_loop, smart_loop)
        ]
        running[cfg.name] = {"stop": ds, "tasks": tasks, "sig": _device_sig(cfg)}

    async def _stop(name: str) -> None:
        r = running.pop(name, None)
        if r:
            r["stop"].set()
            await asyncio.gather(*r["tasks"], return_exceptions=True)

    for cfg in devices:
        _start(cfg)
    try:
        while not stop.is_set():
            await _sleep(stop, reload_interval_s)
            if stop.is_set() or reload_fn is None:
                continue
            try:
                current = await reload_fn()
            except Exception:
                log.exception("cihaz listesi yeniden yüklenemedi")
                continue
            by_name = {c.name: c for c in current}
            sigs = {n: r["sig"] for n, r in running.items()}
            add, change, remove = _plan_reload(sigs, current)
            for name in add:
                log.info("yeni cihaz izlemeye alındı: %s (%s)", name, by_name[name].host)
                _start(by_name[name])
            for name in change:
                log.info("cihaz ayarı değişti, yeniden başlatılıyor: %s", name)
                await _stop(name)
                _start(by_name[name])
            for name in remove:
                log.info("cihaz izlemeden çıkarıldı: %s", name)
                await _stop(name)  # önce döngüler dursun (yeniden-yaratma yarışı yok)
                try:
                    if await store.delete_device_data(name):
                        log.info("%s: nvr satırı ve metrikleri temizlendi", name)
                except Exception:
                    log.exception("%s: kalıntı veri temizlenemedi", name)
    finally:
        stop.set()
        for r in running.values():
            r["stop"].set()
        allt = [t for r in running.values() for t in r["tasks"]]
        if allt:
            await asyncio.gather(*allt, return_exceptions=True)
