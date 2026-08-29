"""Cihaz yönetim paneli (FastAPI).

NVR envanterini web arayüzünden yönetir; parolalar SECRET_KEY (Fernet) ile
şifrelenip device_config tablosuna yazılır — düz metin parola ne veritabanına
ne loglara düşer. Panel HTTP Basic ile korunur (kullanıcı: admin, parola:
PANEL_PASSWORD ortam değişkeni).

Çalıştırma: uvicorn dahua_monitor.panel:app --host 0.0.0.0 --port 8000
Gerekli ortam: DATABASE_URL, SECRET_KEY, PANEL_PASSWORD
"""

from __future__ import annotations

import html
import os
import secrets
from contextlib import asynccontextmanager

import asyncpg
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_pool: asyncpg.Pool | None = None
_fernet: Fernet | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _pool, _fernet
    _fernet = Fernet(os.environ["SECRET_KEY"].encode())
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
    yield
    await _pool.close()


app = FastAPI(title="Dahua NVR Yönetim Paneli", lifespan=_lifespan)
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


_PAGE = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dahua NVR Paneli</title><style>
body{{font-family:system-ui,sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;background:#f7f7f8;color:#1a1a1a}}
h1{{font-size:1.4rem}} table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #ddd;padding:.5rem .7rem;text-align:left;font-size:.92rem}}
th{{background:#eef}} .ok{{color:#0a7d33;font-weight:600}} .bad{{color:#c0202a;font-weight:600}}
form.inline{{display:inline}} fieldset{{background:#fff;border:1px solid #ddd;margin-top:1.5rem;padding:1rem}}
label{{display:inline-block;margin:.3rem 1rem .3rem 0}} input,select{{padding:.25rem}}
button{{padding:.35rem .9rem;cursor:pointer}} .note{{color:#666;font-size:.85rem}}
</style></head><body>
<h1>Dahua NVR Yönetim Paneli</h1>
<p class="note">Cihaz ekleme/değişiklik sonrası toplayıcıyı yeniden başlatın:
<code>docker compose restart collector</code></p>
<table><tr><th>Ad</th><th>Adres</th><th>Kullanıcı</th><th>Durum</th><th>Son görülme</th>
<th>Aktif</th><th></th></tr>{rows}</table>
<fieldset><legend>Cihaz ekle / güncelle (aynı ad = güncelleme)</legend>
<form method="post" action="/devices">
<label>Ad <input name="name" required placeholder="nvr-merkez-01"></label>
<label>IP/host <input name="host" required placeholder="192.168.10.11"></label>
<label>Port <input name="port" type="number" placeholder="80"></label>
<label>Kullanıcı <input name="username" value="monitor" required></label>
<label>Parola <input name="password" type="password" required></label><br>
<label>HTTPS <input name="https" type="checkbox"></label>
<label>Döngüsel kayıt (overwrite) <input name="overwrite" type="checkbox" checked></label>
<label>Min. saklama (gün) <input name="min_retention_days" type="number" min="1"></label>
<label>Kanal sayısı <input name="max_channels" type="number" value="32" min="1"></label>
<button type="submit">Kaydet</button>
</form></fieldset>
<p class="note">Parolalar SECRET_KEY ile şifrelenerek saklanır; bu panel yalnız
şirket ağından erişilebilir olmalıdır.</p>
</body></html>"""


def _row(r) -> str:
    e = html.escape
    durum = "-"
    css = ""
    if r["reachable"] is True:
        durum, css = "erişilebilir", "ok"
    elif r["reachable"] is False:
        durum, css = "ERİŞİLEMİYOR", "bad"
    son = r["last_seen"].strftime("%Y-%m-%d %H:%M") if r["last_seen"] else "-"
    port = f":{r['port']}" if r["port"] else ""
    return (
        f"<tr><td>{e(r['name'])}</td><td>{e(r['host'])}{port}</td>"
        f"<td>{e(r['username'])}</td><td class='{css}'>{durum}</td><td>{son}</td>"
        f"<td>{'evet' if r['enabled'] else 'hayır'}</td><td>"
        f"<form class='inline' method='post' action='/devices/{r['id']}/toggle'>"
        f"<button>{'durdur' if r['enabled'] else 'başlat'}</button></form> "
        f"<form class='inline' method='post' action='/devices/{r['id']}/delete' "
        f"onsubmit=\"return confirm('{e(r['name'])} silinsin mi?')\">"
        f"<button>sil</button></form></td></tr>"
    )


@app.get("/", response_class=HTMLResponse)
async def index(_: str = Depends(_auth)) -> str:
    rows = await _pool.fetch(
        """
        SELECT d.id, d.name, d.host, d.port, d.username, d.enabled,
               n.last_seen, m.reachable
        FROM device_config d
        LEFT JOIN nvr n ON n.name = d.name
        LEFT JOIN LATERAL (
            SELECT reachable FROM nvr_metrics
            WHERE nvr_id = n.id ORDER BY ts DESC LIMIT 1
        ) m ON true
        ORDER BY d.name
        """
    )
    body = "".join(_row(r) for r in rows) or (
        "<tr><td colspan=7>Henüz cihaz eklenmedi</td></tr>"
    )
    return _PAGE.format(rows=body)


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
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/devices/{device_id}/toggle")
async def toggle_device(device_id: int, _: str = Depends(_auth)) -> RedirectResponse:
    await _pool.execute(
        "UPDATE device_config SET enabled = NOT enabled, updated_at=now() WHERE id=$1",
        device_id,
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/devices/{device_id}/delete")
async def delete_device(device_id: int, _: str = Depends(_auth)) -> RedirectResponse:
    await _pool.execute("DELETE FROM device_config WHERE id=$1", device_id)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
