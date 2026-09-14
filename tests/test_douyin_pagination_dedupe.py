from unittest.mock import AsyncMock

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

    await crawler.search()

    assert offsets == [0, 15]
    assert crawler.wait_for_content_slot.await_count == 16
    assert [call.args[0] for call in crawler.batch_get_note_comments.await_args_list] == [
        [str(value)] for value in range(1, 17)
    ]
