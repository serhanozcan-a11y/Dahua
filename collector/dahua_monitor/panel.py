"""Web arayüzü: İzleme Panosu + cihaz yönetimi (FastAPI).

`/` tek sayfalık arayüzü (panel_ui.html) sunar; sayfa veriyi JSON API'den
çeker ve 30 sn'de bir tazeler. Parolalar SECRET_KEY (Fernet) ile şifrelenip
device_config tablosuna yazılır; HTTP Basic (admin / PANEL_PASSWORD) tüm
uçları korur.

Çalıştırma: uvicorn dahua_monitor.panel:app --host 0.0.0.0 --port 8000
Gerekli ortam: DATABASE_URL, SECRET_KEY, PANEL_PASSWORD
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_pool: asyncpg.Pool | None = None
_fernet: Fernet | None = None
_UI = Path(__file__).with_name("panel_ui.html")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _pool, _fernet
    _fernet = Fernet(os.environ["SECRET_KEY"].encode())
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    yield
    await _pool.close()


app = FastAPI(title="Dahua Filo İzleme", lifespan=_lifespan)
_security = HTTPBasic()


def _auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    expected = os.environ.get("PANEL_PASSWORD", "")
    ok = (
        secrets.compare_digest(credentials.username, "admin")
        and expected
        and secrets.compare_digest(credentials.password, expected)
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/", response_class=HTMLResponse)
async def index(_: str = Depends(_auth)) -> str:
    return _UI.read_text(encoding="utf-8")


# ------------------------------------------------------------------ JSON API


def _f(v) -> float | None:
    return None if v is None else float(v)


@app.get("/api/overview")
async def overview(_: str = Depends(_auth)) -> dict:
    async with _pool.acquire() as conn:
        devices = await conn.fetch(
            """
            SELECT coalesce(d.name, n.name) AS name,
                   d.id AS cfg_id, d.host AS cfg_host, d.port, d.enabled,
                   d.min_retention_days, d.overwrite_recording,
                   n.id AS nvr_id, n.host AS seen_host, n.device_type,
                   n.software_version, n.last_seen
            FROM device_config d
            FULL OUTER JOIN nvr n ON n.name = d.name
            ORDER BY 1
            """
        )
        latest_nvr = {
            r["nvr_id"]: r
            for r in await conn.fetch(
                """SELECT DISTINCT ON (nvr_id) nvr_id, reachable, latency_ms
                   FROM nvr_metrics ORDER BY nvr_id, ts DESC"""
            )
        }
        disks: dict[int, list] = {}
        for r in await conn.fetch(
            """SELECT DISTINCT ON (nvr_id, disk_name) nvr_id, disk_name, state,
                      total_bytes, used_bytes, health_ok
               FROM disk_metrics ORDER BY nvr_id, disk_name, ts DESC"""
        ):
            disks.setdefault(r["nvr_id"], []).append(r)
        raids: dict[int, list] = {}
        for r in await conn.fetch(
            """SELECT DISTINCT ON (nvr_id, raid_name) nvr_id, raid_name, level,
                      state, rebuild_pct
               FROM raid_metrics ORDER BY nvr_id, raid_name, ts DESC"""
        ):
            raids.setdefault(r["nvr_id"], []).append(r)
        retention = {
            r["nvr_id"]: _f(r["retention_days"])
            for r in await conn.fetch(
                """SELECT DISTINCT ON (nvr_id) nvr_id, retention_days
                   FROM retention_metrics ORDER BY nvr_id, ts DESC"""
            )
        }
        fill_hist: dict[int, list] = {}
        for r in await conn.fetch(
            """SELECT nvr_id, date_trunc('hour', ts) AS h,
                      avg(100.0*used_bytes/nullif(total_bytes,0)) AS v
               FROM disk_metrics
               WHERE ts > now() - interval '24 hours' AND total_bytes > 0
               GROUP BY 1, 2 ORDER BY 2"""
        ):
            fill_hist.setdefault(r["nvr_id"], []).append(_f(r["v"]))
        ret_hist: dict[int, list] = {}
        for r in await conn.fetch(
            """SELECT nvr_id, ts, retention_days FROM retention_metrics
               WHERE ts > now() - interval '7 days' ORDER BY ts"""
        ):
            ret_hist.setdefault(r["nvr_id"], []).append(_f(r["retention_days"]))

    out = []
    for d in devices:
        nid = d["nvr_id"]
        m = latest_nvr.get(nid)
        dks = disks.get(nid, [])
        total = sum(x["total_bytes"] for x in dks)
        used = sum(x["used_bytes"] for x in dks)
        out.append(
            {
                "name": d["name"],
                "cfg_id": d["cfg_id"],
                "host": d["cfg_host"] or d["seen_host"],
                "port": d["port"],
                "enabled": d["enabled"],
                "managed": d["cfg_id"] is not None,
                "model": d["device_type"] or "",
                "fw": d["software_version"] or "",
                "last_seen": d["last_seen"].isoformat(sep=" ", timespec="minutes")
                if d["last_seen"]
                else None,
                "reachable": m["reachable"] if m else None,
                "latency_ms": _f(m["latency_ms"]) if m else None,
                "min_retention_days": d["min_retention_days"],
                "overwrite": d["overwrite_recording"]
                if d["overwrite_recording"] is not None
                else True,
                "retention_days": retention.get(nid),
                "fill_pct": (100.0 * used / total) if total else None,
                "disks": [
                    {
                        "name": x["disk_name"],
                        "state": x["state"],
                        "fill_pct": (100.0 * x["used_bytes"] / x["total_bytes"])
                        if x["total_bytes"]
                        else None,
                        "health_ok": x["health_ok"],
                    }
                    for x in dks
                ],
                "raids": [
                    {
                        "name": x["raid_name"],
                        "level": x["level"],
                        "state": x["state"],
                        "rebuild_pct": _f(x["rebuild_pct"]),
                    }
                    for x in raids.get(nid, [])
                ],
                "fill_hist": fill_hist.get(nid, []),
                "ret_hist": ret_hist.get(nid, []),
            }
        )
    return {"devices": out}


@app.get("/api/alarms")
async def alarms(_: str = Depends(_auth)) -> dict:
    rows = await _pool.fetch(
        """
        SELECT e.id, e.ts, e.code, e.severity, e.source,
               e.payload->>'message' AS message, n.name AS device
        FROM event e LEFT JOIN nvr n ON n.id = e.nvr_id
        WHERE e.acked_by IS NULL
          AND e.source <> 'event-stream'  -- ham telemetri; alarmı zaten motor üretir
        ORDER BY e.ts DESC LIMIT 100
        """
    )
    return {
        "alarms": [
            {
                "id": r["id"],
                "ts": r["ts"].isoformat(sep=" ", timespec="seconds"),
                "code": r["code"],
                "severity": r["severity"],
                "source": r["source"],
                "message": r["message"] or "",
                "device": r["device"] or "-",
            }
            for r in rows
        ]
    }


@app.post("/api/alarms/{event_id}/ack")
async def ack_alarm(event_id: int, user: str = Depends(_auth)) -> dict:
    await _pool.execute(
        "UPDATE event SET acked_by=$2 WHERE id=$1 AND acked_by IS NULL",
        event_id,
        user,
    )
    return {"ok": True}


# ------------------------------------------------------- Cihaz yönetimi (form)


@app.post("/devices")
async def upsert_device(
    _: str = Depends(_auth),
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form("monitor"),
    password: str = Form(...),
    port: int | None = Form(None),
    https: bool = Form(False),
    overwrite: bool = Form(False),
    min_retention_days: int | None = Form(None),
    max_channels: int = Form(32),
) -> RedirectResponse:
    enc = _fernet.encrypt(password.encode()).decode()
    await _pool.execute(
        """
        INSERT INTO device_config
            (name, host, port, username, password_enc, https,
             overwrite_recording, min_retention_days, max_channels)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        ON CONFLICT (name) DO UPDATE SET
            host=$2, port=$3, username=$4, password_enc=$5, https=$6,
            overwrite_recording=$7, min_retention_days=$8, max_channels=$9,
            updated_at=now()
        """,
        name.strip(), host.strip(), port, username.strip(), enc, https,
        overwrite, min_retention_days, max_channels,
    )
    return RedirectResponse("/#yonetim", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/devices/{device_id}/toggle")
async def toggle_device(device_id: int, _: str = Depends(_auth)) -> RedirectResponse:
    await _pool.execute(
        "UPDATE device_config SET enabled = NOT enabled, updated_at=now() WHERE id=$1",
        device_id,
    )
    return RedirectResponse("/#yonetim", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/devices/{device_id}/delete")
async def delete_device(device_id: int, _: str = Depends(_auth)) -> RedirectResponse:
    await _pool.execute("DELETE FROM device_config WHERE id=$1", device_id)
    return RedirectResponse("/#yonetim", status_code=status.HTTP_303_SEE_OTHER)
