"""Search mode can enrich each stored item's author from the user profile API.

Douyin's search response carries a slimmed author object: `follower_count` is 0
and `signature` is absent even for accounts with tens of millions of followers.
The triage follower thresholds and signature rules therefore need one profile
request per author, which this opt-in flag performs.
"""

import logging
from typing import Dict, List
from unittest.mock import AsyncMock

import pytest

import config
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import DataFetchError

SEARCH_AUTHOR_A = {"uid": "uA", "sec_uid": "secA", "nickname": "新华社", "follower_count": 0}
SEARCH_AUTHOR_B = {"uid": "uB", "sec_uid": "secB", "nickname": "路人", "follower_count": 0}

PROFILES = {
    "secA": {
        "user": {
            "follower_count": 12345678,
            "max_follower_count": 22222222,
            "signature": "新华社官方账号",
            "custom_verify": "",
            "enterprise_verify_reason": "新华社",
            "verification_type": 1,
            "following_count": 10,
            "total_favorited": 999,
            "aweme_count": 100,
        }
    },
    "secB": {"user": {"follower_count": 12, "signature": "普通用户签名"}},
}


def _page() -> Dict:
    """One page whose first two items share an author, third has another."""
    return {
        "has_more": 0,
        "data": [
            {"aweme_info": {"aweme_id": "1", "author": dict(SEARCH_AUTHOR_A)}},
            {"aweme_info": {"aweme_id": "2", "author": dict(SEARCH_AUTHOR_A)}},
            {"aweme_info": {"aweme_id": "3", "author": dict(SEARCH_AUTHOR_B)}},
        ],
        "extra": {"logid": "log-0"},
    }


def _crawler(monkeypatch, stored: List[Dict], *, fetch_profile: bool, get_user_info) -> DouYinCrawler:
    crawler = DouYinCrawler.__new__(DouYinCrawler)

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            return _page()

    crawler.dy_client = FakeClient()
    crawler.dy_client.get_user_info = get_user_info
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()

    async def record(*, aweme_item):
        stored.append({"aweme_id": aweme_item.get("aweme_id"), "author": dict(aweme_item.get("author") or {})})

    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", record)
    for name, value in (
        ("KEYWORDS", "keyword"),
        ("START_PAGE", 0),
        ("STREAM_ITEMS", True),
        ("CRAWLER_MAX_NOTES_COUNT", 3),
        ("CRAWLER_MAX_SLEEP_SEC", 0),
        ("DY_SEARCH_PAGE_SIZE", 15),
        ("DY_SKIP_AWEME_IDS_FILE", ""),
        ("DY_REUSABLE_CONTENT_DB", ""),
        ("SEARCH_RESUME_KEYWORD", ""),
        ("SEARCH_RESUME_PAGE", -1),
        ("DY_FETCH_AUTHOR_PROFILE", fetch_profile),
    ):
        monkeypatch.setattr(config, name, value)
    return crawler


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_items", [True, False])
async def test_profile_is_fetched_once_per_author_and_reaches_the_store(monkeypatch, stream_items):
    stored: List[Dict] = []
    get_user_info = AsyncMock(side_effect=lambda sec_user_id: PROFILES[sec_user_id])
    crawler = _crawler(monkeypatch, stored, fetch_profile=True, get_user_info=get_user_info)
    monkeypatch.setattr(config, "STREAM_ITEMS", stream_items)

    await crawler.search()

    assert [call.args[0] for call in get_user_info.await_args_list] == ["secA", "secB"]
    assert [item["author"]["follower_count"] for item in stored] == [12345678, 12345678, 12]
    assert [item["author"]["signature"] for item in stored] == [
        "新华社官方账号",
        "新华社官方账号",
        "普通用户签名",
    ]
    assert stored[0]["author"]["max_follower_count"] == 22222222
    assert stored[0]["author"]["enterprise_verify_reason"] == "新华社"
    assert stored[0]["author"]["verification_type"] == 1
    assert stored[0]["author"]["nickname"] == "新华社"  # search fields survive


@pytest.mark.asyncio
async def test_disabled_flag_never_touches_the_profile_api(monkeypatch):
    stored: List[Dict] = []
    get_user_info = AsyncMock(side_effect=lambda sec_user_id: PROFILES[sec_user_id])
    crawler = _crawler(monkeypatch, stored, fetch_profile=False, get_user_info=get_user_info)

    await crawler.search()

    get_user_info.assert_not_awaited()
    assert [item["author"]["follower_count"] for item in stored] == [0, 0, 0]
    assert all("signature" not in item["author"] for item in stored)


@pytest.mark.asyncio
async def test_profile_failure_is_logged_and_the_item_is_still_stored(monkeypatch, caplog):
    stored: List[Dict] = []
    get_user_info = AsyncMock(side_effect=RuntimeError("signing_runtime_failed"))
    crawler = _crawler(monkeypatch, stored, fetch_profile=True, get_user_info=get_user_info)

    with caplog.at_level(logging.WARNING, logger="MediaCrawler"):
        await crawler.search()

    assert [item["aweme_id"] for item in stored] == ["1", "2", "3"]
    assert [item["author"]["follower_count"] for item in stored] == [0, 0, 0]
    assert any(
        "author profile fetch failed for secA" in record.message
        and "signing_runtime_failed" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_account_verify_error_from_the_profile_call_propagates(monkeypatch):
    stored: List[Dict] = []
    get_user_info = AsyncMock(side_effect=DataFetchError("ACCOUNT_VERIFY"))
    crawler = _crawler(monkeypatch, stored, fetch_profile=True, get_user_info=get_user_info)

    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await crawler.search()

    assert stored == []
