from __future__ import annotations

import httpx
import respx

from echo_voc.models import AccountInfo
from echo_voc.sources import detect_link
from echo_voc.sources.appstore import AppStoreSource
from echo_voc.sources.youtube import YouTubeSource


def test_detect_account_links():
    assert detect_link("https://space.bilibili.com/486906719")[1] == "account"
    assert detect_link("https://www.youtube.com/@GoogleDevelopers/videos")[1] == "account"
    assert detect_link("https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw")[1] == "account"
    assert detect_link("https://apps.apple.com/cn/developer/wechat/id614694882")[1] == "account"
    assert detect_link("https://apps.apple.com/cn/app/wechat/id414478124")[1] == "work"


@respx.mock
async def test_appstore_developer_apps():
    respx.get("https://itunes.apple.com/lookup").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"wrapperType": "artist", "artistName": "WeChat"},
                    {
                        "wrapperType": "software",
                        "trackId": 1,
                        "trackName": "Old",
                        "releaseDate": "2011-01-01T00:00:00Z",
                    },
                    {
                        "wrapperType": "software",
                        "trackId": 2,
                        "trackName": "New",
                        "releaseDate": "2020-01-01T00:00:00Z",
                        "version": "2.0",
                    },
                ]
            },
        )
    )
    source = AppStoreSource()
    account = await source.resolve_account("https://apps.apple.com/cn/developer/wechat/id614694882")
    assert (account.name, account.meta) == ("WeChat", {"country": "cn"})
    works = [w async for w in source.iter_works(account)]
    assert [w.title for w in works] == ["New", "Old"]
    assert works[0].meta["country"] == "cn" and works[0].meta["version"] == "2.0"


@respx.mock
async def test_youtube_channel_uploads():
    respx.get("https://www.googleapis.com/youtube/v3/channels").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "UCabc",
                        "snippet": {"title": "Chan"},
                        "contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}},
                    }
                ]
            },
        )
    )

    def items(request):
        assert request.url.params["playlistId"] == "UUabc"
        token = request.url.params.get("pageToken")
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "snippet": {"title": f"t{token}"},
                        "contentDetails": {
                            "videoId": f"vid{token or 0}",
                            "videoPublishedAt": "2026-01-01T00:00:00Z",
                        },
                    }
                ],
                "nextPageToken": None if token else "1",
            },
        )

    respx.get("https://www.googleapis.com/youtube/v3/playlistItems").mock(side_effect=items)
    source = YouTubeSource(api_key="k")
    account = await source.resolve_account("https://www.youtube.com/@chan")
    assert account.external_id == "UCabc"
    works = [w async for w in source.iter_works(account)]
    assert [w.external_id for w in works] == ["vid0", "vid1"]
    # The uploads playlist id is cached in meta, so no extra channels call is needed.
    works = [w async for w in source.iter_works(AccountInfo(**account.model_dump()))]
    assert len(works) == 2
