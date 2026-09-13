from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from echo_voc.errors import ConfigError, SourceError
from echo_voc.models import AccountInfo, Comment, FetchResult, WorkInfo
from echo_voc.sources import bilibili_danmaku as dm
from echo_voc.sources import bilibili_wbi as wbi
from echo_voc.sources.base import Source

log = logging.getLogger(__name__)

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:bilibili\.com/video/|^)av(\d+)", re.IGNORECASE)
_SPACE_RE = re.compile(r"(?:space\.bilibili\.com/|^bilibili:)(\d+)")

API = "https://api.bilibili.com"
VIEW_API = f"{API}/x/web-interface/view"
NAV_API = f"{API}/x/web-interface/nav"
REPLY_API = f"{API}/x/v2/reply/main"
SUB_REPLY_API = f"{API}/x/v2/reply/reply"
SPACE_VIDEOS_API = f"{API}/x/space/wbi/arc/search"
SPACE_INFO_API = f"{API}/x/space/wbi/acc/info"
DM_SEG_API = f"{API}/x/v2/dm/web/seg.so"
DM_XML_API = f"{API}/x/v1/dm/list.so"
PAGE_SIZE = 20
MODE_TIME, MODE_HOT = 2, 3
# A full browser User-Agent without matching cookies/WBI signature trips risk control (-352)
# and guest sessions get only 3 hot replies; a bare UA paginates normally.
USER_AGENT = "Mozilla/5.0"
# Browser fingerprint params the space endpoints expect alongside the WBI signature.
DM_PARAMS = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HTEUgKEFwcGxlLCBBTkdMRSBNZXRhbCBSZW5kZXJlcik",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


