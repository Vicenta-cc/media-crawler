"""A requested note count below Douyin's 15-item page size must survive the non-streaming path."""
from unittest.mock import AsyncMock

import pytest

import config
from media_platform.douyin.core import DouYinCrawler


def _fake_page(start: int, count: int = 15) -> dict:
    return {
        "data": [{"aweme_info": {"aweme_id": str(value)}} for value in range(start, start + count)],
        "extra": {"logid": f"log-{start}"},
    }


def _crawler(monkeypatch, offsets: list[int], pages: dict[int, dict], max_notes: int) -> DouYinCrawler:
    crawler = DouYinCrawler.__new__(DouYinCrawler)

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            offsets.append(offset)
            return pages[offset]

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    for name, value in (
        ("KEYWORDS", "keyword"),
        ("START_PAGE", 0),
        ("STREAM_ITEMS", False),
        ("CRAWLER_MAX_NOTES_COUNT", max_notes),
        ("CRAWLER_MAX_SLEEP_SEC", 0),
        ("DY_SEARCH_PAGE_SIZE", 15),
        ("DY_SKIP_AWEME_IDS_FILE", ""),
        ("DY_REUSABLE_CONTENT_DB", ""),
        ("SEARCH_RESUME_KEYWORD", ""),
        ("SEARCH_RESUME_PAGE", -1),
    ):
        monkeypatch.setattr(config, name, value)
    return crawler


@pytest.mark.asyncio
async def test_non_streaming_search_stops_at_the_requested_count_inside_one_page(monkeypatch):
    # 初筛候选要的是 10 条；一页回 15 条时多出来的 5 条既不能入库，也不能去拉评论
    offsets: list[int] = []
    crawler = _crawler(monkeypatch, offsets, {0: _fake_page(1)}, max_notes=10)
    stored = crawler.wait_for_content_slot

    await crawler.search()

    assert offsets == [0]
    assert stored.await_count == 10
    assert config.CRAWLER_MAX_NOTES_COUNT == 10      # 上限不再被页大小顶上去
    [comment_call] = crawler.batch_get_note_comments.await_args_list
    assert comment_call.args[0] == [str(value) for value in range(1, 11)]


@pytest.mark.asyncio
async def test_non_streaming_search_still_pages_past_a_full_page(monkeypatch):
    offsets: list[int] = []
    crawler = _crawler(monkeypatch, offsets, {0: _fake_page(1), 15: _fake_page(16)}, max_notes=16)

    await crawler.search()

    assert offsets == [0, 15]
    assert crawler.wait_for_content_slot.await_count == 16
    assert [len(call.args[0]) for call in crawler.batch_get_note_comments.await_args_list] == [15, 1]
