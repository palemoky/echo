from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from echo_voc.errors import SourceError
from echo_voc.models import Comment, FetchResult
from echo_voc.sources.base import Source

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:bilibili\.com/video/|^)av(\d+)", re.I)

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
REPLY_API = "https://api.bilibili.com/x/v2/reply/main"
# A full browser User-Agent without matching cookies/WBI signature trips risk control (-352)
# and guest sessions get only 3 hot replies; a bare UA paginates normally.
USER_AGENT = "Mozilla/5.0"
PAGE_SIZE = 20
MODE_HOT = 3


class BilibiliSource(Source):
    name = "bilibili"
    description = "Bilibili video comments (hot-sorted; set BILIBILI_COOKIE if rate-limited)"
    example = "https://www.bilibili.com/video/BV1GJ411x7h7"

    def __init__(self, client=None, cookie: str | None = None) -> None:
        super().__init__(client)
        self.cookie = cookie or os.getenv("BILIBILI_COOKIE")

    @classmethod
    def matches(cls, target: str) -> bool:
        return bool(_BV_RE.search(target) or _AV_RE.search(target.strip()))

    async def fetch(self, target: str, limit: int = 200) -> FetchResult:
        aid, title = await self._resolve(target)
        comments: list[Comment] = []
        seen: set[str] = set()
        cursor = 0

        while len(comments) < limit:
            data = await self._get(
                REPLY_API,
                {"type": 1, "oid": aid, "mode": MODE_HOT, "next": cursor, "ps": PAGE_SIZE},
            )
            replies = data.get("replies") or []
            new = 0
            for reply in replies:
                rpid = str(reply["rpid"])
                if rpid in seen:
                    continue
                seen.add(rpid)
                new += 1
                comments.append(
                    Comment(
                        id=rpid,
                        platform=self.name,
                        author=(reply.get("member") or {}).get("uname"),
                        content=(reply.get("content") or {}).get("message", ""),
                        likes=reply.get("like"),
                        created_at=datetime.fromtimestamp(reply["ctime"], tz=timezone.utc),
                        url=f"https://www.bilibili.com/video/av{aid}#reply{rpid}",
                    )
                )
            page = data.get("cursor") or {}
            if not replies or not new or page.get("is_end"):
                break
            cursor = page.get("next", cursor + 1)

        return FetchResult(
            platform=self.name, target=target, title=title, comments=comments[:limit]
        )

    async def _resolve(self, target: str) -> tuple[int, str | None]:
        if m := _BV_RE.search(target):
            params = {"bvid": m[1]}
        elif m := _AV_RE.search(target.strip()):
            params = {"aid": m[1]}
        else:
            raise SourceError(f"Not a Bilibili video URL: {target}")
        data = await self._get(VIEW_API, params)
        return int(data["aid"]), data.get("title")

    async def _get(self, url: str, params: dict) -> dict:
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        resp = await self.client().get(url, params=params, headers=headers)
        if resp.status_code != 200:
            raise SourceError(f"Bilibili API returned HTTP {resp.status_code}")
        body = resp.json()
        if body.get("code") == -352:
            raise SourceError(
                "Bilibili risk control blocked the request (-352). "
                "Set BILIBILI_COOKIE to your browser cookie and retry."
            )
        if body.get("code") != 0:
            raise SourceError(f"Bilibili API error {body.get('code')}: {body.get('message')}")
        return body.get("data") or {}