class BilibiliSource(Source):
    name = "bilibili"
    description = "Bilibili video comments & danmaku (account sync needs BILIBILI_COOKIE)"
    example = "https://www.bilibili.com/video/BV1GJ411x7h7"
    account_example = "https://space.bilibili.com/486906719"
    supports_danmaku = True
    min_interval = 1.0
    jitter = 0.5

    def __init__(self, client=None, cookie: str | None = None) -> None:
        super().__init__(client)
        self.cookie = cookie or os.getenv("BILIBILI_COOKIE")
        self._wbi_key: str | None = None

    @classmethod
    def matches(cls, target: str) -> bool:
        return bool(_BV_RE.search(target) or _AV_RE.search(target.strip()))

    @classmethod
    def matches_account(cls, target: str) -> bool:
        return bool(_SPACE_RE.search(target.strip()))

    # ---- realtime ---------------------------------------------------------------------------

    async def fetch(self, target: str, limit: int = 200) -> FetchResult:
        _, work = await self.resolve_work(target)
        comments: list[Comment] = []
        async for comment in self._iter_replies(int(work.meta["aid"]), MODE_HOT):
            comments.append(comment)
            if len(comments) >= limit:
                break
        return FetchResult(platform=self.name, target=target, title=work.title, comments=comments)

    # ---- account sync -----------------------------------------------------------------------

    async def resolve_work(self, target: str) -> tuple[AccountInfo, WorkInfo]:
        if m := _BV_RE.search(target):
            params = {"bvid": m[1]}
        elif m := _AV_RE.search(target.strip()):
            params = {"aid": m[1]}
        else:
            raise SourceError(f"Not a Bilibili video URL: {target}")
        data = await self._get(VIEW_API, params)
        owner = data.get("owner") or {}
        account = AccountInfo(
            platform=self.name,
            external_id=str(owner.get("mid")),
            name=owner.get("name"),
            url=f"https://space.bilibili.com/{owner.get('mid')}",
        )
        return account, _work_from_view(data)

    async def resolve_account(self, target: str) -> AccountInfo:
        m = _SPACE_RE.search(target.strip())
        if not m:
            raise SourceError(f"Not a Bilibili space URL: {target}")
        mid = m[1]
        name = None
        try:
            name = (await self._get(SPACE_INFO_API, {"mid": mid}, signed=True)).get("name")
        except SourceError as e:  # name is cosmetic; listing videos is what matters
            log.info("Could not read Bilibili account info: %s", e)
        return AccountInfo(
            platform=self.name, external_id=mid, name=name, url=f"https://space.bilibili.com/{mid}"
        )

    async def iter_works(self, account: AccountInfo) -> AsyncIterator[WorkInfo]:
        if not self.cookie:
            raise ConfigError(
                "Listing all videos of a Bilibili account requires BILIBILI_COOKIE "
                "(copy the Cookie header, including SESSDATA, from a logged-in browser)."
            )
        page = 1
        while True:
            data = await self._get(
                SPACE_VIDEOS_API,
                {"mid": account.external_id, "pn": page, "ps": 50, "order": "pubdate", **DM_PARAMS},
                signed=True,
            )
            videos = ((data.get("list") or {}).get("vlist")) or []
            for v in videos:
                yield WorkInfo(
                    platform=self.name,
                    external_id=v["bvid"],
                    title=v.get("title"),
                    url=f"https://www.bilibili.com/video/{v['bvid']}",
                    published_at=datetime.fromtimestamp(v["created"], tz=timezone.utc),
                    meta={
                        "aid": v.get("aid"),
                        "comments": v.get("comment"),
                        "plays": v.get("play"),
                        "author": v.get("author"),
                    },
                )
            total = (data.get("page") or {}).get("count", 0)
            if not videos or page * 50 >= total:
                return
            page += 1

    async def iter_comments(
        self, work: WorkInfo, *, replies: bool = False
    ) -> AsyncIterator[Comment]:
        aid = work.meta.get("aid") or (await self.resolve_work(work.external_id))[1].meta["aid"]
        async for comment in self._iter_replies(int(aid), MODE_TIME, sub_replies=replies):
            yield comment

    async def fetch_danmaku(self, work: WorkInfo) -> list[Comment]:
        pages = work.meta.get("pages")
        if not pages:
            _, fresh = await self.resolve_work(work.external_id)
            pages = fresh.meta["pages"]
            work.meta.update(fresh.meta)
        aid = work.meta["aid"]
        comments: dict[str, Comment] = {}
        for page in pages:
            cid, duration = page["cid"], page.get("duration") or 0
            elems: list[dm.DanmakuElem] = []
            try:
                for index in range(1, max(1, math.ceil(duration * 1000 / dm.SEGMENT_MS)) + 1):
                    resp = await self.request(
                        "GET",
                        DM_SEG_API,
                        params={"type": 1, "oid": cid, "segment_index": index},
                        headers=self._headers(),
                    )
                    if resp.status_code != 200:
                        raise SourceError(f"danmaku segment HTTP {resp.status_code}")
                    elems.extend(dm.decode_segment(resp.content))
            except (SourceError, ValueError) as e:
                log.info("seg.so failed for cid %s (%s); falling back to XML", cid, e)
                resp = await self.request(
                    "GET", DM_XML_API, params={"oid": cid}, headers=self._headers()
                )
                elems = dm.decode_xml(resp.text)
            for e in elems:
                dmid = e.id_str or str(e.id)
                if not dmid or not e.content.strip():
                    continue
                comments[dmid] = Comment(
                    id=dmid,
                    platform=self.name,
                    kind="danmaku",
                    content=e.content,
                    author=e.mid_hash or None,
                    progress_ms=e.progress,
                    created_at=datetime.fromtimestamp(e.ctime, tz=timezone.utc)
                    if e.ctime
                    else None,
                    url=f"https://www.bilibili.com/video/av{aid}?p={page.get('page', 1)}"
                    f"&t={e.progress // 1000}",
                )
        return list(comments.values())

    # ---- helpers ----------------------------------------------------------------------------

    async def _iter_replies(
        self, aid: int, mode: int, *, sub_replies: bool = False
    ) -> AsyncIterator[Comment]:
        seen: set[str] = set()
        cursor = 0
        while True:
            data = await self._get(
                REPLY_API, {"type": 1, "oid": aid, "mode": mode, "next": cursor, "ps": PAGE_SIZE}
            )
            replies = data.get("replies") or []
            new = 0
            for reply in replies:
                rpid = str(reply["rpid"])
                if rpid in seen:
                    continue
                seen.add(rpid)
                new += 1
                yield _to_comment(reply, aid)
                if sub_replies and reply.get("rcount"):
                    async for sub in self._iter_sub_replies(aid, rpid):
                        yield sub
            page = data.get("cursor") or {}
            if not replies or not new or page.get("is_end"):
                return
            cursor = page.get("next", cursor + 1)

    async def _iter_sub_replies(self, aid: int, root: str) -> AsyncIterator[Comment]:
        page = 1
        while True:
            data = await self._get(
                SUB_REPLY_API, {"type": 1, "oid": aid, "root": root, "pn": page, "ps": PAGE_SIZE}
            )
            replies = data.get("replies") or []
            for reply in replies:
                yield _to_comment(reply, aid, kind="reply")
            count = (data.get("page") or {}).get("count", 0)
            if not replies or page * PAGE_SIZE >= count:
                return
            page += 1

    def _headers(self) -> dict:
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _wbi(self) -> str:
        if self._wbi_key is None:
            resp = await self.request("GET", NAV_API, headers=self._headers())
            img = (resp.json().get("data") or {}).get("wbi_img") or {}
            if not img:
                raise SourceError("Could not obtain Bilibili WBI keys")
            self._wbi_key = wbi.mixin_key(img["img_url"], img["sub_url"])
        return self._wbi_key

    async def _get(self, url: str, params: dict, *, signed: bool = False) -> dict:
        for attempt in range(3):
            query = wbi.sign(params, await self._wbi()) if signed else params
            resp = await self.request("GET", url, params=query, headers=self._headers())
            if resp.status_code != 200:
                raise SourceError(f"Bilibili API returned HTTP {resp.status_code}")
            body = resp.json()
            code = body.get("code")
            if code == 0:
                return body.get("data") or {}
            if code == -352 and attempt < 2:
                self._wbi_key = None  # keys rotate daily; refresh and back off
                await asyncio.sleep(self.backoff_base * (attempt + 1) * 5)
                continue
            if code == -352:
                raise SourceError(
                    "Bilibili risk control blocked the request (-352). Set BILIBILI_COOKIE to "
                    "your logged-in browser cookie, wait a few minutes and retry."
                )
            raise SourceError(f"Bilibili API error {code}: {body.get('message')}")
        raise SourceError("unreachable")


