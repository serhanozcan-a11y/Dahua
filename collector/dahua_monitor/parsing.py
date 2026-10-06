"""Dahua CGI yanıtlarının ayrıştırılması.

Dahua CGI uçları `anahtar=değer` satırları döner; anahtarlar noktalı ve
indeksli bir ağaç kodlar, örn::

    list.info[0].Name=/dev/sda
    list.info[0].State=Success
    list.info[0].Detail[0].TotalBytes=1000204886016

Bu modül metni iç içe dict/list yapısına çevirir. Firmware sürümleri alan
ekleyip çıkarabildiği için ayrıştırıcı toleranslıdır: bilinmeyen alanlar
korunur, eksik alanlar hata üretmez.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"([^.\[\]]+)(?:\[(\d+)\])?")


def _coerce(value: str) -> Any:
    v = value.strip()
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _ensure_index(container: list, index: int) -> dict:
    while len(container) <= index:
        container.append({})
    if not isinstance(container[index], dict):
        container[index] = {}
    return container[index]


def parse_kv_tree(text: str) -> dict:
    """`a.b[0].c=v` satırlarını `{"a": {"b": [{"c": v}]}}` yapısına çevirir."""
    root: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line or line.startswith("Error"):
            continue
        key, _, raw = line.partition("=")
        node: Any = root
        tokens = _TOKEN.findall(key.strip())
        for i, (name, index) in enumerate(tokens):
            last = i == len(tokens) - 1
            if index == "":
                if last:
                    node[name] = _coerce(raw)
                else:
                    node = node.setdefault(name, {})
            else:
                arr = node.setdefault(name, [])
                if not isinstance(arr, list):
                    arr = node[name] = [arr]
                item = _ensure_index(arr, int(index))
                if last:
                    # `x[0]=v` biçimi: değeri doğrudan listeye yaz
                    arr[int(index)] = _coerce(raw)
                else:
                    node = item
    return root


def parse_flat(text: str) -> dict[str, str]:
    """Ağaç kurmadan düz `anahtar -> ham değer` sözlüğü (magicBox yanıtları için)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def storage_infos(tree: dict) -> list[dict]:
    """`storageDevice.cgi?action=getDeviceAllInfo` ağacından cihaz listesini çıkarır.

    Bazı sürümler `list.info[N]`, bazıları doğrudan `info[N]` kökü kullanır.
    """
    node = tree.get("list", tree)
    infos = node.get("info", [])
    if isinstance(infos, dict):
        infos = [infos]
    return [i for i in infos if isinstance(i, dict)]
