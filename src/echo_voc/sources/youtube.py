from __future__ import annotations

import os
import re
from datetime import datetime

from echo_voc.errors import ConfigError, SourceError
from echo_voc.models import Comment, FetchResult
from echo_voc.sources.base import Source

_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)(?P<id>[\w-]{11})"
)
API = "https://www.googleapis.com/youtube/v3"


def parse_video_id(target: str) -> str:
    if m := _ID_RE.search(target):
        return m["id"]
    raise SourceError(f"Not a YouTube video URL: {target}")


class YouTubeSource(Source):
    name = "youtube"
    description = "YouTube video comments (YouTube Data API v3, needs YOUTUBE_API_KEY)"
    example = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def __init__(self, client=None, api_key: str | None = None) -> None:
        super().__init__(client)
        self.api_key = api_key or os.getenv("YOUTUBE_API_KEY")

    @classmethod
    def matches(cls, target: str) -> bool:
        return bool(_ID_RE.search(target))

    async def fetch(self, target: str, limit: int = 200) -> FetchResult:
        if not self.api_key:
            raise ConfigError(
                "YOUTUBE_API_KEY is not set. Create one at "
                "https://console.cloud.google.com/apis/credentials (enable YouTube Data API v3)."
            )
        video_id = parse_video_id(target)
        title = await self._title(video_id)

        comments: list[Comment] = []
        page_token: str | None = None
        while len(comments) < limit:
            params = {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": min(100, limit - len(comments)),
                "order": "relevance",
                "textFormat": "plainText",
                "key": self.api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            data = await self._get("commentThreads", params)
            for item in data.get("items", []):
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                comments.append(
                    Comment(
                        id=item["id"],
                        platform=self.name,
                        author=snippet.get("authorDisplayName"),
                        content=snippet.get("textOriginal") or snippet.get("textDisplay", ""),
                        likes=snippet.get("likeCount"),
                        created_at=datetime.fromisoformat(
                            snippet["publishedAt"].replace("Z", "+00:00")
                        ),
                        url=f"https://www.youtube.com/watch?v={video_id}&lc={item['id']}",
                    )
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return FetchResult(
            platform=self.name, target=target, title=title, comments=comments[:limit]
        )

    async def _title(self, video_id: str) -> str | None:
        data = await self._get("videos", {"part": "snippet", "id": video_id, "key": self.api_key})
        items = data.get("items") or []
        if not items:
            raise SourceError(f"YouTube video not found: {video_id}")
        return items[0]["snippet"].get("title")

    async def _get(self, path: str, params: dict) -> dict:
        resp = await self.client().get(f"{API}/{path}", params=params)
        if resp.status_code != 200:
            try:
                message = resp.json()["error"]["message"]
            except Exception:
                message = resp.text[:200]
            raise SourceError(f"YouTube API error ({resp.status_code}): {message}")
        return resp.json()
