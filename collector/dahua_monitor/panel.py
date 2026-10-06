"""Web arayüzü: İzleme Panosu + cihaz yönetimi (FastAPI).

`/` tek sayfalık arayüzü (panel_ui.html) sunar; sayfa veriyi JSON API'den
çeker ve 30 sn'de bir tazeler. Parolalar SECRET_KEY (Fernet) ile şifrelenip
device_config tablosuna yazılır; HTTP Basic (admin / PANEL_PASSWORD) tüm
uçları korur.

Çalıştırma: uvicorn dahua_monitor.panel:app --host 0.0.0.0 --port 8000
Gerekli ortam: DATABASE_URL, SECRET_KEY, PANEL_PASSWORD
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from types import SimpleNamespace

from .alerts import EmailNotifier, TelegramNotifier
from .drivers.cgi import CgiDriver, probe_device, _as_int
from .drivers.rpc2 import Rpc2Client

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
_security = HTTPBasic(auto_error=False)
_SESSION_COOKIE = "nobetci_session"


def _secret_key() -> bytes:
    return os.environ.get("SECRET_KEY", "nobetci").encode()


def _make_session(user: str = "admin") -> str:
    exp = str(int(time.time()) + 7 * 86400)
    ub = base64.urlsafe_b64encode(user.encode()).decode()
    sig = hmac.new(_secret_key(), f"{exp}.{ub}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{ub}.{sig}"


def _session_user(request: Request) -> str | None:
    parts = request.cookies.get(_SESSION_COOKIE, "").split(".")
    if len(parts) != 3:
        return None
    exp, ub, sig = parts
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    good = hmac.new(_secret_key(), f"{exp}.{ub}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        return base64.urlsafe_b64decode(ub).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def _valid_session(request: Request) -> bool:
    return _session_user(request) is not None


def _ad_authenticate(username: str, password: str, cfg: dict) -> bool:
    """Active Directory (LDAP) ile kimlik doğrular. cfg: server/domain/base_dn/
    group (panel ayarları ya da ortam değişkeni). UPN (user@domain) ile SIMPLE
    bind; group+base_dn verilmişse memberOf ile grup üyeliği de kontrol edilir.
    Yapılandırılmamışsa/hata olursa False (yerel admin girişi yine çalışır)."""
    server_url = cfg.get("server")
    domain = cfg.get("domain")
    if not server_url or not domain or not username or not password:
        return False
    try:
        import ldap3
    except ImportError:
        logging.warning("ldap3 yüklü değil — AD kimlik doğrulama devre dışı")
        return False
    upn = username if ("@" in username or "\\" in username) else f"{username}@{domain}"
    try:
        server = ldap3.Server(
            server_url, use_ssl=server_url.lower().startswith("ldaps"),
            get_info=ldap3.NONE, connect_timeout=8,
        )
        conn = ldap3.Connection(
            server, user=upn, password=password,
            authentication=ldap3.SIMPLE, receive_timeout=8,
        )
        if not conn.bind():
            return False
        group = cfg.get("group")
        base = cfg.get("base_dn")
        if group and base:
            conn.search(
                base,
                f"(|(userPrincipalName={upn})(sAMAccountName={username}))",
                attributes=["memberOf"],
            )
            member = False
            for e in conn.entries:
                groups = [str(g).lower() for g in (e.memberOf.values if "memberOf" in e else [])]
                if any(group.lower() in g for g in groups):
                    member = True
                    break
            conn.unbind()
            return member
        conn.unbind()
        return True
    except Exception as exc:
        logging.warning("AD kimlik doğrulama hatası (%s): %s", upn, exc)
        return False


async def _get_settings(prefix: str) -> dict:
    rows = await _pool.fetch(
        "SELECT key, value FROM app_settings WHERE key LIKE $1", prefix + "%"
    )
    return {r["key"]: r["value"] for r in rows}


_BUILD_RE = re.compile(r"build:(\d{4}-\d{2}-\d{2})")


def _fw_build(fw: str) -> str:
    """software_version dizesinden 'build:YYYY-MM-DD' tarihini çıkarır."""
    m = _BUILD_RE.search(fw or "")
    return m.group(1) if m else ""


async def _firmware_reference() -> dict:
    """Model → en son bilinen firmware referansı (app_settings 'firmware.reference',
    tek JSON değer). {model: {latest, build, url, note}}. Araştırmayla doldurulur,
    panelden de düzenlenebilir."""
    raw = (await _get_settings("firmware.reference")).get("firmware.reference")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _firmware_update(model: str, fw: str, ref: dict) -> dict | None:
    """Cihazın firmware'ini referans tabloyla kıyaslar. Referansta model yoksa
    None (bilinmiyor). Kıyas derleme tarihine göre — en güvenilir ölçüt.
    status: 'outdated' | 'current' | 'unknown'. 'unknown' = referansta doğrulanmış
    hedef sürüm yok (EOL / doğrulanamayan hat) — asla 'güncel' iddia edilmez."""
    if not model:
        return None
    r = ref.get(model)
    if not isinstance(r, dict):
        return None
    cur = _fw_build(fw)
    out = {
        "latest": str(r.get("latest") or ""),
        "url": str(r.get("url") or ""),
        "note": str(r.get("note") or ""),
        "current_build": cur,
    }
    if r.get("unknown"):
        out.update({"status": "unknown", "build": "", "outdated": False})
        return out
    latest_build = str(r.get("build") or "")
    outdated = bool(cur and latest_build and cur < latest_build)
    out.update({
        "build": latest_build,
        "outdated": outdated,
        "status": "outdated" if outdated else "current",
    })
    return out


async def _ad_config() -> dict:
    """AD ayarları: panelden kaydedilmiş (app_settings) > ortam değişkeni."""
    s = await _get_settings("ad.")

    def pick(k: str, env: str) -> str:
        return s.get("ad." + k) or os.environ.get(env, "")

    enabled = s.get("ad.enabled")
    if enabled is None:
        enabled = "1" if os.environ.get("AD_SERVER") else "0"
    return {
        "enabled": enabled in ("1", "true", "on"),
        "server": pick("server", "AD_SERVER"),
        "domain": pick("domain", "AD_DOMAIN"),
        "base_dn": pick("base_dn", "AD_BASE_DN"),
        "group": pick("group", "AD_GROUP"),
    }


def _valid_basic(credentials: HTTPBasicCredentials | None) -> bool:
    expected = os.environ.get("PANEL_PASSWORD", "")
    return bool(
        credentials
        and secrets.compare_digest(credentials.username, "admin")
        and expected
        and secrets.compare_digest(credentials.password, expected)
    )


def _auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    """Oturum çerezi (panel arayüzü) ya da HTTP Basic (API/curl) kabul eder."""
    u = _session_user(request)
    if u:
        return u
    if _valid_basic(credentials):
        return "admin"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Basic"},
    )


@app.get("/")
async def index(request: Request):
    if not _valid_session(request):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(_UI.read_text(encoding="utf-8"))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, hata: int = 0):
    if _valid_session(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(_login_html(error=bool(hata)))


@app.post("/login")
async def login_submit(
    password: str = Form(...), username: str = Form(""),
) -> RedirectResponse:
    username = username.strip()
    expected = os.environ.get("PANEL_PASSWORD", "")
    who = None
    # Yerel admin (break-glass): kullanıcı boş ya da 'admin' + PANEL_PASSWORD
    if username in ("", "admin") and expected and secrets.compare_digest(password, expected):
        who = "admin"
    # Active Directory (domain hesabı)
    if not who and username:
        cfg = await _ad_config()
        if cfg["enabled"] and await asyncio.to_thread(
            _ad_authenticate, username, password, cfg
        ):
            who = username
    if who:
        resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(
            _SESSION_COOKIE, _make_session(who), max_age=7 * 86400,
            httponly=True, samesite="lax",
        )
        return resp
    return RedirectResponse("/login?hata=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/me")
async def whoami(request: Request, _: str = Depends(_auth)) -> dict:
    return {"user": _session_user(request) or "admin"}


@app.get("/api/settings/ad")
async def get_ad_settings(_: str = Depends(_auth)) -> dict:
    """Kayıtlı AD yapılandırması (sır yok)."""
    return await _ad_config()


@app.post("/api/settings/ad")
async def set_ad_settings(
    _: str = Depends(_auth),
    enabled: bool = Form(False),
    server: str = Form(""),
    domain: str = Form(""),
    base_dn: str = Form(""),
    group: str = Form(""),
) -> JSONResponse:
    """AD yapılandırmasını panelden kaydeder (app_settings)."""
    vals = {
        "ad.enabled": "1" if enabled else "0",
        "ad.server": server.strip(),
        "ad.domain": domain.strip(),
        "ad.base_dn": base_dn.strip(),
        "ad.group": group.strip(),
    }
    for k, v in vals.items():
        await _pool.execute(
            "INSERT INTO app_settings (key, value) VALUES ($1,$2) "
            "ON CONFLICT (key) DO UPDATE SET value=$2",
            k, v,
        )
    return JSONResponse({"ok": True})


async def _ntp_config() -> dict:
    """Ortak NTP ayarı (app_settings): sunucu/port/zaman dilimi. Boş sunucu =
    sadece anlık saat eşitleme (NTP etkinleştirilmez)."""
    s = await _get_settings("ntp.")
    return {
        "server": s.get("ntp.server", "") or "",
        "port": _as_int(s.get("ntp.port")) or 123,
        "tz": _as_int(s.get("ntp.tz")) if s.get("ntp.tz") is not None else 3,
    }


@app.get("/api/settings/ntp")
async def get_ntp_settings(_: str = Depends(_auth)) -> dict:
    """Kayıtlı ortak NTP ayarı."""
    return await _ntp_config()


@app.post("/api/settings/ntp")
async def set_ntp_settings(
    _: str = Depends(_auth),
    server: str = Form(""),
    port: int = Form(123),
    tz: int = Form(3),
) -> JSONResponse:
    """Ortak NTP ayarını kaydeder (app_settings)."""
    vals = {"ntp.server": server.strip(), "ntp.port": str(int(port)), "ntp.tz": str(int(tz))}
    for k, v in vals.items():
        await _pool.execute(
            "INSERT INTO app_settings (key, value) VALUES ($1,$2) "
            "ON CONFLICT (key) DO UPDATE SET value=$2",
            k, v,
        )
    return JSONResponse({"ok": True})


@app.get("/api/settings/firmware")
async def get_firmware_reference(_: str = Depends(_auth)) -> dict:
    """Model → en son bilinen firmware referans tablosu."""
    return {"reference": await _firmware_reference()}


@app.post("/api/settings/firmware")
async def set_firmware_reference(
    _: str = Depends(_auth),
    reference: str = Form(...),
) -> JSONResponse:
    """Firmware referans tablosunu kaydeder (tek JSON değer). Girdi model→{latest,
    build, url, note} nesnesidir; geçerli JSON değilse reddedilir."""
    try:
        data = json.loads(reference)
        if not isinstance(data, dict):
            raise ValueError("nesne bekleniyor")
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"geçersiz JSON: {exc}"}, status_code=422)
    await _pool.execute(
        "INSERT INTO app_settings (key, value) VALUES ('firmware.reference',$1) "
        "ON CONFLICT (key) DO UPDATE SET value=$1",
        json.dumps(data, ensure_ascii=False),
    )
    return JSONResponse({"ok": True, "models": len(data)})


async def _sync_one_device_time(row, ntp: dict) -> dict:
    """Bir cihazın saatini DÜZELTİR: doğru yerel saati (UTC+tz) yazar; ortak NTP
    sunucusu tanımlıysa NTP'yi de etkinleştirir. Cihaz-yazma. before/after döner."""
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    tz = ntp.get("tz", 3) if ntp.get("tz") is not None else 3
    res = {"device": row["name"], "ok": False, "before": None, "after": None,
           "ntp": False}
    try:
        before = await driver.get_device_time()
        res["before"] = before.strftime("%Y-%m-%d %H:%M:%S") if before else None
        if before is None:
            # cihaz saati okunamıyor → gerçek NVR değil (ör. FortiGate) ya da
            # kimlik/erişim sorunu. Yazma denenmez.
            res["error"] = "cihaz saati okunamadı (NVR değil / erişilemiyor / kimlik)"
            return res
        local = datetime.utcnow() + timedelta(hours=tz)
        try:
            await driver.set_time(local)   # bazı firmware/NTP-açık cihazlar reddeder
        except Exception:
            pass
        if ntp.get("server"):
            try:
                res["ntp"] = await driver.set_ntp(
                    ntp["server"], port=ntp.get("port", 123), tz=tz
                )
            except Exception:
                res["ntp"] = False
        # Kameralar (bağlı IPC'ler) NVR'ın arkasında izole ağdadır ve saatlerini
        # NVR'dan otomatik alır (Dahua varsayılanı) — ayrı API çağrısı yok/gerekmez.
        after = await driver.get_device_time()
        res["after"] = after.strftime("%Y-%m-%d %H:%M:%S") if after else None
        # BAŞARI = saat gerçekten doğru (set reddedilse bile NTP/zaten-doğru olabilir);
        # firmware farkına bakmaksızın tek ölçüt gerçek saat.
        res["ok"] = after is not None and abs((after - local).total_seconds()) < 300
    except Exception as exc:
        res["error"] = str(exc)
    finally:
        await driver.close()
    return res


