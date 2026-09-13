from __future__ import annotations

import json

import httpx
import pytest
import respx

from echo_voc.errors import ConfigError, SourceError
from echo_voc.sources import resolve_source
from echo_voc.sources.appstore import AppStoreSource, parse_target
from echo_voc.sources.bilibili import BilibiliSource
from echo_voc.sources.youtube import YouTubeSource, parse_video_id


@pytest.mark.parametrize(
    ("target", "platform"),
    [
        ("https://apps.apple.com/cn/app/wechat/id414478124", "appstore"),
        ("https://apps.apple.com/app/id414478124", "appstore"),
        ("appstore:414478124@jp", "appstore"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1", "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube"),
        ("https://www.bilibili.com/video/BV1GJ411x7h7/?spm=1", "bilibili"),
        ("https://b23.tv/BV1GJ411x7h7", "bilibili"),
        ("av80433022", "bilibili"),
    ],
)
def test_auto_detect(target, platform):
    assert resolve_source(target).name == platform


def test_unknown_target():
    with pytest.raises(SourceError):
        resolve_source("https://example.com")


def test_parsers():
    assert parse_target("https://apps.apple.com/cn/app/wechat/id414478124") == ("414478124", "cn")
    assert parse_target("https://apps.apple.com/app/id1") == ("1", "us")
    assert parse_video_id("https://www.youtube.com/watch?feature=x&v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def _feed_entry(i: int) -> dict:
    return {
        "author": {"name": {"label": f"user{i}"}},
        "updated": {"label": "2026-09-11T17:56:53-07:00"},
        "im:rating": {"label": "2"},
        "im:version": {"label": "8.0"},
        "im:voteSum": {"label": "3"},
        "id": {"label": str(i)},
        "title": {"label": "t"},
        "content": {"label": f"review {i}"},
    }


@respx.mock
async def test_appstore_feed_pagination():
    def page(request):
        n = int(str(request.url).split("page=")[1].split("/")[0])
        entries = [_feed_entry(n * 100 + i) for i in range(50)] if n <= 2 else []
        return httpx.Response(200, json={"feed": {"entry": entries}})

    respx.get(url__regex=r"https://itunes\.apple\.com/cn/rss/customerreviews/.*").mock(
        side_effect=page
    )
    result = await AppStoreSource().fetch("appstore:1@cn", limit=70)
    assert len(result.comments) == 70
    c = result.comments[0]
    assert (c.rating, c.likes, c.version, c.author) == (2.0, 3, "8.0", "user100")


@respx.mock
async def test_appstore_falls_back_to_web_page():
    respx.get(url__regex=r"https://itunes\.apple\.com/.*").mock(
        return_value=httpx.Response(200, json={"feed": {}})
    )
    server_data = {
        "data": [
            {
                "items": [
                    {
                        "review": {
                            "$kind": "Review",
                            "id": "9",
                            "title": "Hi",
                            "contents": "Nice",
                            "rating": "5",
                            "reviewerName": "a",
                            "date": "2026-06-27T10:16:10.000Z",
                        }
                    },
                ]
            }
        ]
    }
    html = (
        "<title>Demo - App Store</title>"
        f'<script type="application/json" id="serialized-server-data">{json.dumps(server_data)}'
        "</script>"
    )
    respx.get(url__startswith="https://apps.apple.com/").mock(
        return_value=httpx.Response(200, text=html)
    )
    result = await AppStoreSource().fetch("appstore:1")
    assert [c.content for c in result.comments] == ["Nice"]
    assert result.title == "Demo - App Store"


@respx.mock
async def test_bilibili_cursor_pagination():
    respx.get("https://api.bilibili.com/x/web-interface/view").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"aid": 42, "bvid": "BV1GJ411x7h7", "title": "Video"}}
        )
    )

    def replies(request):
        cursor = int(request.url.params["next"])
        start = max(cursor, 1)
        items = [
            {
                "rpid": start * 100 + i,
                "ctime": 1700000000,
                "like": i,
                "member": {"uname": "u"},
                "content": {"message": f"m{start}-{i}"},
            }
            for i in range(20)
        ]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"replies": items, "cursor": {"next": start + 1, "is_end": start >= 2}},
            },
        )

    respx.get("https://api.bilibili.com/x/v2/reply/main").mock(side_effect=replies)
    result = await BilibiliSource().fetch("https://www.bilibili.com/video/BV1GJ411x7h7", limit=100)
    assert result.title == "Video"
    assert len(result.comments) == 40
    assert len({c.id for c in result.comments}) == 40


@respx.mock
async def test_bilibili_risk_control_message():
    respx.get("https://api.bilibili.com/x/web-interface/view").mock(
        return_value=httpx.Response(200, json={"code": -352, "message": "-352"})
    )
    with pytest.raises(SourceError, match="BILIBILI_COOKIE"):
        await BilibiliSource().fetch("BV1GJ411x7h7")


async def test_youtube_requires_key(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        await YouTubeSource().fetch("https://youtu.be/dQw4w9WgXcQ")


@respx.mock
async def test_youtube_pagination():
    respx.get("https://www.googleapis.com/youtube/v3/videos").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "dQw4w9WgXcQ", "snippet": {"title": "Vid"}}]}
        )
    )

    def threads(request):
        token = request.url.params.get("pageToken")
        items = [
            {
                "id": f"{token or 'p1'}-{i}",
                "snippet": {
                    "topLevelComment": {
                        "snippet": {
                            "authorDisplayName": "a",
                            "textOriginal": "hello",
                            "likeCount": 1,
                            "publishedAt": "2026-01-01T00:00:00Z",
                        }
                    }
                },
            }
            for i in range(int(request.url.params["maxResults"]))
        ]
        return httpx.Response(200, json={"items": items, "nextPageToken": None if token else "p2"})

    respx.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(side_effect=threads)
    result = await YouTubeSource(api_key="k").fetch("https://youtu.be/dQw4w9WgXcQ", limit=150)
    assert result.title == "Vid"
    assert len(result.comments) == 150
