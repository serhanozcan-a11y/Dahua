"""RPC2 istemcisi — web arayüzünün JSON kanalı. DENEYSEL, varsayılan KAPALI.

RAID rebuild yüzdesi ve üye disk listesi gibi CGI'da bulunmayan detaylar için
kullanılır. RPC2 Dahua tarafından resmî belgelenmediğinden metot adları
firmware'e göre değişebilir; Faz 0 saha testinde gerçek cihazda doğrulanmadan
cihaz konfigürasyonunda `rpc2: true` yapılmamalıdır. Başarısız olduğunda
izleme etkilenmez — yalnızca RAID detayı zenginleştirilemez.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from ..models import RaidState
from .base import AuthFailed, DriverError


def _md5_upper(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest().upper()


def _smart_raw(byid: dict, i: int):
    a = byid.get(i)
    if not a:
        return None
    try:
        return int(str(a.get("Raw")).split()[0])
    except (TypeError, ValueError):
        return None


def _assess_smart(name: str, attrs: list) -> dict:
    """SMART öznitelik listesini öngörücü sağlık değerlendirmesine çevirir.

    KRİTİK: Predict bayrağı, herhangi özniteliğin Current<=Threshold'a düşmesi,
    bekleyen sektör (197)>0, düzeltilemeyen (198)>0, ya da sıcaklık>=60.
    UYARI: reallocated (5)>0, sıcaklık>=55, ya da reported-uncorrect (187)>0.
    Böylece disk ARIZALANMADAN, bozulma başladığında haber verilir."""
    byid: dict = {}
    for a in attrs:
        try:
            byid[int(a.get("ID"))] = a
        except (TypeError, ValueError):
            pass
    temp = _smart_raw(byid, 194)
    if temp is None:
        temp = _smart_raw(byid, 190)
    poh = _smart_raw(byid, 9)
    realloc = _smart_raw(byid, 5)
    pending = _smart_raw(byid, 197)
    uncorr = _smart_raw(byid, 198)
    reported = _smart_raw(byid, 187)
    predict = any((a.get("Predict") or 0) for a in attrs)
    over_thresh = False
    for a in attrs:
        cur, thr = a.get("Current"), a.get("Threshold")
        try:
            if thr is not None and cur is not None and int(thr) > 0 and int(cur) <= int(thr):
                over_thresh = True
        except (TypeError, ValueError):
            pass
    crit = bool(
        predict or over_thresh or (pending and pending > 0)
        or (uncorr and uncorr > 0) or (temp is not None and temp >= 60)
    )
    warn = bool(
        (realloc and realloc > 0) or (temp is not None and temp >= 55)
        or (reported and reported > 0)
    )
    return {
        "disk": name,
        "health": "crit" if crit else ("warn" if warn else "ok"),
        "temperature_c": temp,
        "power_on_hours": poh,
        "reallocated": realloc,
        "pending": pending,
        "uncorrectable": uncorr,
        "predict": bool(predict),
        "attrs": [
            {
                "id": a.get("ID"), "name": a.get("Name"),
                "current": a.get("Current"), "worst": a.get("Worst"),
                "threshold": a.get("Threshold"), "raw": a.get("Raw"),
                "predict": a.get("Predict"),
            }
            for a in attrs
        ],
    }


class Rpc2Client:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        verify_tls: bool = False,
        timeout_s: float = 15.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), verify=verify_tls, timeout=timeout_s
        )
        self._user = username
        self._password = password
        self._session: str | int | None = None
        self._id = 0

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise DriverError(f"RPC2 {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise DriverError(f"RPC2 {path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise DriverError(f"RPC2 {path}: JSON değil") from exc

    async def login(self) -> None:
        # 1. adım: challenge (realm + random) alınır
        self._id += 1
        first = await self._post(
            "/RPC2_Login",
            {
                "method": "global.login",
                "params": {
                    "userName": self._user,
                    "password": "",
                    "clientType": "Web3.0",
                },
                "id": self._id,
            },
        )
        self._session = first.get("session")
        challenge = first.get("params") or {}
        realm = challenge.get("realm", "")
        random_ = challenge.get("random", "")
        # 2. adım: MD5(user:random:MD5(user:realm:pass)) ile gerçek giriş
        digest = _md5_upper(
            f"{self._user}:{random_}:{_md5_upper(f'{self._user}:{realm}:{self._password}')}"
        )
        self._id += 1
        second = await self._post(
            "/RPC2_Login",
            {
                "method": "global.login",
                "params": {
                    "userName": self._user,
                    "password": digest,
                    "clientType": "Web3.0",
                    "authorityType": "Default",
                },
                "id": self._id,
                "session": self._session,
            },
        )
        if not second.get("result"):
            raise AuthFailed("RPC2 login reddedildi")
        self._session = second.get("session", self._session)
        # Eski firmware'ler (ör. 2016 yapıları) /RPC2 çağrılarında session'ı
        # GÖVDEDE değil DhWebClientSessionID ÇEREZİNDE bekler; gövde-session ile
        # 301 döner. Çerezi eklemek yeni firmware'i bozmaz (aynı değeri yok sayar).
        if self._session is not None:
            self._client.cookies.set("DhWebClientSessionID", str(self._session))

    async def call(self, method: str, params: dict | None = None) -> dict:
        if self._session is None:
            await self.login()
        self._id += 1
        resp = await self._post(
            "/RPC2",
            {
                "method": method,
                "params": params,
                "id": self._id,
                "session": self._session,
            },
        )
        if resp.get("result") is False:
            raise DriverError(f"RPC2 {method}: {resp.get('error')}")
        return resp.get("params") or {}

    # --- RPC2 nesne (factory) yardımcıları --------------------------------
    # Bazı servisler (StorageDeviceManager vb.) önce factory.instance ile bir
    # nesne alıp sonra çağrılara `object` alanı geçmeyi gerektirir.

    async def _instance(self, service: str, params: dict | None = None) -> Any:
        self._id += 1
        r = await self._post(
            "/RPC2",
            {
                "method": f"{service}.factory.instance",
                "params": params,
                "id": self._id,
                "session": self._session,
            },
        )
        obj = r.get("result")
        if obj in (None, False):
            raise DriverError(f"RPC2 {service}.factory.instance: nesne alınamadı")
        return obj

    async def _call_on(
        self, method: str, obj: Any, params: dict | None = None
    ) -> dict:
        self._id += 1
        r = await self._post(
            "/RPC2",
            {
                "method": method,
                "params": params,
                "object": obj,
                "id": self._id,
                "session": self._session,
            },
        )
        if r.get("result") is False:
            raise DriverError(f"RPC2 {method}: {r.get('error')}")
        return r.get("params") or {}

    async def _destroy(self, service: str, obj: Any) -> None:
        try:
            self._id += 1
            await self._post(
                "/RPC2",
                {
                    "method": f"{service}.destroy",
                    "params": None,
                    "object": obj,
                    "id": self._id,
                    "session": self._session,
                },
            )
        except DriverError:
            pass  # nesne temizliği best-effort

    @staticmethod
    def _derive_raid_state(state_words: str, failed: int, sync: Any) -> RaidState:
        rebuilding = any(
            w in state_words for w in ("recover", "rebuild", "resync")
        ) or (isinstance(sync, (int, float)) and 0 < sync < 100)
        if rebuilding:
            return RaidState.REBUILDING
        if "inactive" in state_words or "fail" in state_words:
            return RaidState.FAILED
        if failed > 0 or "degrad" in state_words:
            return RaidState.DEGRADED
        if "active" in state_words or "clean" in state_words:
            return RaidState.ACTIVE
        return RaidState.UNKNOWN

    async def get_raid_details(self) -> dict[str, dict[str, Any]]:
        """RAID adı -> {level, state, rebuild_pct, members, failed/working/total}.

        `StorageDeviceManager.getDeviceInfos` (RPC2 factory nesnesi) üzerinden;
        NVR5xxx firmware'inde RAID üye/arıza sayıları buradan gelir. Metot ve
        alan adları firmware'e göre değişebildiğinden best-effort'tur — hata
        durumunda üst katman (scheduler) izlemeyi bozmadan devam eder.

        Uzun ömürlü istemcide RPC2 oturumu poll aralığı boyunca (Dahua ~1 dk'da
        sonlandırır) zaman aşımına uğrar; bu durumda ilk çağrı hata verir. Bu
        yüzden hata halinde oturumu sıfırlayıp bir kez yeniden giriş yaparak
        tekrar denenir — aksi halde ilk poll'dan sonra zenginleştirme sessizce
        durur.
        """
        try:
            return await self._raid_details_once()
        except (DriverError, AuthFailed):
            self._session = None  # muhtemelen oturum düştü -> yeniden giriş + tek retry
            return await self._raid_details_once()

    async def _raid_details_once(self) -> dict[str, dict[str, Any]]:
        if self._session is None:
            await self.login()
        obj = await self._instance("StorageDeviceManager")
        try:
            params = await self._call_on(
                "StorageDeviceManager.getDeviceInfos", obj
            )
        finally:
            await self._destroy("StorageDeviceManager", obj)

        out: dict[str, dict[str, Any]] = {}
        for dev in params.get("device", []):
            raid = dev.get("Raid")
            name = dev.get("Name") or dev.get("name") or ""
            if not isinstance(raid, dict) or not name:
                continue  # RAID olmayan (tekil disk) kayıtları atla
            failed = int(raid.get("FailedDevices", 0) or 0)
            working = int(raid.get("WorkingDevices", 0) or 0)
            total = int(raid.get("TotalDevices", raid.get("RaidDevices", 0)) or 0)
            spare = int(raid.get("SpareDevices", 0) or 0)
            active = int(raid.get("ActiveDevices", working) or 0)
            sync = raid.get("Sync")
            level = raid.get("Level")
            # Üye disk detayları: ad (Members) ile MemberInfos (ID=yuva, Spare) indeks
            # indeks eşleşir; panel disk sağlığı ızgarası için birleştirilir.
            member_names = list(raid.get("Members") or [])
            member_infos = raid.get("MemberInfos") or []
            members_detail = []
            for i, nm in enumerate(member_names):
                mi = member_infos[i] if i < len(member_infos) and isinstance(
                    member_infos[i], dict
                ) else {}
                members_detail.append(
                    {"name": nm, "slot": mi.get("ID"), "spare": bool(mi.get("Spare"))}
                )
            state_words = " ".join(
                str(s) for s in (raid.get("State") or [])
            ).lower()
            state = self._derive_raid_state(state_words, failed, sync)
            rebuild = (
                float(sync)
                if state is RaidState.REBUILDING
                and isinstance(sync, (int, float))
                and 0 < sync <= 100
                else None
            )
            out[name] = {
                "level": f"RAID{level}" if level not in (None, "") else "",
                "level_num": level if isinstance(level, int) else None,
                "state": state,
                "rebuild_pct": rebuild,
                "members": member_names,
                "members_detail": members_detail,
                "failed_devices": failed,
                "working_devices": working,
                "active_devices": active,
                "total_devices": total,
                "spare_devices": spare,
            }
        return out

    async def get_camera_states(self) -> dict[int, str]:
        """Kanal (uzak kamera) bağlantı durumları: {kanal(0 tabanlı): durum}.

        LogicDeviceManager.getCameraState({uniqueChannels:[-1]}) tüm kanalların
        connectionState'ini döndürür ('Connected' ya da kopuk). Oturum düşerse
        bir kez yeniden giriş denenir; başarısızsa boş döner (izlemeyi bozmaz)."""
        try:
            return await self._camera_states_once()
        except (DriverError, AuthFailed):
            self._session = None
            try:
                return await self._camera_states_once()
            except (DriverError, AuthFailed):
                return {}

    async def _camera_states_once(self) -> dict[int, str]:
        if self._session is None:
            await self.login()
        params = await self.call(
            "LogicDeviceManager.getCameraState", {"uniqueChannels": [-1]}
        )
        out: dict[int, str] = {}
        for s in params.get("states", []):
            ch = s.get("channel")
            if ch is not None:
                out[int(ch)] = str(s.get("connectionState") or "Unknown")
        return out

    async def get_camera_login_errors(self, channels: list[int]) -> dict[int, int]:
        """Kopuk kameralar için giriş hata kodu (neden bağlanamıyor — tanı).
        channel 0 tabanlı. Best-effort; hata halinde o kanalı atlar."""
        if self._session is None:
            try:
                await self.login()
            except (DriverError, AuthFailed):
                return {}
        out: dict[int, int] = {}
        for ch in channels:
            try:
                r = await self.call(
                    "LogicDeviceManager.getCameraLoginErrorCode", {"channel": int(ch)}
                )
                if r.get("errorCode") is not None:
                    out[int(ch)] = r["errorCode"]
            except (DriverError, AuthFailed):
                continue
        return out

    async def get_disk_smart(self, disk_names: list[str] | None = None) -> list[dict]:
        """Her fiziksel diskin SMART tablosu + değerlendirilmiş sağlık.

        RaidManager.getSubSmartInfos({sub:/dev/sdX}) her disk için öznitelik
        listesi verir (web UI'nin kullandığı yol). Fiziksel disk adları önce
        getDeviceInfos device[].Raid.Members'tan alınır; eski firmware'de bu metod
        yoksa/param hatası verirse çağıranın verdiği `disk_names` (CGI
        factory.getCollect'ten) yedeğine düşer. Oturum düşerse bir kez yeniden
        giriş denenir; başarısızsa boş döner (izlemeyi bozmaz)."""
        try:
            return await self._disk_smart_once(disk_names)
        except (DriverError, AuthFailed):
            self._session = None
            try:
                return await self._disk_smart_once(disk_names)
            except (DriverError, AuthFailed):
                return []

    async def _disk_smart_once(self, disk_names: list[str] | None = None) -> list[dict]:
        if self._session is None:
            await self.login()
        members: list[str] = []
        sdm = await self._instance("StorageDeviceManager")
        try:
            di = await self._call_on("StorageDeviceManager.getDeviceInfos", sdm)
            for dev in di.get("device", []):
                raid = dev.get("Raid") or {}
                members += [m for m in (raid.get("Members") or []) if m]
        except DriverError:
            pass  # eski firmware: getDeviceInfos yok/param hatası → CGI yedeğine düş
        finally:
            await self._destroy("StorageDeviceManager", sdm)
        if not members and disk_names:
            # yalnız fiziksel diskler (/dev/sdX); md dizileri SMART vermez
            members = [n for n in disk_names if "/sd" in n]
        if not members:
            return []
        # RaidManager yalnız RAID üyeleri için; RAID'siz (tek disk) NVR'da
        # factory.instance başarısız olur — o durumda doğrudan devStorage'a düşeriz.
        rm = None
        try:
            rm = await self._instance("RaidManager")
        except DriverError:
            rm = None
        out: list[dict] = []
        try:
            for name in members:
                vals = None
                if rm is not None:
                    try:
                        r = await self._call_on(
                            "RaidManager.getSubSmartInfos", rm, {"sub": name}
                        )
                        vals = r.get("values")
                    except DriverError:
                        vals = None
                if not vals:
                    # RAID üyesi değilse (tek disk / RAID'siz NVR) SMART farklı
                    # metodla gelir: devStorage.getSmartValue (instance {name}).
                    vals = await self._devstorage_smart(name)
                if vals:
                    out.append(_assess_smart(name, vals))
        finally:
            if rm is not None:
                try:
                    await self._destroy("RaidManager", rm)
                except DriverError:
                    pass
        return out

    async def _devstorage_smart(self, name: str) -> list | None:
        """Tek disk (RAID'siz) SMART: devStorage.factory.instance({name}) →
        getSmartValue. RaidManager.getSubSmartInfos yalnız RAID üyelerinde çalışır."""
        try:
            obj = await self._instance("devStorage", {"name": name})
        except DriverError:
            return None
        try:
            sv = await self._call_on("devStorage.getSmartValue", obj)
            return sv.get("values")
        except DriverError:
            return None
        finally:
            try:
                await self._destroy("devStorage", obj)
            except DriverError:
                pass

    async def close(self) -> None:
        if self._session is not None:
            try:
                self._id += 1
                await self._post(
                    "/RPC2",
                    {
                        "method": "global.logout",
                        "params": None,
                        "id": self._id,
                        "session": self._session,
                    },
                )
            except DriverError:
                pass
        await self._client.aclose()