@app.post("/api/device/{cfg_id}/synctime")
async def device_synctime(cfg_id: int, _: str = Depends(_auth)) -> JSONResponse:
    """Tek cihazın saatini doğru zamana ayarlar (+ NTP tanımlıysa etkinleştirir)."""
    row = await _device_row(cfg_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "cihaz yok"}, status_code=404)
    res = await _sync_one_device_time(row, await _ntp_config())
    return JSONResponse(res)


@app.post("/api/synctime/all")
async def synctime_all(_: str = Depends(_auth)) -> JSONResponse:
    """TÜM erişilebilir cihazların saatini eşitler. Erişilemeyenler atlanır."""
    ntp = await _ntp_config()
    rows = await _pool.fetch(
        "SELECT name, host, port, username, password_enc, https, verify_tls "
        "FROM device_config WHERE enabled ORDER BY name"
    )
    results = []
    for row in rows:
        results.append(await _sync_one_device_time(row, ntp))
    ok = sum(1 for r in results if r["ok"])
    return JSONResponse({"ok": True, "synced": ok, "total": len(results), "results": results})


async def _alerting_cfg(reveal: bool = False) -> dict:
    """Bildirim yapılandırması (app_settings). Sırlar (tg_token, smtp_pass) Fernet
    ile şifreli saklanır; reveal=True yalnız gönderim/test içindir."""
    s = await _get_settings("alerting.")

    def dec(k: str) -> str:
        v = s.get(k)
        if not v:
            return ""
        try:
            return _fernet.decrypt(v.encode()).decode()
        except Exception:
            return ""

    return {
        "tg_enabled": s.get("alerting.tg_enabled") in ("1", "true", "on"),
        "tg_token": dec("alerting.tg_token") if reveal else "",
        "tg_token_set": bool(s.get("alerting.tg_token")),
        "tg_chat": s.get("alerting.tg_chat", "") or "",
        "mail_enabled": s.get("alerting.mail_enabled") in ("1", "true", "on"),
        "smtp_host": s.get("alerting.smtp_host", "") or "",
        "smtp_port": _as_int(s.get("alerting.smtp_port")) or 587,
        "smtp_tls": s.get("alerting.smtp_tls", "1") in ("1", "true", "on"),
        "smtp_user": s.get("alerting.smtp_user", "") or "",
        "smtp_pass": dec("alerting.smtp_pass") if reveal else "",
        "smtp_pass_set": bool(s.get("alerting.smtp_pass")),
        "mail_from": s.get("alerting.mail_from", "") or "",
        "mail_to": s.get("alerting.mail_to", "") or "",
        "min_severity": s.get("alerting.min_severity", "high") or "high",
    }


