"""Minimal decoder for Bilibili's danmaku protobuf (``x/v2/dm/web/seg.so``) and XML fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

SEGMENT_MS = 6 * 60 * 1000


@dataclass
class DanmakuElem:
    id: int = 0
    progress: int = 0  # ms into the video
    mode: int = 0
    content: str = ""
    ctime: int = 0
    mid_hash: str = ""
    weight: int = 0
    id_str: str = ""


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _fields(buf: bytes):
    """Yield (field_number, wire_type, value) for a protobuf message."""
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 1:
            value, pos = buf[pos : pos + 8], pos + 8
        elif wire == 2:
            length, pos = _varint(buf, pos)
            value, pos = buf[pos : pos + length], pos + length
        elif wire == 5:
            value, pos = buf[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire}")
        yield field, wire, value


_ELEM_FIELDS = {1: "id", 2: "progress", 3: "mode", 7: "content", 8: "ctime", 6: "mid_hash",
                9: "weight", 12: "id_str"}  # fmt: skip


def decode_segment(buf: bytes) -> list[DanmakuElem]:
    """Decode a ``DmSegMobileReply`` (field 1 = repeated DanmakuElem)."""
    elems = []
    for field, wire, value in _fields(buf):
        if field != 1 or wire != 2:
            continue
        elem = DanmakuElem()
        for f, w, v in _fields(value):
            name = _ELEM_FIELDS.get(f)
            if name is None:
                continue
            if w == 2:
                v = v.decode("utf-8", errors="replace")
            setattr(elem, name, v)
        elems.append(elem)
    return elems


_XML_D = re.compile(r'<d p="([^"]*)">(.*?)</d>', re.S)


def decode_xml(xml: str) -> list[DanmakuElem]:
    """Parse the legacy ``list.so`` XML.

    Each ``<d p="...">`` holds: progress_s, mode, size, color, ctime, pool, midhash, id, weight.
    """
    elems = []
    for attrs, text in _XML_D.findall(xml):
        p = attrs.split(",")
        if len(p) < 8:
            continue
        elems.append(
            DanmakuElem(
                id=int(p[7]),
                progress=int(float(p[0]) * 1000),
                mode=int(p[1]),
                ctime=int(p[4]),
                mid_hash=p[6],
                weight=int(p[8]) if len(p) > 8 and p[8].isdigit() else 0,
                content=unescape(text),
            )
        )
    return elems
