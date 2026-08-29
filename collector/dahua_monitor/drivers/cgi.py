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

    async def _storage_infos(self) -> list[dict]:
        text = await self._get("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo")
        return parsing.storage_infos(parsing.parse_kv_tree(text))

    @staticmethod
    def _is_raid(info: dict) -> bool:
        name = str(info.get("Name", ""))
        return "/md" in name or "raid" in str(info.get("Type", "")).lower()

    async def get_disks(self) -> list[DiskInfo]:
        disks: list[DiskInfo] = []
        for info in await self._storage_infos():
            if self._is_raid(info):
                continue
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
                    health_ok=bool(health) if health is not None else None,
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
            end = (datetime.now() + timedelta(days=1)).strftime(self._TS_FMT)
            cond = (
                f"action=findFile&object={token}"
                f"&condition.Channel={channel}"
                f"&condition.StartTime={quote('2000-01-01 00:00:00')}"
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