@app.get("/api/settings/alerting")
async def get_alerting_settings(_: str = Depends(_auth)) -> dict:
    """Kayıtlı bildirim yapılandırması (sırlar maskeli — yalnız 'set' bilgisi)."""
    return await _alerting_cfg(reveal=False)


@app.post("/api/settings/alerting")
async def set_alerting_settings(
    _: str = Depends(_auth),
    tg_enabled: bool = Form(False),
    tg_token: str = Form(""),
    tg_chat: str = Form(""),
    mail_enabled: bool = Form(False),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_tls: bool = Form(True),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    mail_from: str = Form(""),
    mail_to: str = Form(""),
    min_severity: str = Form("high"),
) -> JSONResponse:
    """Bildirim yapılandırmasını kaydeder. Sırlar (token/parola) yalnız yeni değer
    verilirse güncellenir; boş bırakılırsa mevcut şifreli değer korunur."""
    vals = {
        "alerting.tg_enabled": "1" if tg_enabled else "0",
        "alerting.tg_chat": tg_chat.strip(),
        "alerting.mail_enabled": "1" if mail_enabled else "0",
        "alerting.smtp_host": smtp_host.strip(),
        "alerting.smtp_port": str(int(smtp_port)),
        "alerting.smtp_tls": "1" if smtp_tls else "0",
        "alerting.smtp_user": smtp_user.strip(),
        "alerting.mail_from": mail_from.strip(),
        "alerting.mail_to": mail_to.strip(),
        "alerting.min_severity": (min_severity.strip() or "high"),
    }
    if tg_token.strip():
        vals["alerting.tg_token"] = _fernet.encrypt(tg_token.strip().encode()).decode()
    if smtp_pass:
        vals["alerting.smtp_pass"] = _fernet.encrypt(smtp_pass.encode()).decode()
    for k, v in vals.items():
        await _pool.execute(
            "INSERT INTO app_settings (key, value) VALUES ($1,$2) "
            "ON CONFLICT (key) DO UPDATE SET value=$2",
            k, v,
        )
    return JSONResponse({"ok": True})


@app.post("/api/settings/alerting/test")
async def test_alerting(_: str = Depends(_auth)) -> JSONResponse:
    """Kayıtlı kanallara bir test bildirimi gönderir (panel doğrudan gönderir)."""
    cfg = await _alerting_cfg(reveal=True)
    subject = "[Nöbetçi] Test bildirimi"
    body = "Bu bir test mesajıdır — bildirim kanalı çalışıyor. Kritik alarmlar buraya düşecek."
    results: dict = {}
    if cfg["tg_enabled"] and cfg["tg_token"] and cfg["tg_chat"]:
        try:
            await TelegramNotifier(cfg["tg_token"], cfg["tg_chat"]).send(subject, body)
            results["telegram"] = {"ok": True}
        except Exception as exc:
            results["telegram"] = {"ok": False, "error": str(exc)[:200]}
    if cfg["mail_enabled"] and cfg["smtp_host"] and cfg["mail_to"]:
        ecfg = SimpleNamespace(
            smtp_host=cfg["smtp_host"], smtp_port=cfg["smtp_port"],
            starttls=cfg["smtp_tls"], username=cfg["smtp_user"],
            password=cfg["smtp_pass"], from_addr=cfg["mail_from"] or cfg["smtp_user"],
            to=[x.strip() for x in cfg["mail_to"].split(",") if x.strip()],
        )
        try:
            await EmailNotifier(ecfg).send(subject, body)
            results["email"] = {"ok": True}
        except Exception as exc:
            results["email"] = {"ok": False, "error": str(exc)[:200]}
    if not results:
        return JSONResponse({"ok": False, "error": "Etkin ve kayıtlı bir kanal yok"})
    return JSONResponse({"ok": True, "results": results})


@app.post("/api/settings/ad/test")
async def test_ad_settings(
    _: str = Depends(_auth),
    username: str = Form(...),
    password: str = Form(...),
    server: str = Form(""),
    domain: str = Form(""),
    base_dn: str = Form(""),
    group: str = Form(""),
) -> JSONResponse:
    """Verilen (henüz kaydedilmemiş olabilen) AD ayarlarını ve bir test hesabını
    dener; bind + grup sonucunu döndürür."""
    cur = await _ad_config()
    cfg = {
        "server": server.strip() or cur["server"],
        "domain": domain.strip() or cur["domain"],
        "base_dn": base_dn.strip() or cur["base_dn"],
        "group": group.strip() or cur["group"],
    }
    if not cfg["server"] or not cfg["domain"]:
        return JSONResponse({"ok": False, "error": "Sunucu ve domain gerekli"})
    ok = await asyncio.to_thread(_ad_authenticate, username.strip(), password, cfg)
    return JSONResponse({"ok": ok})


