"""Sahte Dahua NVR — gerçek cihaz olmadan geliştirme/test için.

storageDevice.cgi ve magicBox.cgi uçlarını taklit eder. Senaryo, ortam
değişkeniyle seçilir; böylece alarm/pano geliştirirken arıza durumları
üretilebilir:

    SIM_SCENARIO=healthy|disk_error|raid_degraded  SIM_PORT=8080  python nvr_sim.py

Bağımlılığı yoktur (stdlib http.server). Kimlik doğrulama uygulamaz;
sürücünün auth akışı için testlerdeki httpx.MockTransport kullanılır.
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

SCENARIO = os.environ.get("SIM_SCENARIO", "healthy")
PORT = int(os.environ.get("SIM_PORT", "8080"))

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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib API adı)
        url = urlparse(self.path)
        action = parse_qs(url.query).get("action", [""])[0]
        if url.path == "/cgi-bin/magicBox.cgi" and action in MAGICBOX:
            body = MAGICBOX[action]
        elif url.path == "/cgi-bin/storageDevice.cgi" and action == "getDeviceAllInfo":
            body = storage_response()
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
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