def _work_from_view(data: dict) -> WorkInfo:
    return WorkInfo(
        platform="bilibili",
        external_id=data["bvid"],
        title=data.get("title"),
        url=f"https://www.bilibili.com/video/{data['bvid']}",
        published_at=datetime.fromtimestamp(data["pubdate"], tz=timezone.utc)
        if data.get("pubdate")
        else None,
        meta={
            "aid": data["aid"],
            "duration": data.get("duration"),
            "pages": [
                {
                    "cid": p["cid"],
                    "page": p.get("page", 1),
                    "part": p.get("part"),
                    "duration": p.get("duration"),
                }
                for p in data.get("pages")
                or [{"cid": data.get("cid"), "duration": data.get("duration")}]
            ],
            "comments": (data.get("stat") or {}).get("reply"),
            "danmaku": (data.get("stat") or {}).get("danmaku"),
        },
    )


def _to_comment(reply: dict, aid: int, kind: str = "comment") -> Comment:
    rpid = str(reply["rpid"])
    parent = str(reply.get("root") or "") or None
    return Comment(
        id=rpid,
        platform="bilibili",
        kind=kind,
        parent_id=parent if parent and parent != "0" else None,
        author=str((reply.get("member") or {}).get("mid") or "") or None,
        content=(reply.get("content") or {}).get("message", ""),
        likes=reply.get("like"),
        created_at=datetime.fromtimestamp(reply["ctime"], tz=timezone.utc),
        url=f"https://www.bilibili.com/video/av{aid}#reply{rpid}",
    )
