"""Bilibili WBI request signing (used by space / user endpoints)."""

from __future__ import annotations

import hashlib
import time
import urllib.parse

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]  # fmt: skip


def mixin_key(img_url: str, sub_url: str) -> str:
    raw = img_url.rsplit("/", 1)[-1].split(".")[0] + sub_url.rsplit("/", 1)[-1].split(".")[0]
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign(params: dict, key: str, now: int | None = None) -> dict:
    """Return params with ``wts`` and ``w_rid`` added."""
    signed = {**params, "wts": now if now is not None else int(time.time())}
    signed = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in sorted(signed.items())
    }
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return signed