@app.post("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


def _login_html(error: bool = False) -> str:
    err = (
        '<div class="err">Parola hatalı — tekrar deneyin.</div>' if error else ""
    )
    return (
        """<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nöbetçi · Giriş</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{--bg:#0c121a;--surface:#141c27;--field:#0f1722;--ink:#e8eef6;--muted:#94a4b6;--faint:#6b7c8f;--hairline:#26313f;--accent:#5ea0dd;--accent2:#42c3d4;--accent-soft:rgba(94,160,221,.18);--ok:#33c081;--crit:#e5706b;--glow:rgba(94,160,221,.14)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;background:var(--bg);background-image:radial-gradient(900px 520px at 50% -140px,var(--glow),transparent 70%);color:var(--ink);font-family:"IBM Plex Sans",system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
.card{width:min(400px,100%);background:var(--surface);border:1px solid var(--hairline);border-radius:20px;box-shadow:inset 0 1px 0 rgba(255,255,255,.045),0 24px 64px rgba(0,0,0,.55);padding:34px 32px;animation:in .42s cubic-bezier(.16,.8,.3,1)}
@keyframes in{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:5px}
.logo{width:40px;height:40px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:21px;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 8px 20px rgba(94,160,221,.4)}
.brand b{font-size:21px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13.5px;margin:3px 0 26px}
label{display:block;font-size:11.5px;font-weight:600;color:var(--faint);text-transform:uppercase;letter-spacing:.07em;margin-bottom:8px}
input{width:100%;background:var(--field);border:1px solid var(--hairline);border-radius:11px;padding:13px 15px;color:var(--ink);font:inherit;font-size:15px;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
button{width:100%;margin-top:18px;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#06131f;border:0;border-radius:11px;padding:13px;font:inherit;font-size:15px;font-weight:700;cursor:pointer;transition:transform .08s,filter .15s}
button:hover{filter:brightness(1.07)} button:active{transform:translateY(1px)}
.err{background:color-mix(in srgb,var(--crit) 15%,transparent);border:1px solid var(--crit);color:var(--crit);font-size:13px;border-radius:10px;padding:10px 13px;margin-bottom:16px}
.foot{margin-top:22px;text-align:center;color:var(--faint);font-size:11px;font-family:"IBM Plex Mono",monospace;letter-spacing:.03em}
@media (prefers-reduced-motion:reduce){.card{animation:none}}
</style></head><body>
<form class="card" method="post" action="/login">
  <div class="brand"><span class="logo">🖥️</span><b>Nöbetçi</b></div>
  <div class="sub">Dahua NVR filo izleme · kontrol merkezi</div>
  """
        + err
        + """<label for="u">Kullanıcı</label>
  <input id="u" name="username" type="text" autofocus autocomplete="username" placeholder="domain hesabı ya da admin" style="margin-bottom:16px">
  <label for="p">Parola</label>
  <input id="p" name="password" type="password" autocomplete="current-password" placeholder="••••••••">
  <button type="submit">Giriş yap</button>
  <div class="foot">domain (AD) ya da yerel hesap · yalnızca yetkili erişim</div>
</form></body></html>"""
    )


# ------------------------------------------------------------------ JSON API


def _f(v) -> float | None:
    return None if v is None else float(v)


def _smart_summary(row) -> dict | None:
    """disk_smart aggregate satırını API özetine çevirir."""
    if row is None:
        return None
    return {
        "total": row["total"],
        "ok": row["ok"],
        "warn": row["warn"],
        "crit": row["crit"],
        "max_temp": row["max_temp"],
        "min_poh": row["min_poh"],
        "predict": bool(row["predict"]),
    }


def _camera_out(row) -> dict | None:
    """camera_state satırını API modeline çevirir (kopuk kamera listesi dahil)."""
    if row is None:
        return None
    ol = row["offline_list"]
    if isinstance(ol, str):
        try:
            ol = json.loads(ol)
        except ValueError:
            ol = []
    return {
        "total": row["total"],
        "online": row["online"],
        "offline": row["offline"],
        "offline_list": ol or [],
    }


def _raid_out(x) -> dict:
    """raid_metrics satırını API modeline çevirir; RPC2 ile zenginleştirilmiş
    fiziksel disk sayıları (working/total/failed/spare) ve üye disk adları raw
    JSONB'nin 'rpc2' anahtarından çıkarılır (yoksa None kalır)."""
    raw = x["raw"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    rpc2 = (raw or {}).get("rpc2") or {}
    return {
        "name": x["raid_name"],
        "level": x["level"],
        "level_num": rpc2.get("level_num"),
        "state": x["state"],
        "rebuild_pct": _f(x["rebuild_pct"]),
        "working": rpc2.get("working_devices"),
        "active": rpc2.get("active_devices"),
        "total": rpc2.get("total_devices"),
        "failed": rpc2.get("failed_devices"),
        "spare": rpc2.get("spare_devices"),
        "members": list(rpc2.get("members") or []),
        "members_detail": list(rpc2.get("members_detail") or []),
    }


def _device_caps(disks, raids, smart) -> dict:
    """Cihazın türetilmiş yetenek profili: sistemin bu modele/firmware'e göre
    hangi yollarla veri aldığı (görünürlük + ops). Runtime sürücü zaten uyum
    sağlıyor; bu profil onu şeffaf kılar."""
    has_raid = bool(raids)
    smart_n = int((smart and smart["total"]) or 0)
    if has_raid:
        smart_src = "RAID üyesi · getSubSmartInfos" if smart_n else "—"
    elif smart_n:
        smart_src = "Tek disk · devStorage.getSmartValue"
    else:
        smart_src = "—"
    return {
        "disk_count": len(disks),
        "has_raid": has_raid,
        "smart_disks": smart_n,
        "smart_source": smart_src,
    }


async def _build_overview() -> list:
    """Tüm cihazların tam durumunu üretir (panel /api/overview ve /api/v1 paylaşır)."""
    fw_ref = await _firmware_reference()
    async with _pool.acquire() as conn:
        devices = await conn.fetch(
            """
            SELECT coalesce(d.name, n.name) AS name,
                   d.id AS cfg_id, d.host AS cfg_host, d.port, d.enabled,
                   d.https, d.username, d.min_retention_days, d.overwrite_recording,
                   n.id AS nvr_id, n.host AS seen_host, n.device_type,
                   n.software_version, n.serial, n.last_seen
            -- Yalnızca yönetilen (device_config'teki) cihazlar; panelden silinen
            -- cihaz, nvr satırı henüz temizlenmemiş olsa bile listeden hemen düşer.
            FROM device_config d
            LEFT JOIN nvr n ON n.name = d.name
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
                      state, rebuild_pct, raw
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
        cameras = {
            r["nvr_id"]: r
            for r in await conn.fetch(
                "SELECT nvr_id, total, online, offline, offline_list FROM camera_state"
            )
        }
        smart = {
            r["nvr_id"]: r
            for r in await conn.fetch(
                """SELECT nvr_id, count(*) AS total,
                          count(*) FILTER (WHERE health='ok') AS ok,
                          count(*) FILTER (WHERE health='warn') AS warn,
                          count(*) FILTER (WHERE health='crit') AS crit,
                          max(temperature_c) AS max_temp,
                          min(power_on_hours) AS min_poh,
                          bool_or(predict) AS predict
                   FROM disk_smart GROUP BY nvr_id"""
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
                "https": d["https"],
                "username": d["username"],
                "enabled": d["enabled"],
                "managed": d["cfg_id"] is not None,
                "model": d["device_type"] or "",
                "fw": d["software_version"] or "",
                "serial": d["serial"] or "",
                "update": _firmware_update(
                    d["device_type"] or "", d["software_version"] or "", fw_ref
                ),
                "caps": _device_caps(dks, raids.get(nid, []), smart.get(nid)),
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
                "cameras": _camera_out(cameras.get(nid)),
                "smart": _smart_summary(smart.get(nid)),
                "fill_pct": (100.0 * used / total) if total else None,
                "total_bytes": total or None,
                "used_bytes": used if total else None,
                "disks": [
                    {
                        "name": x["disk_name"],
                        "state": x["state"],
                        "total_bytes": x["total_bytes"],
                        "used_bytes": x["used_bytes"],
                        "fill_pct": (100.0 * x["used_bytes"] / x["total_bytes"])
                        if x["total_bytes"]
                        else None,
                        "health_ok": x["health_ok"],
                    }
                    for x in dks
                ],
                "raids": [_raid_out(x) for x in raids.get(nid, [])],
                "fill_hist": fill_hist.get(nid, []),
                "ret_hist": ret_hist.get(nid, []),
            }
        )
    return out


@app.get("/api/overview")
async def overview(_: str = Depends(_auth)) -> dict:
    return {"devices": await _build_overview()}


def _prom_label(v) -> str:
    return str(v or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


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


@app.get("/api/events")
async def events(
    _: str = Depends(_auth),
    status: str = "open",
    severity: str = "all",
    limit: int = 200,
) -> dict:
    """Olay/alarm geçmişi (alarm panosu). status: open|all, severity: all|high|
    critical. 'open' = kapatılmamış ve bilgi düzeyi olmayan (Recovered hariç)."""
    conds = ["e.source <> 'event-stream'"]  # ham telemetri değil
    if status == "open":
        conds.append("e.acked_by IS NULL")
        conds.append("e.severity <> 'info'")
    if severity == "critical":
        conds.append("e.severity = 'critical'")
    elif severity == "high":
        conds.append("e.severity IN ('high','critical')")
    where = " AND ".join(conds)
    limit = max(1, min(int(limit), 1000))
    rows = await _pool.fetch(
        f"""
        SELECT e.id, e.ts, e.code, e.severity, e.source,
               e.payload->>'message' AS message, n.name AS device, e.acked_by
        FROM event e LEFT JOIN nvr n ON n.id = e.nvr_id
        WHERE {where}
        ORDER BY e.ts DESC LIMIT {limit}
        """
    )
    open_count = await _pool.fetchval(
        "SELECT count(*) FROM event WHERE acked_by IS NULL "
        "AND severity <> 'info' AND source <> 'event-stream'"
    )
    return {
        "open_count": open_count or 0,
        "events": [
            {
                "id": r["id"],
                "ts": r["ts"].isoformat(sep=" ", timespec="seconds"),
                "code": r["code"],
                "severity": r["severity"],
                "source": r["source"],
                "message": r["message"] or "",
                "device": r["device"] or "-",
                "acked_by": r["acked_by"],
            }
            for r in rows
        ],
    }


@app.post("/api/alarms/{event_id}/ack")
async def ack_alarm(event_id: int, user: str = Depends(_auth)) -> dict:
    await _pool.execute(
        "UPDATE event SET acked_by=$2 WHERE id=$1 AND acked_by IS NULL",
        event_id,
        user,
    )
    return {"ok": True}


@app.get("/api/channels/{cfg_id}")
async def channels(cfg_id: int, _: str = Depends(_auth)) -> JSONResponse:
    """Bir cihazın kamera/kanal adlarını canlı çeker (detay modali açılınca).

    Kanal adları nadiren değiştiğinden DB'de tutulmaz; istendiğinde cihazdan
    okunur. Parola panelde Fernet ile çözülür, log'a yazılmaz."""
    row = await _pool.fetchrow(
        "SELECT name, host, port, username, password_enc, https, verify_tls "
        "FROM device_config WHERE id=$1",
        cfg_id,
    )
    if row is None:
        return JSONResponse({"error": "cihaz bulunamadı", "channels": []}, status_code=404)
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    try:
        names = await driver.get_channel_names()
        try:
            enc = await driver.get_encode_settings()
        except Exception:
            enc = {}  # kodlama alınamazsa yalnız adlar döner
    except Exception as exc:  # cihaz erişilemez/parola vb. — izlemeyi etkilemez
        return JSONResponse({"error": str(exc), "channels": []})
    finally:
        await driver.close()
    # Kopuk kanallar: collector'ın camera_state anlık görüntüsünden (≤2 dk taze)
    offline: set[int] = set()
    cs = await _pool.fetchrow(
        "SELECT cs.offline_list FROM camera_state cs "
        "JOIN nvr n ON n.id=cs.nvr_id WHERE n.name=$1",
        row["name"],
    )
    if cs and cs["offline_list"]:
        ol = cs["offline_list"]
        if isinstance(ol, str):
            try:
                ol = json.loads(ol)
            except ValueError:
                ol = []
        offline = {
            int(x["channel"]) for x in ol if x.get("channel") is not None
        }
    for c in names:
        e = enc.get(c["idx"] - 1, {})  # encode 0 tabanlı, kanal adı 1 tabanlı
        c["codec"] = e.get("codec")
        c["bitrate"] = e.get("bitrate")
        c["bitrate_control"] = e.get("bitrate_control")
        c["fps"] = e.get("fps")
        c["resolution"] = e.get("resolution")
        c["online"] = c["idx"] not in offline
    return JSONResponse({"channels": names})


@app.get("/api/smart")
async def disk_smart_fleet(_: str = Depends(_auth)) -> dict:
    """Filo geneli tüm fiziksel disklerin özeti (sıcaklık ısı-haritası için)."""
    rows = await _pool.fetch(
        """SELECT n.name AS device, ds.disk_name, ds.health, ds.temperature_c,
                  ds.power_on_hours, ds.reallocated, ds.pending, ds.uncorrectable,
                  ds.predict
           FROM disk_smart ds JOIN nvr n ON n.id=ds.nvr_id
           ORDER BY n.name, ds.disk_name"""
    )
    return {"disks": [dict(r) for r in rows]}


async def _smart_trend(nvr_id: int, disk_name: str) -> dict:
    """Disk SMART geçmişinden öngörücü trend: kritik sayaçların (reallocated/pending)
    zamana göre artış hızı (en küçük kareler eğimi, birim/gün) + risk verdisi.
    Yeterli geçmiş yoksa 'collecting' döner (veri birikiyor)."""
    rows = await _pool.fetch(
        """SELECT ts, reallocated, pending, temperature_c
           FROM disk_smart_history
           WHERE nvr_id=$1 AND disk_name=$2 AND ts > now() - interval '60 days'
           ORDER BY ts""",
        nvr_id, disk_name,
    )
    n = len(rows)
    if n < 4:
        return {"status": "collecting", "points": n}
    t0 = rows[0]["ts"]
    xs = [(r["ts"] - t0).total_seconds() / 86400.0 for r in rows]
    span = xs[-1]
    if span < 0.5:  # yarım günden az — trend anlamsız
        return {"status": "collecting", "points": n}

    def slope(field: str) -> float:
        ys = [float(r[field] or 0) for r in rows]
        m = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        den = m * sxx - sx * sx
        return (m * sxy - sx * sy) / den if den else 0.0

    re_s, pe_s, tmp_s = slope("reallocated"), slope("pending"), slope("temperature_c")
    cur_re = rows[-1]["reallocated"] or 0
    cur_pe = rows[-1]["pending"] or 0
    if cur_pe > 0 or pe_s > 0.01:
        verdict, note = "crit", "bekleyen sektör var/artıyor — arıza riski yüksek"
    elif re_s > 0.05:
        verdict, note = "warn", f"yeniden atanan sektör artıyor (~{re_s:.1f}/gün)"
    elif cur_re > 0:
        verdict, note = "watch", "yeniden atanan sektör var ama stabil"
    else:
        verdict, note = "ok", "stabil — bozulma eğilimi yok"
    return {
        "status": "ok", "points": n, "span_days": round(span, 1),
        "reallocated_slope": round(re_s, 3), "pending_slope": round(pe_s, 3),
        "temp_slope": round(tmp_s, 3), "verdict": verdict, "note": note,
    }


@app.get("/api/smart/{cfg_id}")
async def disk_smart_detail(cfg_id: int, _: str = Depends(_auth)) -> dict:
    """Bir cihazın fiziksel disklerinin SMART detayı + öngörücü trend (detay modali)."""
    rows = await _pool.fetch(
        """SELECT n.id AS nvr_id, ds.disk_name, ds.health, ds.temperature_c,
                  ds.power_on_hours, ds.reallocated, ds.pending, ds.uncorrectable,
                  ds.predict, ds.attrs
           FROM disk_smart ds JOIN nvr n ON n.id=ds.nvr_id
           JOIN device_config d ON d.name=n.name
           WHERE d.id=$1 ORDER BY ds.disk_name""",
        cfg_id,
    )
    out = []
    for r in rows:
        attrs = r["attrs"]
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = []
        out.append(
            {
                "disk": r["disk_name"], "health": r["health"],
                "temperature_c": r["temperature_c"],
                "power_on_hours": r["power_on_hours"],
                "reallocated": r["reallocated"], "pending": r["pending"],
                "uncorrectable": r["uncorrectable"], "predict": r["predict"],
                "attrs": attrs or [],
                "trend": await _smart_trend(r["nvr_id"], r["disk_name"]),
            }
        )
    return {"disks": out}


@app.get("/api/snapshot/{cfg_id}/{channel}")
async def snapshot(cfg_id: int, channel: int, _: str = Depends(_auth)) -> Response:
    """Bir kanaldan anlık JPEG kare (canlı kamera görüntüsü). <img> ile çekilir.
    Cihazdan snapshot.cgi ile alınır; parola panelde çözülür, loglanmaz."""
    row = await _pool.fetchrow(
        "SELECT host, port, username, password_enc, https, verify_tls "
        "FROM device_config WHERE id=$1",
        cfg_id,
    )
    if row is None:
        return Response(status_code=404)
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    try:
        img = await driver.get_snapshot(channel)
    except Exception:  # kamera kapalı/erişilemez -> 502, UI 'görüntü yok' gösterir
        return Response(status_code=502)
    finally:
        await driver.close()
    return Response(
        content=img, media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/users/{cfg_id}")
async def active_users(cfg_id: int, _: str = Depends(_auth)) -> JSONResponse:
    """Bir cihaza O AN bağlı (açık oturum) kullanıcıları canlı çeker.

    userManager.cgi getActiveUserInfoAll üzerinden; her istekte anlık durum
    okunur (DB'de tutulmaz). Parola Fernet ile çözülür, loglanmaz."""
    row = await _pool.fetchrow(
        "SELECT host, port, username, password_enc, https, verify_tls "
        "FROM device_config WHERE id=$1",
        cfg_id,
    )
    if row is None:
        return JSONResponse({"error": "cihaz bulunamadı", "users": []}, status_code=404)
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    try:
        raw = await driver.get_active_users()
    except Exception as exc:
        return JSONResponse({"error": str(exc), "users": []})
    finally:
        await driver.close()
    users = [
        {
            "ip": u.get("ClientAddress") or "—",
            "user": u.get("Name") or "?",
            "group": u.get("Group") or "",
            "type": u.get("ClientType") or "",
            "since": u.get("LoginTime") or "",
        }
        for u in raw
    ]
    return JSONResponse({"users": users})


# ------------------------------------------------------- Uzaktan işlem (yazma)


async def _device_row(cfg_id: int):
    return await _pool.fetchrow(
        "SELECT name, host, port, username, password_enc, https, verify_tls "
        "FROM device_config WHERE id=$1",
        cfg_id,
    )


@app.post("/api/device/{cfg_id}/reboot")
async def device_reboot(cfg_id: int, _: str = Depends(_auth)) -> JSONResponse:
    """Cihazı uzaktan yeniden başlatır. GERİ ALINAMAZ; UI çift onay alır."""
    row = await _device_row(cfg_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "cihaz yok"}, status_code=404)
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    try:
        await driver.reboot()
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    finally:
        await driver.close()
    return JSONResponse({"ok": True, "device": row["name"]})


_FACTORY_RISKY = {"888888", "666666", "default", "guest", "test", "admin888"}


@app.get("/api/device/{cfg_id}/accounts")
async def device_accounts(cfg_id: int, _: str = Depends(_auth)) -> JSONResponse:
    """Cihazdaki tanımlı hesaplar + güvenlik denetimi (fabrika/riskli hesap,
    yönetici sayısı). Salt-okunur — parola denetlenmez (lockout riski)."""
    row = await _device_row(cfg_id)
    if row is None:
        return JSONResponse({"accounts": [], "flags": []}, status_code=404)
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    try:
        users = await driver.get_users()
    except Exception as exc:
        return JSONResponse({"error": str(exc), "accounts": [], "flags": []})
    finally:
        await driver.close()
    admins = [u for u in users if str(u.get("group", "")).lower() == "admin"]
    risky = [u["name"] for u in users if str(u.get("name", "")).lower() in _FACTORY_RISKY]
    flags = []
    if risky:
        flags.append({"level": "crit", "text": "Fabrika/riskli hesap açık: " + ", ".join(risky)})
    if len(admins) > 2:
        flags.append({"level": "warn", "text": f"{len(admins)} yönetici hesabı — gerekli mi?"})
    if not flags:
        flags.append({"level": "ok", "text": "Fabrika/default hesap yok"})
    return JSONResponse({"accounts": users, "admins": len(admins), "flags": flags})


@app.get("/api/device/{cfg_id}/camdiag")
async def device_camdiag(cfg_id: int, _: str = Depends(_auth)) -> JSONResponse:
    """Kopuk kameraların giriş hata kodu (neden çevrimdışı — salt-okunur tanı)."""
    row = await _device_row(cfg_id)
    if row is None:
        return JSONResponse({"error": "cihaz yok", "diag": []}, status_code=404)
    cs = await _pool.fetchrow(
        "SELECT cs.offline_list FROM camera_state cs JOIN nvr n ON n.id=cs.nvr_id "
        "WHERE n.name=$1",
        row["name"],
    )
    ol = (cs and cs["offline_list"]) or "[]"
    if isinstance(ol, str):
        try:
            ol = json.loads(ol)
        except ValueError:
            ol = []
    if not ol:
        return JSONResponse({"diag": []})
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    rpc2 = Rpc2Client(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    chans = [int(x["channel"]) - 1 for x in ol if x.get("channel") is not None]
    try:
        errs = await rpc2.get_camera_login_errors(chans)
    except Exception:
        errs = {}
    finally:
        await rpc2.close()
    diag = [
        {
            "channel": x["channel"], "name": x.get("name", ""),
            "state": x.get("state", ""),
            "error_code": errs.get(int(x["channel"]) - 1),
        }
        for x in ol if x.get("channel") is not None
    ]
    return JSONResponse({"diag": diag})


@app.get("/api/device/{cfg_id}/adconfig")
async def device_ad_get(cfg_id: int, _: str = Depends(_auth)) -> JSONResponse:
    """NVR'ın KENDİ Active Directory ayarını okur (cihaz domain'e üye mi).
    Salt-okunur."""
    row = await _device_row(cfg_id)
    if row is None:
        return JSONResponse({"error": "cihaz yok"}, status_code=404)
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    port = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{port}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    try:
        cfg = await driver.get_ad_config()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    finally:
        await driver.close()
    if not cfg:
        return JSONResponse({"error": "cihaz AD ayarını desteklemiyor"})
    cfg["supported"] = True
    return JSONResponse(cfg)


@app.post("/api/device/{cfg_id}/adconfig")
async def device_ad_set(
    cfg_id: int,
    _: str = Depends(_auth),
    enable: bool = Form(False),
    server: str = Form(""),
    port: int = Form(389),
    base_dn: str = Form(""),
    filter: str = Form(""),
) -> JSONResponse:
    """NVR'ın Active Directory ayarını YAZAR (cihazı domain'e üye yapar). Bu bir
    cihaz-yazma işlemidir; UI onay alır. Yanlış ayar cihaz girişini etkileyebilir
    ama yerel admin her zaman geçerli kalır."""
    row = await _device_row(cfg_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "cihaz yok"}, status_code=404)
    if enable and (not server.strip() or server.strip() == "0.0.0.0"):
        return JSONResponse(
            {"ok": False, "error": "Etkinleştirmek için geçerli sunucu adresi gerekli"},
            status_code=400,
        )
    pw = _fernet.decrypt(row["password_enc"].encode()).decode()
    scheme = "https" if row["https"] else "http"
    dport = row["port"] or (443 if row["https"] else 80)
    driver = CgiDriver(
        f"{scheme}://{row['host']}:{dport}", row["username"], pw,
        verify_tls=row["verify_tls"],
    )
    try:
        ok = await driver.set_ad_config(
            enable, server.strip(), port, base_dn.strip(), filter.strip()
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    finally:
        await driver.close()
    return JSONResponse({"ok": ok, "device": row["name"]})


# ------------------------------------------------------- Subnet tarama (toplu)


async def _tcp_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        _, writer = await asyncio.wait_for(fut, timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _parse_hosts(spec: str) -> list[str]:
    """CIDR (10.20.56.0/24) ya da son-oktet aralığı (10.20.56.1-254) → IP listesi."""
    spec = spec.strip()
    if "/" in spec:
        net = ipaddress.ip_network(spec, strict=False)
        return [str(h) for h in net.hosts()]
    if "-" in spec:
        base, _, last = spec.rpartition("-")
        prefix, _, first = base.strip().rpartition(".")
        return [f"{prefix}.{i}" for i in range(int(first), int(last.strip()) + 1)]
    ipaddress.ip_address(spec)  # doğrula
    return [spec]


@app.post("/api/scan")
async def scan_subnet(
    _: str = Depends(_auth),
    cidr: str = Form(...),
    username: str = Form("admin"),
    password: str = Form(...),
) -> JSONResponse:
    """Bir IP aralığını tarar: önce açık web portu (80/443/37777) olan adayları
    bulur, sonra o adaylarda cihaz kimliğini yoklar (host başına tek deneme —
    lockout-güvenli) ve bulunan Dahua NVR'ları döndürür."""
    try:
        hosts = _parse_hosts(cidr)
    except (ValueError, IndexError):
        return JSONResponse(
            {"error": "Geçersiz aralık (CIDR '10.20.56.0/24' ya da '10.20.56.1-254')"},
            status_code=400,
        )
    if not hosts or len(hosts) > 512:
        return JSONResponse({"error": "Aralık en fazla 512 IP olmalı"}, status_code=400)
    sem = asyncio.Semaphore(64)

    async def candidate(h: str):
        async with sem:
            for p in (80, 443, 37777):
                if await _tcp_open(h, p):
                    return h
        return None

    live = [h for h in await asyncio.gather(*[candidate(h) for h in hosts]) if h]
    existing = {r["host"] for r in await _pool.fetch("SELECT host FROM device_config")}
    psem = asyncio.Semaphore(8)

    async def probe(h: str):
        async with psem:
            try:
                info = await probe_device(h, username.strip(), password, timeout_s=6.0)
            except Exception:
                info = {"found": False}
        if info.get("found"):
            return {
                "host": h, "https": info.get("https"), "port": info.get("port"),
                "model": info.get("device_type") or "",
                "serial": info.get("serial") or "",
                "channels": info.get("channels"), "existing": h in existing,
            }
        return None

    found = [d for d in await asyncio.gather(*[probe(h) for h in live]) if d]
    return JSONResponse({"scanned": len(hosts), "live": len(live), "found": found})


# ------------------------------------------------------- Cihaz yönetimi (form)


@app.post("/devices/probe")
async def probe(
    _: str = Depends(_auth),
    host: str = Form(...),
    username: str = Form("monitor"),
    password: str = Form(...),
) -> JSONResponse:
    """IP + kimlikle cihazı yoklar; http/https, port, model ve kanal sayısını
    otomatik bulur ('Tespit et'). Yeni cihaz eklemeden önce formu doldurmak için."""
    result = await probe_device(host.strip(), username.strip(), password)
    return JSONResponse(result)


@app.post("/devices")
async def upsert_device(
    _: str = Depends(_auth),
    name: str = Form(...),
    host: str = Form(...),
    username: str = Form("monitor"),
    password: str = Form(""),           # opsiyonel — düzenlemede boş = mevcut parola korunur
    port: int | None = Form(None),
    https: bool = Form(False),
    overwrite: bool = Form(False),
    min_retention_days: int | None = Form(None),
    max_channels: int = Form(32),
    rpc2: bool = Form(True),
    autodetect: bool = Form(True),
    cfg_id: str | None = Form(None),
) -> JSONResponse:
    """Cihaz ekler (cfg_id yok) ya da düzenler (cfg_id var). JSON döner:
    {ok, name, edited, warning} veya {ok:false, error}. Salt isim değişikliğinde
    (parola boş) cihaza HİÇ bağlanılmaz — hem gereksiz gecikme hem yanlış parola
    lockout riski önlenir. Düzenlemede parola boş bırakılırsa mevcut parola korunur."""
    edit_id = int(cfg_id) if cfg_id and cfg_id.strip().isdigit() else None
    new_name = name.strip()
    host = host.strip()
    if not new_name or not host:
        return JSONResponse({"ok": False, "error": "Ad ve IP/host zorunlu"}, status_code=400)

    old = None
    if edit_id is not None:
        old = await _pool.fetchrow(
            "SELECT name, host, password_enc FROM device_config WHERE id=$1", edit_id
        )
        if old is None:
            return JSONResponse(
                {"ok": False, "error": "Cihaz bulunamadı (liste yenilenmiş olabilir)"},
                status_code=404,
            )
        clash = await _pool.fetchrow(
            "SELECT 1 FROM device_config WHERE name=$1 AND id<>$2", new_name, edit_id
        )
        if clash:
            return JSONResponse(
                {"ok": False, "error": f"'{new_name}' adı başka bir cihazda kullanılıyor"},
                status_code=409,
            )
    else:
        if not password:
            return JSONResponse(
                {"ok": False, "error": "Yeni cihaz için parola gerekli"}, status_code=400
            )
        exists = await _pool.fetchrow("SELECT 1 FROM device_config WHERE name=$1", new_name)
        if exists:
            return JSONResponse(
                {"ok": False, "error": f"'{new_name}' adında bir cihaz zaten var"},
                status_code=409,
            )

    # Otomatik tespit YALNIZCA elde düz parola varken ve (yeni ekleme VEYA host
    # değişmiş) ise. Salt isim değişikliğinde cihaza bağlanmayız.
    warning = None
    host_changed = bool(old) and old["host"] != host
    if autodetect and password and (edit_id is None or host_changed):
        try:
            info = await probe_device(
                host, username.strip(), password,
                prefer=(https, port or (443 if https else 80)),
                timeout_s=6.0,
            )
        except Exception:
            info = {"found": False, "reason": "unreachable"}
        if info.get("found"):
            https = bool(info["https"])
            port = info.get("port") or port
            if info.get("channels"):
                max_channels = int(info["channels"])
        else:
            reason = info.get("reason")
            # YENİ eklemede yanlış cihaz / kimlik reddini REDDET: hatalı kayıt
            # oluşturma (FortiGate vb.) ve yanlış parolayla tekrar deneyip cihazı
            # kilitleme riskini önler. Düzenlemede (cihaz zaten var) yalnız uyarı.
            if edit_id is None and reason in ("auth", "not_dahua"):
                msg = info.get("error") or "cihaz doğrulanamadı"
                return JSONResponse({"ok": False, "error": msg}, status_code=422)
            warning = info.get("error") or "cihaza ulaşılamadı — girilen değerlerle kaydedildi"

    # parola: düzenlemede boş bırakıldıysa mevcut şifreli parola korunur
    if edit_id is not None and not password:
        enc = old["password_enc"]
    else:
        enc = _fernet.encrypt(password.encode()).decode()

    if edit_id is not None:
        await _pool.execute(
            """
            UPDATE device_config SET
                name=$1, host=$2, port=$3, username=$4, password_enc=$5, https=$6,
                overwrite_recording=$7, min_retention_days=$8, max_channels=$9,
                rpc2=$10, updated_at=now()
            WHERE id=$11
            """,
            new_name, host, port, username.strip(), enc, https,
            overwrite, min_retention_days, max_channels, rpc2, edit_id,
        )
        # ad değiştiyse bağlı nvr satırını taşı (metrikler/olaylar korunur). Yeni
        # adda hayalet nvr varsa (device_config yok) önce onu temizle, sonra taşı.
        if old["name"] != new_name:
            try:
                await _pool.execute("DELETE FROM nvr WHERE name=$1", new_name)
                await _pool.execute(
                    "UPDATE nvr SET name=$1 WHERE name=$2", new_name, old["name"]
                )
            except Exception:
                pass
        return JSONResponse(
            {"ok": True, "name": new_name, "edited": True, "warning": warning}
        )

    await _pool.execute(
        """
        INSERT INTO device_config
            (name, host, port, username, password_enc, https,
             overwrite_recording, min_retention_days, max_channels, rpc2)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """,
        new_name, host, port, username.strip(), enc, https,
        overwrite, min_retention_days, max_channels, rpc2,
    )
    return JSONResponse(
        {"ok": True, "name": new_name, "edited": False, "warning": warning}
    )


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


# ================================================================= Genel API
# Programatik erişim: X-API-Key başlığı (ya da Bearer) ile korunan, versiyonlu,
# salt-okunur /api/v1 uçları. Gelecekteki entegrasyonlar bunu kullanır.
# Otomatik dokümantasyon: /docs (Swagger) ve /openapi.json.


async def _api_auth(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> str:
    """API anahtarı doğrulama (X-API-Key ya da 'Bearer <key>'). Panel'in Basic
    auth'undan ayrıdır; anahtar sha256 karması ile saklanır, düz metin tutulmaz."""
    key = x_api_key
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    if not key:
        raise HTTPException(status_code=401, detail="API anahtarı gerekli (X-API-Key başlığı)")
    h = hashlib.sha256(key.encode()).hexdigest()
    row = await _pool.fetchrow(
        "SELECT id, name FROM api_key WHERE key_hash=$1 AND enabled", h
    )
    if row is None:
        raise HTTPException(status_code=403, detail="Geçersiz ya da iptal edilmiş API anahtarı")
    await _pool.execute("UPDATE api_key SET last_used=now() WHERE id=$1", row["id"])
    return row["name"]


@app.get("/api/v1/ping", tags=["v1"])
async def v1_ping(client: str = Depends(_api_auth)) -> dict:
    """Bağlantı + kimlik testi. Anahtarın adını döndürür."""
    return {"ok": True, "service": "nobetci", "version": "1", "client": client}


@app.get("/metrics")
async def metrics(client: str = Depends(_api_auth)) -> Response:
    """Prometheus uyumlu metrik ucu (X-API-Key / Bearer anahtarı ile korunur).
    Mevcut Zabbix/Grafana altyapısına beslenebilir (mimari §2.2)."""
    devs = await _build_overview()
    open_alerts = await _pool.fetchval(
        "SELECT count(*) FROM event WHERE acked_by IS NULL AND severity <> 'info' "
        "AND source <> 'event-stream'"
    ) or 0
    lines: list[str] = []

    def head(name: str, help_: str) -> None:
        lines.append(f"# HELP nobetci_{name} {help_}")
        lines.append(f"# TYPE nobetci_{name} gauge")

    reach = sum(1 for d in devs if d.get("reachable"))
    head("devices_total", "Toplam yonetilen cihaz")
    lines.append(f"nobetci_devices_total {len(devs)}")
    head("devices_reachable", "Erisilebilir cihaz")
    lines.append(f"nobetci_devices_reachable {reach}")
    head("open_alerts", "Acik alarm sayisi")
    lines.append(f"nobetci_open_alerts {open_alerts}")
    head("device_reachable", "NVR erisilebilir (1/0)")
    for d in devs:
        lbl = f'device="{_prom_label(d["name"])}",model="{_prom_label(d.get("model"))}"'
        lines.append(f"nobetci_device_reachable{{{lbl}}} {1 if d.get('reachable') else 0}")

    def gauge(name: str, help_: str, valfn) -> None:
        head(name, help_)
        for d in devs:
            v = valfn(d)
            if v is not None:
                lines.append(f'nobetci_{name}{{device="{_prom_label(d["name"])}"}} {v}')

    def sm(d, k):
        return (d.get("smart") or {}).get(k) if d.get("smart") else None

    def cam(d, k):
        return (d.get("cameras") or {}).get(k) if d.get("cameras") else None

    gauge("capacity_fill_percent", "Disk doluluk yuzdesi",
          lambda d: round(d["fill_pct"], 1) if d.get("fill_pct") is not None else None)
    gauge("retention_days", "Saklama derinligi (gun)",
          lambda d: round(d["retention_days"], 1) if d.get("retention_days") is not None else None)
    gauge("cameras_online", "Cevrimici kamera sayisi", lambda d: cam(d, "online"))
    gauge("cameras_total", "Toplam kamera sayisi", lambda d: cam(d, "total"))
    gauge("disk_max_temp_celsius", "En yuksek disk sicakligi", lambda d: sm(d, "max_temp"))
    gauge("smart_disks_total", "SMART izlenen disk", lambda d: sm(d, "total"))
    gauge("smart_disks_crit", "SMART kritik disk", lambda d: sm(d, "crit"))
    gauge("smart_disks_warn", "SMART uyari disk", lambda d: sm(d, "warn"))
    gauge("smart_predict", "Uretici ariza ongorusu (1/0)",
          lambda d: (1 if sm(d, "predict") else 0) if d.get("smart") else None)
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/api/v1/devices", tags=["v1"])
async def v1_devices(client: str = Depends(_api_auth)) -> dict:
    """Tüm cihazların tam durumu (kapasite, RAID, kamera, disk SMART özeti)."""
    return {"devices": await _build_overview()}


@app.get("/api/v1/devices/{name}", tags=["v1"])
async def v1_device(name: str, client: str = Depends(_api_auth)) -> dict:
    """Tek cihazın tam durumu (ada göre)."""
    for d in await _build_overview():
        if d["name"] == name:
            return d
    raise HTTPException(status_code=404, detail="cihaz bulunamadı")


@app.get("/api/v1/devices/{name}/smart", tags=["v1"])
async def v1_device_smart(name: str, client: str = Depends(_api_auth)) -> dict:
    """Bir cihazın fiziksel disklerinin SMART detayı (öngörücü bakım)."""
    rows = await _pool.fetch(
        """SELECT ds.disk_name, ds.health, ds.temperature_c, ds.power_on_hours,
                  ds.reallocated, ds.pending, ds.uncorrectable, ds.predict, ds.attrs
           FROM disk_smart ds JOIN nvr n ON n.id=ds.nvr_id
           WHERE n.name=$1 ORDER BY ds.disk_name""",
        name,
    )
    disks = []
    for r in rows:
        attrs = r["attrs"]
        if isinstance(attrs, str):
            try:
                attrs = json.loads(attrs)
            except ValueError:
                attrs = []
        disks.append({**dict(r), "attrs": attrs or []})
    return {"device": name, "disks": disks}


@app.get("/api/v1/alarms", tags=["v1"])
async def v1_alarms(client: str = Depends(_api_auth), status: str = "open") -> dict:
    """Açık ('open') ya da tüm ('all') alarm/olay geçmişi."""
    where = "e.source <> 'event-stream'"
    if status == "open":
        where += " AND e.acked_by IS NULL AND e.severity <> 'info'"
    rows = await _pool.fetch(
        f"""SELECT e.id, e.ts, e.code, e.severity,
                   e.payload->>'message' AS message, n.name AS device, e.acked_by
            FROM event e LEFT JOIN nvr n ON n.id=e.nvr_id
            WHERE {where} ORDER BY e.ts DESC LIMIT 500"""
    )
    return {
        "alarms": [
            {
                "id": r["id"], "ts": r["ts"].isoformat(sep=" ", timespec="seconds"),
                "code": r["code"], "severity": r["severity"],
                "message": r["message"] or "", "device": r["device"] or "-",
                "acked_by": r["acked_by"],
            }
            for r in rows
        ]
    }


@app.get("/api/v1/fleet", tags=["v1"])
async def v1_fleet(client: str = Depends(_api_auth)) -> dict:
    """Filo geneli özet: cihaz/disk/kamera sayıları, açık alarm."""
    devs = await _build_overview()
    reachable = sum(1 for d in devs if d.get("reachable"))
    cam_total = sum((d.get("cameras") or {}).get("total", 0) for d in devs)
    cam_off = sum((d.get("cameras") or {}).get("offline", 0) for d in devs)
    smart_total = sum((d.get("smart") or {}).get("total", 0) for d in devs)
    smart_attn = sum(
        (d.get("smart") or {}).get("warn", 0) + (d.get("smart") or {}).get("crit", 0)
        for d in devs
    )
    open_alarms = await _pool.fetchval(
        "SELECT count(*) FROM event WHERE acked_by IS NULL "
        "AND severity <> 'info' AND source <> 'event-stream'"
    )
    return {
        "devices": len(devs), "reachable": reachable,
        "cameras_total": cam_total, "cameras_offline": cam_off,
        "smart_disks": smart_total, "smart_attention": smart_attn,
        "open_alarms": open_alarms or 0,
    }


# --- API anahtarı yönetimi (panel Basic auth ile korunur) ---


@app.post("/api/keys")
async def create_api_key(_: str = Depends(_auth), name: str = Form(...)) -> JSONResponse:
    """Yeni API anahtarı üretir. Anahtar YALNIZCA bir kez, üretildiğinde döner
    (karması saklanır); kaybolursa yenisi üretilmeli."""
    raw = "nk_" + secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode()).hexdigest()
    await _pool.execute(
        "INSERT INTO api_key (name, key_hash) VALUES ($1, $2)", name.strip(), h
    )
    return JSONResponse({"name": name.strip(), "key": raw})


@app.get("/api/keys")
async def list_api_keys(_: str = Depends(_auth)) -> dict:
    rows = await _pool.fetch(
        "SELECT id, name, enabled, created_at, last_used FROM api_key ORDER BY created_at DESC"
    )
    return {
        "keys": [
            {
                "id": r["id"], "name": r["name"], "enabled": r["enabled"],
                "created_at": r["created_at"].isoformat(sep=" ", timespec="minutes"),
                "last_used": r["last_used"].isoformat(sep=" ", timespec="minutes")
                if r["last_used"] else None,
            }
            for r in rows
        ]
    }


@app.post("/api/keys/{key_id}/revoke")
async def revoke_api_key(key_id: int, _: str = Depends(_auth)) -> dict:
    await _pool.execute("DELETE FROM api_key WHERE id=$1", key_id)
    return {"ok": True}
