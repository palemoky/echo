from __future__ import annotations

import httpx
import pytest
import respx

from echo_voc.analyzer_danmaku import find_hotspots, histogram, mmss
from echo_voc.errors import ConfigError
from echo_voc.models import AccountInfo, Comment, WorkInfo
from echo_voc.sources import bilibili_danmaku as dm
from echo_voc.sources import bilibili_wbi as wbi
from echo_voc.sources.bilibili import BilibiliSource


def test_wbi_matches_reference_vector():
    # Example from the community-maintained Bilibili API docs.
    key = wbi.mixin_key(
        "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
    )
    assert key == "ea1db124af3c7062474693fa704f4ff8"
    signed = wbi.sign({"foo": "114", "bar": "514", "zab": 1919810}, key, now=1702204169)
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, value) -> bytes:
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    data = value.encode() if isinstance(value, str) else value
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def _elem(id_: int, progress: int, content: str, ctime: int = 1700000000) -> bytes:
    body = (
        _field(1, id_)
        + _field(2, progress)
        + _field(3, 1)
        + _field(5, 16777215)
        + _field(6, "abc123")
        + _field(7, content)
        + _field(8, ctime)
        + _field(12, str(id_))
    )
    return _field(1, body)


def test_decode_segment_protobuf():
    buf = _elem(40685126698926083, 601, "我是一万个点赞") + _elem(2, 9416, "Never gonna 🎵")
    elems = dm.decode_segment(buf)
    assert [(e.id, e.progress, e.content, e.mid_hash) for e in elems] == [
        (40685126698926083, 601, "我是一万个点赞", "abc123"),
        (2, 9416, "Never gonna 🎵", "abc123"),
    ]
    assert elems[0].id_str == "40685126698926083"


def test_decode_xml():
    xml = (
        '<i><d p="0.60100,4,25,15138834,1604742895,0,9e5adeaa,40685126698926083,10">'
        "我是&amp;点赞</d></i>"
    )
    [e] = dm.decode_xml(xml)
    assert (e.id, e.progress, e.content, e.ctime, e.weight) == (
        40685126698926083,
        601,
        "我是&点赞",
        1604742895,
        10,
    )


def test_hotspots_merge_adjacent_peaks():
    danmaku = [
        Comment(id=str(i), platform="bilibili", kind="danmaku", content="x", progress_ms=p)
        for i, p in enumerate(
            [5_000] * 3 + [65_000] * 40 + [95_000] * 35 + [200_000] * 4 + [290_000] * 2
        )
    ]
    hist = histogram(danmaku, 300, 30)
    assert hist[2] == 40 and hist[3] == 35
    [spot] = find_hotspots(hist, 30)
    assert (spot.start_ms, spot.end_ms, spot.count) == (60_000, 120_000, 75)
    assert mmss(3_725_000) == "1:02:05"


@respx.mock
async def test_iter_works_signs_and_paginates(monkeypatch):
    respx.get("https://api.bilibili.com/x/web-interface/nav").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": -101,
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                    }
                },
            },
        )
    )

    def search(request):
        assert "w_rid" in request.url.params and "SESSDATA" in request.headers["Cookie"]
        page = int(request.url.params["pn"])
        vlist = [
            {
                "bvid": f"BV{page:02d}{i:08d}",
                "aid": page * 100 + i,
                "title": f"v{i}",
                "created": 1700000000 - page * 1000 - i,
                "comment": 1,
            }
            for i in range(50 if page == 1 else 10)
        ]
        return httpx.Response(
            200, json={"code": 0, "data": {"list": {"vlist": vlist}, "page": {"count": 60}}}
        )

    respx.get("https://api.bilibili.com/x/space/wbi/arc/search").mock(side_effect=search)
    source = BilibiliSource(cookie="SESSDATA=abc")
    works = [w async for w in source.iter_works(AccountInfo(platform="bilibili", external_id="1"))]
    assert len(works) == 60 and works[0].meta["aid"] == 100


async def test_iter_works_requires_cookie(monkeypatch):
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)
    with pytest.raises(ConfigError, match="BILIBILI_COOKIE"):
        async for _ in BilibiliSource().iter_works(
            AccountInfo(platform="bilibili", external_id="1")
        ):
            pass


@respx.mock
async def test_fetch_danmaku_segments_and_dedupes():
    calls = []

    def seg(request):
        calls.append(int(request.url.params["segment_index"]))
        return httpx.Response(200, content=_elem(1, 1000, "a") + _elem(2, 400_000, "b"))

    respx.get("https://api.bilibili.com/x/v2/dm/web/seg.so").mock(side_effect=seg)
    work = WorkInfo(
        platform="bilibili",
        external_id="BV1",
        meta={"aid": 9, "pages": [{"cid": 5, "page": 1, "duration": 700}]},
    )
    items = await BilibiliSource().fetch_danmaku(work)
    assert calls == [1, 2]  # 700s → two 6-minute segments
    assert sorted(d.id for d in items) == ["1", "2"]
    assert items[0].kind == "danmaku" and items[0].url.endswith("t=1")
