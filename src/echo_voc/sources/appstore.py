from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from echo_voc.errors import SourceError
from echo_voc.models import Comment, FetchResult
from echo_voc.sources.base import Source

# https://apps.apple.com/cn/app/wechat/id414478124  or  appstore:414478124[@cn]
_URL_RE = re.compile(r"apps\.apple\.com/(?:(?P<country>[a-z]{2})/)?app/(?:[^/]+/)?id(?P<id>\d+)")
_SHORT_RE = re.compile(r"^appstore:(?P<id>\d+)(?:@(?P<country>[a-z]{2}))?$", re.I)

log = logging.getLogger(__name__)

WEB_URL = "https://apps.apple.com/{country}/app/id{id}?see-all=reviews&platform=iphone"
FEED_URL = "https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={id}/sortby=mostrecent/json"
MAX_PAGES = 10  # Apple's public feed caps at 10 pages x 50 reviews


def _label(entry: dict, key: str) -> str | None:
    value = entry.get(key)
    return value.get("label") if isinstance(value, dict) else None


def parse_target(target: str) -> tuple[str, str]:
    """Return (app_id, country)."""
    if m := _URL_RE.search(target):
        return m["id"], (m["country"] or "us")
    if m := _SHORT_RE.match(target.strip()):
        return m["id"], (m["country"] or "us").lower()
    raise SourceError(f"Not an App Store URL: {target}")


class AppStoreSource(Source):
    name = "appstore"
    description = "Apple App Store reviews (RSS feed, up to 500 per region; web fallback)"
    example = "https://apps.apple.com/cn/app/wechat/id414478124"

    @classmethod
    def matches(cls, target: str) -> bool:
        return bool(_URL_RE.search(target) or _SHORT_RE.match(target.strip()))

    async def fetch(self, target: str, limit: int = 200) -> FetchResult:
        app_id, country = parse_target(target)
        comments, title = await self._fetch_feed(app_id, country, limit)
        if not comments:
            # Apple's RSS feed intermittently returns no entries; the web page embeds ~20 reviews.
            log.info("App Store RSS feed empty, falling back to web page")
            comments, title = await self._fetch_web(app_id, country)
        if not comments:
            raise SourceError(
                f"No reviews found for app {app_id} in region '{country}'. "
                f"Try another region, e.g. appstore:{app_id}@us"
            )
        return FetchResult(
            platform=self.name, target=target, title=title, comments=comments[:limit]
        )

    async def _fetch_feed(self, app_id: str, country: str, limit: int):
        comments: list[Comment] = []
        title: str | None = None
        for page in range(1, MAX_PAGES + 1):
            resp = await self.client().get(FEED_URL.format(country=country, page=page, id=app_id))
            if resp.status_code != 200:
                break
            try:
                entries = resp.json().get("feed", {}).get("entry") or []
            except ValueError:
                break
            if isinstance(entries, dict):  # a single entry is not wrapped in a list
                entries = [entries]
            if not entries:
                break
            for entry in entries:
                if "im:rating" not in entry:  # older feeds put app metadata first
                    title = title or _label(entry, "im:name")
                    continue
                comments.append(_to_comment(entry, app_id, country))
            if len(comments) >= limit:
                break
        return comments, title

    async def _fetch_web(self, app_id: str, country: str):
        resp = await self.client().get(WEB_URL.format(country=country, id=app_id))
        if resp.status_code == 404:
            raise SourceError(f"App {app_id} not found in App Store region '{country}'")
        if resp.status_code != 200:
            raise SourceError(f"App Store page returned HTTP {resp.status_code}")
        return parse_web_reviews(resp.text, app_id, country)


def parse_web_reviews(html: str, app_id: str, country: str) -> tuple[list[Comment], str | None]:
    m = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return [], None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return [], None

    reviews: dict[str, dict] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("$kind") == "Review" and node.get("id"):
                reviews.setdefault(str(node["id"]), node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    title_match = re.search(r"<title>(.*?)</title>", html, re.S)
    title = title_match.group(1).strip() if title_match else None
    comments = [
        Comment(
            id=rid,
            platform="appstore",
            author=r.get("reviewerName"),
            title=r.get("title"),
            content=r.get("contents") or "",
            rating=float(r["rating"]) if r.get("rating") else None,
            created_at=datetime.fromisoformat(r["date"].replace("Z", "+00:00"))
            if r.get("date")
            else None,
            url=WEB_URL.format(country=country, id=app_id),
        )
        for rid, r in reviews.items()
    ]
    return comments, title


def _to_comment(entry: dict, app_id: str, country: str) -> Comment:
    updated = _label(entry, "updated")
    rating = _label(entry, "im:rating")
    votes = _label(entry, "im:voteSum")
    return Comment(
        id=_label(entry, "id") or "",
        platform="appstore",
        author=(entry.get("author") or {}).get("name", {}).get("label"),
        title=_label(entry, "title"),
        content=_label(entry, "content") or "",
        rating=float(rating) if rating else None,
        likes=int(votes) if votes and votes.lstrip("-").isdigit() else None,
        created_at=datetime.fromisoformat(updated) if updated else None,
        version=_label(entry, "im:version"),
        url=f"https://apps.apple.com/{country}/app/id{app_id}?see-all=reviews",
    )
