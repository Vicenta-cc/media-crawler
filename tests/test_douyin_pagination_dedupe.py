from unittest.mock import AsyncMock
from pathlib import Path
import sqlite3
import json

import pytest

import config
from media_platform.douyin.client import DouYinClient
from media_platform.douyin.core import DouYinCrawler


@pytest.mark.asyncio
async def test_comment_pages_are_deduplicated_by_platform_cid():
    client = DouYinClient.__new__(DouYinClient)
    pages = iter([
        {"has_more": 1, "cursor": 20, "comments": [{"cid": "a"}, {"cid": "b"}]},
        {"has_more": 0, "cursor": 40, "comments": [{"cid": "b"}, {"cid": "c"}]},
    ])
    client.get_aweme_comments = AsyncMock(side_effect=lambda _aweme_id, _cursor: next(pages))
    callback = AsyncMock()

    result = await client.get_aweme_all_comments(
        "post", crawl_interval=0, callback=callback, max_count=10
    )

    assert [comment["cid"] for comment in result] == ["a", "b", "c"]
    assert [[comment["cid"] for comment in call.args[1]] for call in callback.await_args_list] == [
        ["a", "b"], ["c"]
    ]


@pytest.mark.asyncio
async def test_comment_empty_page_with_stalled_cursor_exits():
    client = DouYinClient.__new__(DouYinClient)
    client.get_aweme_comments = AsyncMock(
        return_value={"has_more": 1, "cursor": 0, "comments": []}
    )

    result = await client.get_aweme_all_comments("post", crawl_interval=0, max_count=10)

    assert result == []
    client.get_aweme_comments.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_uses_page_size_stride_and_unique_aweme_quota(monkeypatch):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    offsets = []

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            offsets.append(offset)
            start = 1 if offset == 0 else 15
            return {
                "data": [{"aweme_info": {"aweme_id": str(value)}} for value in range(start, start + 15)],
                "extra": {"logid": f"log-{offset}"},
            }

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()

    async def save_aweme(*, aweme_item):
        return None

    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", save_aweme)
    monkeypatch.setattr(config, "KEYWORDS", "keyword")
    monkeypatch.setattr(config, "START_PAGE", 0)
    monkeypatch.setattr(config, "STREAM_ITEMS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 16)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "DY_SEARCH_PAGE_SIZE", 15)
    monkeypatch.setattr(config, "DY_REUSABLE_CONTENT_DB", "")

    await crawler.search()

    assert offsets == [0, 15]
    assert crawler.wait_for_content_slot.await_count == 16
    assert [call.args[0] for call in crawler.batch_get_note_comments.await_args_list] == [
        [str(value)] for value in range(1, 17)
    ]


@pytest.mark.asyncio
async def test_search_skips_reusable_aweme_before_detail_media_or_comments(tmp_path, monkeypatch):
    ids_file = tmp_path / "reusable.txt"
    ids_file.write_text("1\n", encoding="utf-8")
    crawler = DouYinCrawler.__new__(DouYinCrawler)

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            return {"data": [{"aweme_info": {"aweme_id": "1"}}, {"aweme_info": {"aweme_id": "2"}}], "extra": {"logid": "x"}}

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    monkeypatch.setattr(config, "KEYWORDS", "keyword")
    monkeypatch.setattr(config, "START_PAGE", 0)
    monkeypatch.setattr(config, "STREAM_ITEMS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 1)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    monkeypatch.setattr(config, "DY_SEARCH_PAGE_SIZE", 15)
    monkeypatch.setattr(config, "DY_SKIP_AWEME_IDS_FILE", str(ids_file))
    monkeypatch.setattr(config, "DY_REUSABLE_CONTENT_DB", "")

    await crawler.search()

    crawler.get_aweme_media.assert_awaited_once()
    assert crawler.get_aweme_media.await_args.kwargs["aweme_item"]["aweme_id"] == "2"


@pytest.mark.asyncio
async def test_search_uses_indexed_reusable_content_db(tmp_path, monkeypatch):
    db = tmp_path / "audit.sqlite3"
    payload_path = tmp_path / "aweme-1.json"
    payload_path.write_text(json.dumps({"item": {"aweme_id": "1"}}), encoding="utf-8")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE contents (platform TEXT, content_key TEXT, collection_status TEXT, raw_item_path TEXT)")
        conn.execute("INSERT INTO contents VALUES ('dy', '1', 'complete', ?)", (str(payload_path),))
    crawler = DouYinCrawler.__new__(DouYinCrawler)

    class FakeClient:
        async def search_info_by_keyword(self, **kwargs):
            return {"data": [{"aweme_info": {"aweme_id": "1"}}, {"aweme_info": {"aweme_id": "2"}}], "extra": {"logid": "x"}}

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    for name, value in (("KEYWORDS", "keyword"), ("START_PAGE", 0), ("STREAM_ITEMS", True), ("CRAWLER_MAX_NOTES_COUNT", 1), ("CRAWLER_MAX_SLEEP_SEC", 0), ("DY_SEARCH_PAGE_SIZE", 15), ("DY_SKIP_AWEME_IDS_FILE", "")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "DY_REUSABLE_CONTENT_DB", str(db))

    await crawler.search()

    crawler.get_aweme_media.assert_awaited_once()
    assert crawler.get_aweme_media.await_args.kwargs["aweme_item"]["aweme_id"] == "2"
