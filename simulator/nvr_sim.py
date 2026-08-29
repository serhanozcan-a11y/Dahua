"""Sahte Dahua NVR — gerçek cihaz olmadan geliştirme/test için.

storageDevice.cgi ve magicBox.cgi uçlarını taklit eder. Senaryo, ortam
değişkeniyle seçilir; böylece alarm/pano geliştirirken arıza durumları
üretilebilir:

    SIM_SCENARIO=healthy|disk_error|raid_degraded  SIM_PORT=8080  python nvr_sim.py

Bağımlılığı yoktur (stdlib http.server). Kimlik doğrulama uygulamaz;
sürücünün auth akışı için testlerdeki httpx.MockTransport kullanılır.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCENARIO = os.environ.get("SIM_SCENARIO", "healthy")
PORT = int(os.environ.get("SIM_PORT", "8080"))
OLDEST_DAYS = float(os.environ.get("SIM_OLDEST_DAYS", "32"))

MAGICBOX = {
    "getDeviceType": "type=NVR608-32-4KS2\r\n",
    "getSerialNo": "sn=SIM0000001\r\n",
    "getSoftwareVersion": "version=3.216.0000000.1\r\n",
}


def _disk(i: int, name: str, state: str, total: int, used: int, is_error: str) -> str:
    return (
        f"list.info[{i}].Name={name}\r\n"
        f"list.info[{i}].State={state}\r\n"
        f"list.info[{i}].Detail[0].Type=ReadWrite\r\n"
        f"list.info[{i}].Detail[0].TotalBytes={total}\r\n"
        f"list.info[{i}].Detail[0].UsedBytes={used}\r\n"
        f"list.info[{i}].Detail[0].IsError={is_error}\r\n"
        f"list.info[{i}].HealthDataFlag={'false' if is_error == 'true' else 'true'}\r\n"
    )


def storage_response() -> str:
    tb = 6_001_175_126_016  # 6 TB
    if SCENARIO == "disk_error":
        disks = [
            _disk(0, "/dev/sda", "Success", tb, tb // 2, "false"),
            _disk(1, "/dev/sdb", "Failure", tb, 0, "true"),
        ]
        raid_state = "Success"
    elif SCENARIO == "raid_degraded":
        disks = [
            _disk(0, "/dev/sda", "Success", tb, tb // 2, "false"),
            _disk(1, "/dev/sdb", "Absent", 0, 0, "false"),
        ]
        raid_state = "Degraded"
    else:
        disks = [
            _disk(0, "/dev/sda", "Success", tb, tb // 2, "false"),
            _disk(1, "/dev/sdb", "Success", tb, tb // 3, "false"),
        ]
        raid_state = "Success"
    i = len(disks)
    raid = (
        f"list.info[{i}].Name=/dev/md0\r\n"
        f"list.info[{i}].State={raid_state}\r\n"
        f"list.info[{i}].Type=Raid5\r\n"
        f"list.info[{i}].Detail[0].TotalBytes={2 * tb}\r\n"
        f"list.info[{i}].Detail[0].UsedBytes={tb}\r\n"
        f"list.info[{i}].Detail[0].IsError=false\r\n"
    )
    return "".join(disks) + raid


def media_find_response(action: str, query: dict) -> str | None:
    """mediaFileFind akışının basit taklidi (tek finder, durum tutulmaz)."""
    if action == "factory.create":
        return "result=1000\r\n"
    if action == "findFile":
        return "OK\r\n"
    if action == "findNextFile":
        oldest = datetime.now() - timedelta(days=OLDEST_DAYS)
        start = oldest.strftime("%Y-%m-%d %H:%M:%S")
        end = (oldest + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        return (
            "found=1\r\n"
            "items[0].Channel=1\r\n"
            f"items[0].StartTime={start}\r\n"
            f"items[0].EndTime={end}\r\n"
            "items[0].FilePath=/mnt/dvr/sim/oldest.dav\r\n"
        )
    if action in ("close", "destroy"):
        return "OK\r\n"
    return None


def rpc2_response(payload: dict) -> dict:
    """Basitleştirilmiş RPC2: login challenge + RAID.getDevices."""
    method = payload.get("method", "")
    rid = payload.get("id", 0)
    if method == "global.login" and not payload.get("params", {}).get("password"):
        return {
            "id": rid, "session": "SIMSESSION", "result": False,
            "params": {"realm": "Login to SIM", "random": "42abc",
                       "encryption": "Default"},
        }
    if method == "global.login":
        return {"id": rid, "session": "SIMSESSION", "result": True}
    if method == "RAID.getDevices":
        state = "Degrade" if SCENARIO == "raid_degraded" else "Active"
        raid = {
            "Name": "/dev/md0", "State": state,
            "Members": ["/dev/sda", "/dev/sdb"], "HotSpares": [],
        }
        if SCENARIO == "raid_degraded":
            raid["RebuildProgress"] = 37
        return {"id": rid, "result": True, "params": {"raids": [raid]}}
    return {"id": rid, "result": True}


class Handler(BaseHTTPRequestHandler):
    def _stream_events(self) -> None:
        """attach: senaryoya göre bir olay, ardından heartbeat akışı."""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=myboundary")
        self.end_headers()
        try:
            if SCENARIO == "disk_error":
                self.wfile.write(b"Code=StorageFailure;action=Start;index=1\r\n")
            elif SCENARIO == "raid_degraded":
                self.wfile.write(b"Code=StorageAbnormal;action=Start;index=0\r\n")
            self.wfile.flush()
            while True:
                time.sleep(5)
                self.wfile.write(b"Heartbeat\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:  # noqa: N802 (stdlib API adı)
        url = urlparse(self.path)
        if url.path not in ("/RPC2_Login", "/RPC2"):
            self.send_error(400, "Bad Request")
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        data = json.dumps(rpc2_response(payload)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API adı)
        url = urlparse(self.path)
        query = parse_qs(url.query)
        action = query.get("action", [""])[0]
        if url.path == "/cgi-bin/eventManager.cgi" and action == "attach":
            self._stream_events()
            return
        if url.path == "/cgi-bin/magicBox.cgi" and action in MAGICBOX:
            body = MAGICBOX[action]
        elif url.path == "/cgi-bin/storageDevice.cgi" and action == "getDeviceAllInfo":
            body = storage_response()
        elif url.path == "/cgi-bin/mediaFileFind.cgi":
            maybe = media_find_response(action, query)
            if maybe is None:
                self.send_error(400, "Bad Request")
                return
            body = maybe
        else:
            self.send_error(400, "Bad Request")
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        pass


if __name__ == "__main__":
    print(f"Sahte NVR :{PORT} üzerinde, senaryo: {SCENARIO}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
