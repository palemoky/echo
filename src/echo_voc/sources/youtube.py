from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from datetime import datetime

from echo_voc.errors import ConfigError, SourceError
from echo_voc.models import AccountInfo, Comment, FetchResult, WorkInfo
from echo_voc.sources.base import Source

_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)(?P<id>[\w-]{11})"
)
_CHANNEL_RE = re.compile(r"youtube\.com/(?:channel/(?P<id>UC[\w-]{22})|(?P<handle>@[\w.-]+))")
API = "https://www.googleapis.com/youtube/v3"


def parse_video_id(target: str) -> str:
    if m := _ID_RE.search(target):
        return m["id"]
    raise SourceError(f"Not a YouTube video URL: {target}")


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class YouTubeSource(Source):
    name = "youtube"
    description = "YouTube video comments (YouTube Data API v3, needs YOUTUBE_API_KEY)"
    example = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    account_example = "https://www.youtube.com/@GoogleDevelopers"

    def __init__(self, client=None, api_key: str | None = None) -> None:
        super().__init__(client)
        self.api_key = api_key or os.getenv("YOUTUBE_API_KEY")

    @classmethod
    def matches(cls, target: str) -> bool:
        return bool(_ID_RE.search(target))

    @classmethod
    def matches_account(cls, target: str) -> bool:
        return bool(_CHANNEL_RE.search(target))

    async def fetch(self, target: str, limit: int = 200) -> FetchResult:
        video_id = parse_video_id(target)
        video = await self._video(video_id)
        comments: list[Comment] = []
        async for comment in self._iter_threads(video_id, order="relevance"):
            comments.append(comment)
            if len(comments) >= limit:
                break
        return FetchResult(
            platform=self.name,
            target=target,
            title=video["snippet"].get("title"),
            comments=comments,
        )

    async def resolve_work(self, target: str) -> tuple[AccountInfo, WorkInfo]:
        video = await self._video(parse_video_id(target))
        snippet = video["snippet"]
        account = AccountInfo(
            platform=self.name,
            external_id=snippet["channelId"],
            name=snippet.get("channelTitle"),
            url=f"https://www.youtube.com/channel/{snippet['channelId']}",
        )
        return account, self._work(video["id"], snippet.get("title"), snippet.get("publishedAt"))

    async def resolve_account(self, target: str) -> AccountInfo:
        m = _CHANNEL_RE.search(target)
        if not m:
            raise SourceError(f"Not a YouTube channel URL: {target}")
        params = {"part": "snippet,contentDetails"}
        params |= {"id": m["id"]} if m["id"] else {"forHandle": m["handle"]}
        items = (await self._get("channels", params)).get("items") or []
        if not items:
            raise SourceError(f"YouTube channel not found: {target}")
        ch = items[0]
        return AccountInfo(
            platform=self.name,
            external_id=ch["id"],
            name=ch["snippet"].get("title"),
            url=f"https://www.youtube.com/channel/{ch['id']}",
            meta={"uploads": ch["contentDetails"]["relatedPlaylists"]["uploads"]},
        )

    async def iter_works(self, account: AccountInfo) -> AsyncIterator[WorkInfo]:
        uploads = (
            account.meta.get("uploads")
            or (
                await self.resolve_account(f"https://www.youtube.com/channel/{account.external_id}")
            ).meta["uploads"]
        )
        token: str | None = None
        while True:
            params = {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": 50}
            if token:
                params["pageToken"] = token
            data = await self._get("playlistItems", params)
            for item in data.get("items", []):
                details = item["contentDetails"]
                yield self._work(
                    details["videoId"],
                    item["snippet"].get("title"),
                    details.get("videoPublishedAt") or item["snippet"].get("publishedAt"),
                )
            token = data.get("nextPageToken")
            if not token:
                return

    async def iter_comments(
        self, work: WorkInfo, *, replies: bool = False
    ) -> AsyncIterator[Comment]:
        async for comment in self._iter_threads(work.external_id, order="time", replies=replies):
            yield comment

    # ---- helpers ----------------------------------------------------------------------------

    def _work(self, video_id: str, title: str | None, published: str | None) -> WorkInfo:
        return WorkInfo(
            platform=self.name,
            external_id=video_id,
            title=title,
            url=f"https://www.youtube.com/watch?v={video_id}",
            published_at=_ts(published),
        )

    async def _video(self, video_id: str) -> dict:
        data = await self._get("videos", {"part": "snippet", "id": video_id})
        items = data.get("items") or []
        if not items:
            raise SourceError(f"YouTube video not found: {video_id}")
        return items[0]

    async def _iter_threads(
        self, video_id: str, *, order: str, replies: bool = False
    ) -> AsyncIterator[Comment]:
        token: str | None = None
        part = "snippet,replies" if replies else "snippet"
        while True:
            params = {
                "part": part,
                "videoId": video_id,
                "maxResults": 100,
                "order": order,
                "textFormat": "plainText",
            }
            if token:
                params["pageToken"] = token
            data = await self._get("commentThreads", params)
            for item in data.get("items", []):
                top = item["snippet"]["topLevelComment"]
                yield _comment(top.get("id", item["id"]), top["snippet"], video_id)
                # The thread embeds up to 5 replies; enough for feedback purposes.
                for reply in (item.get("replies") or {}).get("comments", []):
                    yield _comment(reply["id"], reply["snippet"], video_id, parent=item["id"])
            token = data.get("nextPageToken")
            if not token:
                return

    async def _get(self, path: str, params: dict) -> dict:
        if not self.api_key:
            raise ConfigError(
                "YOUTUBE_API_KEY is not set. Create one at "
                "https://console.cloud.google.com/apis/credentials (enable YouTube Data API v3)."
            )
        resp = await self.request("GET", f"{API}/{path}", params={**params, "key": self.api_key})
        if resp.status_code != 200:
            try:
                message = resp.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = resp.text[:200]
            if resp.status_code == 403 and "disabled comments" in message:
                return {}
            raise SourceError(f"YouTube API error ({resp.status_code}): {message}")
        return resp.json()


def _comment(comment_id: str, snippet: dict, video_id: str, parent: str | None = None) -> Comment:
    return Comment(
        id=comment_id,
        platform="youtube",
        kind="reply" if parent else "comment",
        parent_id=parent,
        author=(snippet.get("authorChannelId") or {}).get("value")
        or snippet.get("authorDisplayName"),
        content=snippet.get("textOriginal") or snippet.get("textDisplay", ""),
        likes=snippet.get("likeCount"),
        created_at=_ts(snippet.get("publishedAt")),
        url=f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
    )
