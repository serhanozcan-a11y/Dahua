"""HTTP CGI sürücüsü — birincil veri yolu.

Kimlik doğrulama stratejisi: önce Digest denenir (güncel firmware'lerin tek
kabul ettiği yöntem), 401 dönerse bir kez Basic denenir (çok eski firmware).
İkisi de reddedilirse AuthFailed yükseltilir ve üst katman cihazı duraklatır —
lockout koruması nedeniyle bu sürücü asla kendi kendine parola retry yapmaz.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

from .. import parsing
from ..models import DeviceIdentity, DiskInfo, DiskState, RaidInfo, RaidState
from .base import AuthFailed, DriverError


def _as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None

_STATE_MAP = {
    "success": DiskState.OK,
    "runing": DiskState.OK,      # bazı firmware'lerde "Runing" (sic) döner
    "running": DiskState.OK,
    "failure": DiskState.ERROR,
    "error": DiskState.ERROR,
    "absent": DiskState.ABSENT,
    "notexist": DiskState.ABSENT,
}

_RAID_STATE_MAP = {
    "active": RaidState.ACTIVE,
    "clean": RaidState.ACTIVE,
    "degraded": RaidState.DEGRADED,
    "recovering": RaidState.REBUILDING,
    "rebuilding": RaidState.REBUILDING,
    "failed": RaidState.FAILED,
    "inactive": RaidState.FAILED,
}


class CgiDriver:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        verify_tls: bool = False,
        timeout_s: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auths: list[httpx.Auth] = [
            httpx.DigestAuth(username, password),
            httpx.BasicAuth(username, password),
        ]
        self._auth: httpx.Auth | None = None  # keşfedilen çalışan yöntem
        self._client = httpx.AsyncClient(
            base_url=self._base_url, verify=verify_tls, timeout=timeout_s
        )

    async def _get(self, path: str) -> str:
        auths = [self._auth] if self._auth else self._auths
        last: httpx.Response | None = None
        for auth in auths:
            try:
                resp = await self._client.get(path, auth=auth)
            except httpx.HTTPError as exc:
                raise DriverError(f"{self._base_url}{path}: {exc}") from exc
            if resp.status_code == 401:
                last = resp
                continue
            if resp.status_code >= 400:
                raise DriverError(f"{path}: HTTP {resp.status_code}")
            self._auth = auth
            return resp.text
        self._auth = None
        raise AuthFailed(f"{self._base_url}: Digest ve Basic reddedildi (HTTP 401)")

    async def _get_bytes(self, path: str) -> bytes:
        """İkili (JPEG vb.) yanıt için _get'in bayt sürümü."""
        auths = [self._auth] if self._auth else self._auths
        for auth in auths:
            try:
                resp = await self._client.get(path, auth=auth)
            except httpx.HTTPError as exc:
                raise DriverError(f"{self._base_url}{path}: {exc}") from exc
            if resp.status_code == 401:
                continue
            if resp.status_code >= 400:
                raise DriverError(f"{path}: HTTP {resp.status_code}")
            self._auth = auth
            return resp.content
        self._auth = None
        raise AuthFailed(f"{self._base_url}: Digest ve Basic reddedildi (HTTP 401)")

    async def get_snapshot(self, channel: int) -> bytes:
        """Bir kanaldan anlık JPEG kare (snapshot.cgi). channel 1 tabanlıdır."""
        return await self._get_bytes(f"/cgi-bin/snapshot.cgi?channel={int(channel)}")

    async def reboot(self) -> bool:
        """Cihazı yeniden başlatır (magicBox.cgi?action=reboot). GERİ ALINAMAZ;
        cihaz 1-2 dk erişilemez olur ve kayıt kesilir. Çağıran onay almalı."""
        txt = await self._get("/cgi-bin/magicBox.cgi?action=reboot")
        return "error" not in txt.lower()

    async def set_time(self, dt: datetime) -> bool:
        """Cihazın saatini AYARLAR (global.cgi setCurrentTime). Cihaz-yazma; dt
        cihazın YEREL saati olmalı (zaman dilimine göre). Kayıt tarihleri buna
        göre damgalanır — bozuk saati anında düzeltir."""
        ts = quote(dt.strftime("%Y-%m-%d %H:%M:%S"))
        txt = await self._get(f"/cgi-bin/global.cgi?action=setCurrentTime&time={ts}")
        return "ok" in txt.lower()

    async def set_ntp(
        self, server: str, *, port: int = 123, tz: int = 3, period: int = 60
    ) -> bool:
        """NTP'yi etkinleştirir ve ortak sunucuya ayarlar (configManager setConfig).
        Cihaz-yazma. tz = zaman dilimi indeksi (3 = UTC+3, Türkiye)."""
        params = [
            ("NTP.Enable", "true"),
            ("NTP.Address", server),
            ("NTP.Port", str(int(port))),
            ("NTP.TimeZone", str(int(tz))),
            ("NTP.UpdatePeriod", str(int(period))),
            ("NTP.ServerList[0].Enable", "true"),
            ("NTP.ServerList[0].Address", server),
            ("NTP.ServerList[0].Port", str(int(port))),
        ]
        q = "&".join(f"{k}={quote(str(v))}" for k, v in params)
        txt = await self._get(f"/cgi-bin/configManager.cgi?action=setConfig&{q}")
        return "error" not in txt.lower()

    async def get_ntp_config(self) -> dict:
        """Cihazın NTP ayarı (enable/server/port/tz)."""
        try:
            text = await self._get(
                "/cgi-bin/configManager.cgi?action=getConfig&name=NTP"
            )
        except DriverError:
            return {}
        kv = {}
        for line in text.replace("\r", "").split("\n"):
            if line.startswith("table.NTP.") and "=" in line:
                k, v = line.split("=", 1)
                kv[k[len("table.NTP."):].strip()] = v.strip()
        return {
            "enable": str(kv.get("Enable", "")).lower() == "true",
            "server": kv.get("Address", ""),
            "port": _as_int(kv.get("Port")) or 123,
            "tz": _as_int(kv.get("TimeZone")),
        }

    async def get_storage_device_names(self) -> list[str]:
        """Depolama aygıt adları (/dev/sdX fiziksel diskler, /dev/mdX RAID) —
        storageDevice.cgi?action=factory.getCollect. Eski firmware'de
        getDeviceAllInfo çökse bile bu çalışır; SMART için disk listesi kaynağı."""
        try:
            text = await self._get(
                "/cgi-bin/storageDevice.cgi?action=factory.getCollect"
            )
        except DriverError:
            return []
        names: list[str] = []
        for line in text.replace("\r", "").split("\n"):
            line = line.strip()
            if line.startswith("list[") and "=" in line:
                val = line.split("=", 1)[1].strip()
                if val:
                    names.append(val)
        return names

    async def get_ad_config(self) -> dict:
        """NVR'ın kendi Active Directory ayarı (configManager). Enable/Server/
        Port/BaseDN/Filter."""
        try:
            text = await self._get(
                "/cgi-bin/configManager.cgi?action=getConfig&name=ActiveDirectory"
            )
        except DriverError:
            return {}
        rows = parsing.parse_kv_tree(text).get("table", {}).get("ActiveDirectory", [])
        if isinstance(rows, dict):
            rows = [rows]
        a = rows[0] if rows else {}
        return {
            "enable": str(a.get("Enable", "")).lower() == "true",
            "server": a.get("Server", "") or "",
            "port": _as_int(a.get("Port")) or 389,
            "base_dn": a.get("BaseDN", "") or "",
            "filter": a.get("Filter", "") or "",
        }

    async def set_ad_config(
        self, enable: bool, server: str, port: int, base_dn: str, filter_: str = ""
    ) -> bool:
        """NVR'ın Active Directory ayarını YAZAR (configManager setConfig). Bu bir
        cihaz-yazma işlemidir; çağıran açık onay almalı."""
        params = [
            ("ActiveDirectory[0].Enable", "true" if enable else "false"),
            ("ActiveDirectory[0].Server", server),
            ("ActiveDirectory[0].Port", str(int(port))),
            ("ActiveDirectory[0].BaseDN", base_dn),
            ("ActiveDirectory[0].Filter", filter_),
        ]
        q = "&".join(f"{k}={quote(str(v))}" for k, v in params)
        txt = await self._get(f"/cgi-bin/configManager.cgi?action=setConfig&{q}")
        return "error" not in txt.lower()

    async def get_identity(self) -> DeviceIdentity:
        merged: dict[str, str] = {}
        for action in ("getDeviceType", "getSerialNo", "getSoftwareVersion"):
            text = await self._get(f"/cgi-bin/magicBox.cgi?action={action}")
            merged.update(parsing.parse_flat(text))
        return DeviceIdentity(
            device_type=merged.get("type", ""),
            serial=merged.get("sn", ""),
            software_version=merged.get("version", ""),
        )

    async def get_active_users(self) -> list[dict]:
        """Cihaza o an bağlı (oturum açmış) kullanıcılar — userManager.cgi.

        Her kayıt: Name, Group, ClientType, ClientAddress (IP ya da yerel için
        'Yerel'/'Local'), LoginTime, Id. Yetkisiz giriş tespiti (scheduler'daki
        login_loop) bu listeyi allowlist'e karşı değerlendirir.
        """
        text = await self._get(
            "/cgi-bin/userManager.cgi?action=getActiveUserInfoAll"
        )
        tree = parsing.parse_kv_tree(text)
        users = tree.get("users", [])
        if isinstance(users, dict):
            users = [users]
        return [u for u in users if isinstance(u, dict)]

    async def get_users(self) -> list[dict]:
        """Tanımlı kullanıcı hesapları (userManager getUserInfoAll) — güvenlik
        denetimi için. Her kayıt: name, group (admin/user), memo."""
        try:
            text = await self._get("/cgi-bin/userManager.cgi?action=getUserInfoAll")
        except DriverError:
            return []
        users = parsing.parse_kv_tree(text).get("users", [])
        if isinstance(users, dict):
            users = [users]
        out = []
        for u in users:
            if isinstance(u, dict) and u.get("Name"):
                out.append({
                    "name": u.get("Name", ""),
                    "group": u.get("Group", ""),
                    "memo": u.get("Memo", ""),
                })
        return out

    async def get_channel_count(self) -> int | None:
        """Kanal sayısı: configManager ChannelTitle girdilerinin sayısı.

        (`magicBox.getProductDefinition` bu firmware'de yok; ChannelTitle her
        sürümde var ve kanal başına bir girdi döner.)
        """
        try:
            text = await self._get(
                "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"
            )
        except DriverError:
            return None
        titles = parsing.parse_kv_tree(text).get("table", {}).get("ChannelTitle", [])
        if isinstance(titles, dict):
            titles = [titles]
        return len(titles) or None

    async def get_channel_names(self) -> list[dict]:
        """Kanal (kamera) adları: configManager ChannelTitle -> [{idx, name}].

        Her kanal için 1 tabanlı indeks ve kullanıcı tarafından verilen ad döner
        (ör. 'XRAY KARA TARAFI'). Firmware bazı slotları boş bırakabilir; boş ad
        'Kanal N' olarak doldurulur.
        """
        try:
            text = await self._get(
                "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle"
            )
        except DriverError:
            return []
        titles = parsing.parse_kv_tree(text).get("table", {}).get("ChannelTitle", [])
        if isinstance(titles, dict):
            titles = [titles]
        out = []
        for i, t in enumerate(titles):
            name = (t.get("Name") if isinstance(t, dict) else "") or f"Kanal {i + 1}"
            out.append({"idx": i + 1, "name": name})
        return out

    async def get_encode_settings(self) -> dict[int, dict]:
        """Kanal başına ANA AKIŞ (MainFormat[0]) video ayarları: codec, bit hızı
        (kbps), kare hızı (fps), çözünürlük, bit hızı kontrolü (CBR/VBR).

        configManager Encode çıktısı büyük olduğundan yalnız MainFormat[0].Video
        satırları ayrıştırılır. 0 tabanlı kanal indeksiyle döner (kanal 1 -> 0)."""
        try:
            text = await self._get(
                "/cgi-bin/configManager.cgi?action=getConfig&name=Encode"
            )
        except DriverError:
            return {}
        prefix = "table.Encode["
        marker = "].MainFormat[0].Video."
        raw: dict[int, dict] = {}
        for line in text.replace("\r", "").split("\n"):
            line = line.strip()
            if not line.startswith(prefix) or marker not in line:
                continue
            key, _, val = line.partition("=")
            try:
                ch = int(key[len(prefix):key.index("]")])
            except ValueError:
                continue
            raw.setdefault(ch, {})[key.rsplit(".", 1)[-1]] = val.strip()
        out: dict[int, dict] = {}
        for ch, v in raw.items():
            res = v.get("resolution")
            if not res and v.get("Width") and v.get("Height"):
                res = f"{v['Width']}x{v['Height']}"
            out[ch] = {
                "codec": v.get("Compression"),
                "bitrate": _as_int(v.get("BitRate")),
                "bitrate_control": v.get("BitRateControl"),
                "fps": _as_int(v.get("FPS")),
                "resolution": res,
            }
        return out

    async def _storage_infos(self) -> list[dict]:
        text = await self._get("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo")
        return parsing.storage_infos(parsing.parse_kv_tree(text))

    @staticmethod
    def _is_raid(info: dict) -> bool:
        name = str(info.get("Name", ""))
        return "/md" in name or "raid" in str(info.get("Type", "")).lower()

    async def get_disks(self) -> list[DiskInfo]:
        # Firmware farkı: kimi cihaz fiziksel diskleri ayrı listeler
        # (/dev/sda...), kimi (ör. NVR5xxx RAID modu) tüm depolamayı tek bir
        # dizi kaydı (/dev/md0) olarak verip fiziksel disk göstermez. İkinci
        # durumda kapasiteyi kaybetmemek için diziyi de kapasite birimi say.
        # (getDeviceAllInfo üst düzey kayıtları çakışmaz; üye diskler ayrı
        # listelenmediğinden çift sayım olmaz.)
        infos = await self._storage_infos()
        physical = [i for i in infos if not self._is_raid(i)]
        source = physical if physical else infos
        disks: list[DiskInfo] = []
        for info in source:
            is_raid = self._is_raid(info)
            details = info.get("Detail") or [{}]
            if isinstance(details, dict):
                details = [details]
            total = sum(int(d.get("TotalBytes", 0) or 0) for d in details)
            used = sum(int(d.get("UsedBytes", 0) or 0) for d in details)
            is_error = any(bool(d.get("IsError", False)) for d in details)
            health = info.get("HealthDataFlag", details[0].get("HealthDataFlag"))
            state = _STATE_MAP.get(
                str(info.get("State", "")).lower(), DiskState.UNKNOWN
            )
            if is_error and state is DiskState.OK:
                state = DiskState.ERROR
            disks.append(
                DiskInfo(
                    name=str(info.get("Name", "?")),
                    state=state,
                    total_bytes=total,
                    used_bytes=used,
                    is_error=is_error,
                    # RAID dizisinin per-disk SMART bayrağı yoktur (HealthDataFlag=0
                    # gelir) -> bilinmiyor (None); aksi halde sahte SMART alarmı
                    # üretirdi. Fiziksel diskte HealthDataFlag davranışı korunur.
                    health_ok=None
                    if is_raid
                    else (bool(health) if health is not None else None),
                    type=str(details[0].get("Type", "")),
                    raw=info,
                )
            )
        return disks

    async def get_raids(self) -> list[RaidInfo]:
        # MVP: getDeviceAllInfo içindeki RAID kayıtları. Rebuild yüzdesi ve üye
        # listesi gibi detaylar Faz 2'de Rpc2Driver ile zenginleşecek.
        raids: list[RaidInfo] = []
        for info in await self._storage_infos():
            if not self._is_raid(info):
                continue
            state_raw = str(info.get("State", "")).lower()
            state = _RAID_STATE_MAP.get(state_raw)
            if state is None:
                # "Success" gibi genel disk durumları da dönebiliyor
                state = (
                    RaidState.ACTIVE
                    if _STATE_MAP.get(state_raw) is DiskState.OK
                    else RaidState.UNKNOWN
                )
            raids.append(
                RaidInfo(
                    name=str(info.get("Name", "?")),
                    level=str(info.get("Type", "")),
                    state=state,
                    raw=info,
                )
            )
        return raids

    # --- Anlık olay akışı --------------------------------------------------

    async def stream_events(self, codes: list[str]):
        """`eventManager.cgi?action=attach` uzun ömürlü akışı.

        (code, action, index) üçlüleri üretir; bağlantı kapanınca generator
        biter — yeniden bağlanma ve backoff üst katmanın (scheduler) işidir.
        """
        path = (
            "/cgi-bin/eventManager.cgi?action=attach&codes=["
            + ",".join(codes)
            + "]"
        )
        auths = [self._auth] if self._auth else self._auths
        got_401 = False
        for auth in auths:
            try:
                async with self._client.stream(
                    "GET", path, auth=auth, timeout=httpx.Timeout(15, read=None)
                ) as resp:
                    if resp.status_code == 401:
                        got_401 = True
                        continue
                    if resp.status_code >= 400:
                        raise DriverError(f"event stream: HTTP {resp.status_code}")
                    self._auth = auth
                    async for raw in resp.aiter_lines():
                        if "Code=" in raw:
                            yield self._parse_event(raw)
                    return
            except httpx.HTTPError as exc:
                raise DriverError(f"event stream: {exc}") from exc
        if got_401:
            self._auth = None
            raise AuthFailed(f"{self._base_url}: event stream auth reddedildi")

    @staticmethod
    def _parse_event(line: str) -> tuple[str, str, str]:
        # Satır biçimi: "...Code=StorageFailure;action=Start;index=0..."
        fields: dict[str, str] = {}
        for part in line[line.index("Code=") :].strip().split(";"):
            key, _, value = part.partition("=")
            fields[key.strip()] = value.strip()
        return (
            fields.get("Code", ""),
            fields.get("action", ""),
            fields.get("index", ""),
        )

    # --- En eski kayıt tarihi (saklama derinliği) -------------------------
    #
    # mediaFileFind.cgi akışı: factory.create -> findFile (2000'den bugüne,
    # kanal bazında) -> findNextFile&count=1 (sonuçlar zaman sıralı geldiği
    # için ilk dosya en eskisidir) -> close + destroy. Kanal numaralandırması
    # firmware'e göre 0 veya 1 tabanlı olabilir; Faz 0 saha testinde
    # doğrulanacak (config: first_channel).

    _TS_FMT = "%Y-%m-%d %H:%M:%S"
    # Saklama üst sınırı: cihaz saati sıfırlanmışsa (ör. 2000-01-01 tarihli
    # dosyalar — NTP/pil yok) bunlar gerçek saklama değildir. Sorguyu makul bir
    # tabandan (son ~5 yıl) başlatarak bu artefaktları eleriz; hiçbir gözetim
    # NVR'ı 5 yıldan fazla tutmaz.
    _MAX_RETENTION_DAYS = 1825

    async def get_device_time(self) -> datetime | None:
        """Cihazın KENDİ saati (global.cgi getCurrentTime). Saat sapması tespiti
        için — cihaz saati bozuksa (ör. 2000) retention/kayıt tarihleri anlamsız."""
        try:
            text = await self._get("/cgi-bin/global.cgi?action=getCurrentTime")
        except DriverError:
            return None
        raw = parsing.parse_flat(text).get("result", "")
        try:
            return datetime.strptime(str(raw).strip(), self._TS_FMT)
        except ValueError:
            return None

    async def get_oldest_recording(self, channels: list[int]) -> datetime | None:
        oldest: datetime | None = None
        for ch in channels:
            ts = await self._oldest_on_channel(ch)
            if ts is not None and (oldest is None or ts < oldest):
                oldest = ts
        return oldest

    async def _oldest_on_channel(self, channel: int) -> datetime | None:
        text = await self._get("/cgi-bin/mediaFileFind.cgi?action=factory.create")
        token = parsing.parse_flat(text).get("result")
        if not token:
            raise DriverError("mediaFileFind: finder oluşturulamadı")
        try:
            now = datetime.now()
            end = (now + timedelta(days=1)).strftime(self._TS_FMT)
            start = (now - timedelta(days=self._MAX_RETENTION_DAYS)).strftime(
                self._TS_FMT
            )
            cond = (
                f"action=findFile&object={token}"
                f"&condition.Channel={channel}"
                f"&condition.StartTime={quote(start)}"
                f"&condition.EndTime={quote(end)}"
            )
            try:
                found_resp = await self._get(f"/cgi-bin/mediaFileFind.cgi?{cond}")
            except DriverError:
                return None  # bu kanalda kayıt yok (firmware Error/400 döner)
            if "OK" not in found_resp:
                return None
            text = await self._get(
                f"/cgi-bin/mediaFileFind.cgi?action=findNextFile"
                f"&object={token}&count=1"
            )
            tree = parsing.parse_kv_tree(text)
            items = tree.get("items", [])
            if isinstance(items, dict):
                items = [items]
            for item in items:
                raw = str(item.get("StartTime", ""))
                try:
                    return datetime.strptime(raw, self._TS_FMT)
                except ValueError:
                    continue
            return None
        finally:
            for action in ("close", "destroy"):
                try:
                    await self._get(
                        f"/cgi-bin/mediaFileFind.cgi?action={action}&object={token}"
                    )
                except DriverError:
                    pass  # finder temizliği best-effort

    async def close(self) -> None:
        await self._client.aclose()


