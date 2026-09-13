from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime

from echo_voc.errors import SourceError
from echo_voc.models import AccountInfo, Comment, FetchResult, WorkInfo
from echo_voc.sources.base import Source

# https://apps.apple.com/cn/app/wechat/id414478124  or  appstore:414478124[@cn]
_URL_RE = re.compile(r"apps\.apple\.com/(?:(?P<country>[a-z]{2})/)?app/(?:[^/]+/)?id(?P<id>\d+)")
_SHORT_RE = re.compile(r"^appstore:(?P<id>\d+)(?:@(?P<country>[a-z]{2}))?$", re.I)
_DEV_RE = re.compile(
    r"apps\.apple\.com/(?:(?P<country>[a-z]{2})/)?developer/(?:[^/]+/)?id(?P<id>\d+)"
)
LOOKUP_URL = "https://itunes.apple.com/lookup"

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

    account_example = "https://apps.apple.com/cn/developer/wechat/id614694882"

    @classmethod
    def matches_account(cls, target: str) -> bool:
        return bool(_DEV_RE.search(target))

    async def resolve_account(self, target: str) -> AccountInfo:
        m = _DEV_RE.search(target)
        if not m:
            raise SourceError(f"Not an App Store developer URL: {target}")
        country = m["country"] or "us"
        results = await self._lookup({"id": m["id"], "entity": "software", "country": country})
        artist = next((r for r in results if r.get("wrapperType") == "artist"), {})
        return AccountInfo(
            platform=self.name,
            external_id=m["id"],
            name=artist.get("artistName"),
            url=f"https://apps.apple.com/{country}/developer/id{m['id']}",
            meta={"country": country},
        )

    async def resolve_work(self, target: str) -> tuple[AccountInfo, WorkInfo]:
        app_id, country = parse_target(target)
        results = await self._lookup({"id": app_id, "country": country})
        if not results:
            raise SourceError(f"App {app_id} not found in App Store region '{country}'")
        app = results[0]
        account = AccountInfo(
            platform=self.name,
            external_id=str(app.get("artistId")),
            name=app.get("artistName"),
            url=app.get("artistViewUrl"),
            meta={"country": country},
        )
        return account, _work_from_lookup(app, country)

    async def iter_works(self, account: AccountInfo) -> AsyncIterator[WorkInfo]:
        country = account.meta.get("country", "us")
        results = await self._lookup(
            {"id": account.external_id, "entity": "software", "country": country, "limit": 200}
        )
        apps = [r for r in results if r.get("wrapperType") == "software"]
        apps.sort(key=lambda a: a.get("releaseDate") or "", reverse=True)
        for app in apps:
            yield _work_from_lookup(app, country)

    async def iter_comments(
        self, work: WorkInfo, *, replies: bool = False
    ) -> AsyncIterator[Comment]:
        country = work.meta.get("country", "us")
        for page in range(1, MAX_PAGES + 1):
            entries = await self._feed_page(work.external_id, country, page)
            if not entries:
                if page == 1:  # RSS intermittently empty: fall back to reviews on the web page
                    comments, _ = await self._fetch_web(work.external_id, country)
                    comments.sort(key=lambda c: c.created_at or datetime.min, reverse=True)
                    for comment in comments:
                        yield comment
                return
            for entry in entries:
                if "im:rating" in entry:
                    yield _to_comment(entry, work.external_id, country)

    async def _lookup(self, params: dict) -> list[dict]:
        resp = await self.request("GET", LOOKUP_URL, params=params)
        if resp.status_code != 200:
            raise SourceError(f"App Store lookup returned HTTP {resp.status_code}")
        return resp.json().get("results") or []

    async def _feed_page(self, app_id: str, country: str, page: int) -> list[dict]:
        resp = await self.request("GET", FEED_URL.format(country=country, page=page, id=app_id))
        if resp.status_code != 200:
            return []
        try:
            entries = resp.json().get("feed", {}).get("entry") or []
        except ValueError:
            return []
        return [entries] if isinstance(entries, dict) else entries

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
            entries = await self._feed_page(app_id, country, page)
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
        resp = await self.request("GET", WEB_URL.format(country=country, id=app_id))
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
            kind="review",
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


def _work_from_lookup(app: dict, country: str) -> WorkInfo:
    released = app.get("releaseDate")
    return WorkInfo(
        platform="appstore",
        external_id=str(app["trackId"]),
        title=app.get("trackName"),
        url=app.get("trackViewUrl"),
        published_at=datetime.fromisoformat(released.replace("Z", "+00:00")) if released else None,
        meta={
            "country": country,
            "version": app.get("version"),
            "rating": app.get("averageUserRating"),
            "rating_count": app.get("userRatingCount"),
        },
    )


def _to_comment(entry: dict, app_id: str, country: str) -> Comment:
    updated = _label(entry, "updated")
    rating = _label(entry, "im:rating")
    votes = _label(entry, "im:voteSum")
    return Comment(
        id=_label(entry, "id") or "",
        platform="appstore",
        kind="review",
        author=(entry.get("author") or {}).get("name", {}).get("label"),
        title=_label(entry, "title"),
        content=_label(entry, "content") or "",
        rating=float(rating) if rating else None,
        likes=int(votes) if votes and votes.lstrip("-").isdigit() else None,
        created_at=datetime.fromisoformat(updated) if updated else None,
        version=_label(entry, "im:version"),
        url=f"https://apps.apple.com/{country}/app/id{app_id}?see-all=reviews",
    )
