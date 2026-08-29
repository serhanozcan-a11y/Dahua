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

from .base import AuthFailed, DriverError


def _md5_upper(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest().upper()


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

    async def get_raid_details(self) -> dict[str, dict[str, Any]]:
        """RAID adı -> {state, rebuild_pct, members, hot_spares} (best effort)."""
        params = await self.call("RAID.getDevices")
        items = params.get("raids") or params.get("devices") or []
        out: dict[str, dict[str, Any]] = {}
        for item in items:
            name = item.get("Name") or item.get("name") or ""
            if not name:
                continue
            out[name] = {
                "state": item.get("State") or item.get("state") or "",
                "rebuild_pct": item.get("RebuildProgress", item.get("Progress")),
                "members": item.get("Members") or [],
                "hot_spares": item.get("HotSpares") or [],
            }
        return out

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