async def _http_reachable(host: str, verify_tls: bool = False, timeout_s: float = 5.0) -> bool:
    """host herhangi bir HTTP yanıtı veriyor mu (durum kodu ne olursa olsun) —
    'ulaşılabilir ama Dahua NVR değil' (ör. firewall) ile 'hiç ulaşılamıyor'
    ayrımı için. Yönlendirme takip edilir."""
    for url in (f"http://{host}", f"https://{host}"):
        try:
            async with httpx.AsyncClient(
                verify=verify_tls, timeout=timeout_s, follow_redirects=True
            ) as c:
                await c.get(url)
                return True
        except Exception:
            continue
    return False


async def probe_device(
    host: str,
    username: str,
    password: str,
    *,
    prefer: tuple[bool, int] | None = None,
    verify_tls: bool = False,
    timeout_s: float = 10.0,
) -> dict:
    """host + kimlikle şema/portları dener; ilk kimlik-doğrulayan şema/porttan
    cihaz kimliğini + kanal sayısını döndürür. Yeni cihaz eklerken (panel
    /devices/probe ve kayıtta otomatik-tespit) http/https, port ve kanal
    sayısını otomatik bulmak için kullanılır.

    `prefer` verilirse (kullanıcının girdiği (https, port) — ör. özel bir port)
    önce o denenir, sonra standart https:443 ve http:80.

    Döner: {found:True, https, port, scheme, device_type, serial,
    software_version, channels} ya da {found:False, error}. Yanlış parolada tek
    deneme yapılır (lockout koruması): kimlik reddi ilk ulaşılan şemada
    anlaşılır ve sonraki şema denenmez.
    """
    candidates: list[tuple[bool, int]] = []
    if prefer and prefer[1]:
        candidates.append((bool(prefer[0]), int(prefer[1])))
    for c in ((True, 443), (False, 80)):
        if c not in candidates:
            candidates.append(c)
    last_err = ""
    for https, port in candidates:
        scheme = "https" if https else "http"
        driver = CgiDriver(
            f"{scheme}://{host}:{port}", username, password,
            verify_tls=verify_tls, timeout_s=timeout_s,
        )
        try:
            identity = await driver.get_identity()  # kimlik doğrulama burada olur
        except AuthFailed:
            await driver.close()
            return {"found": False, "reason": "auth",
                    "error": "kimlik doğrulama reddedildi (kullanıcı adı/parola?)"}
        except DriverError as exc:
            last_err = str(exc)
            await driver.close()
            continue  # bu şema/port ulaşılamadı, diğerini dene
        # Boş/geçersiz kimlik (ör. firewall'ın yönlendirme/HTML yanıtı) gerçek bir
        # Dahua NVR değildir — bu adayı geç, sonunda 'not_dahua' olarak işaretlenir.
        if not (identity.device_type or "").strip():
            last_err = "geçerli Dahua kimliği alınamadı"
            await driver.close()
            continue
        try:
            channels = await driver.get_channel_count()
        except DriverError:
            channels = None
        finally:
            await driver.close()
        return {
            "found": True, "https": https, "port": port, "scheme": scheme,
            "device_type": identity.device_type, "serial": identity.serial,
            "software_version": identity.software_version, "channels": channels,
        }
    # Hiçbir aday Dahua kimliği vermedi. Ham HTTP yanıtı geliyorsa adres
    # ulaşılabilir ama Dahua NVR değil (ör. firewall); hiç yanıt yoksa erişilemez.
    if await _http_reachable(host, verify_tls=verify_tls):
        return {"found": False, "reason": "not_dahua",
                "error": "adres yanıt veriyor ama Dahua NVR değil (firewall/başka cihaz olabilir)"}
    return {"found": False, "reason": "unreachable",
            "error": f"cihaza ulaşılamadı ({last_err})".strip()}
