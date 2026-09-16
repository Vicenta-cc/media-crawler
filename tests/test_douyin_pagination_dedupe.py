from unittest.mock import AsyncMock
from pathlib import Path
import sqlite3
import json

import pytest

import config
from media_platform.douyin.client import DouYinClient
from media_platform.douyin.core import DouYinCrawler, reusable_aweme_is_complete
from media_platform.douyin.exception import DataFetchError
from tools.async_file_writer import AsyncFileWriter


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [DataFetchError('ACCOUNT_VERIFY'), RuntimeError('signing_runtime_failed')])
async def test_comment_failure_reaches_parent_before_post_completion(monkeypatch, error):
    crawler = DouYinCrawler()
    crawler.dy_client = type('Client', (), {})()
    crawler.dy_client.get_aweme_all_comments = AsyncMock(side_effect=error)
    monkeypatch.setattr(config, 'ENABLE_GET_COMMENTS', True)
    monkeypatch.setattr(config, 'MAX_CONCURRENCY_NUM', 1)
    with pytest.raises(type(error), match=str(error)):
        await crawler.batch_get_note_comments(['fixture'])


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
async def test_comment_jsonl_is_idempotent_across_writer_instances(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    first_writer = AsyncFileWriter(platform="douyin", crawler_type="search")
    resumed_writer = AsyncFileWriter(platform="douyin", crawler_type="search")

    assert await first_writer.write_to_jsonl(
        {"comment_id": "comment-1", "aweme_id": "post-1"},
        "comments",
        unique_key="comment_id",
    ) is True
    assert await resumed_writer.write_to_jsonl(
        {"comment_id": "comment-1", "aweme_id": "post-1"},
        "comments",
        unique_key="comment_id",
    ) is False
    assert await resumed_writer.write_to_jsonl(
        {"comment_id": "comment-2", "aweme_id": "post-1"},
        "comments",
        unique_key="comment_id",
    ) is True

    jsonl_path = Path(first_writer._get_file_path("jsonl", "comments"))
    rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert [row["comment_id"] for row in rows] == ["comment-1", "comment-2"]


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
async def test_stalled_root_cursor_keeps_current_page_subcomments():
    client = DouYinClient.__new__(DouYinClient)
    client.get_aweme_comments = AsyncMock(
        return_value={
            "has_more": 1,
            "cursor": 0,
            "comments": [{"cid": "root", "reply_comment_total": 1}],
        }
    )
    client.get_sub_comments = AsyncMock(
        return_value={
            "has_more": 0,
            "cursor": 1,
            "comments": [{"cid": "reply"}],
        }
    )

    result = await client.get_aweme_all_comments(
        "post", is_fetch_sub_comments=True, crawl_interval=0, max_count=5
    )

    assert [comment["cid"] for comment in result] == ["root", "reply"]
    client.get_aweme_comments.assert_awaited_once()
    client.get_sub_comments.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_start_page", "expected_offset"),
    [(0, 0), (1, 0), (2, 15)],
)
async def test_user_start_page_maps_to_douyin_offset_and_honors_has_more(
    monkeypatch, configured_start_page, expected_offset
):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    offsets = []

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            offsets.append(offset)
            return {
                "has_more": 0,
                "data": [{"aweme_info": {"aweme_id": "only"}}],
                "extra": {"logid": "final"},
            }

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    for name, value in (
        ("KEYWORDS", "keyword"),
        ("START_PAGE", configured_start_page),
        ("STREAM_ITEMS", True),
        ("CRAWLER_MAX_NOTES_COUNT", 2),
        ("CRAWLER_MAX_SLEEP_SEC", 0),
        ("DY_SEARCH_PAGE_SIZE", 15),
        ("DY_SKIP_AWEME_IDS_FILE", ""),
        ("DY_REUSABLE_CONTENT_DB", ""),
        ("SEARCH_RESUME_KEYWORD", ""),
        ("SEARCH_RESUME_PAGE", -1),
    ):
        monkeypatch.setattr(config, name, value)

    await crawler.search()

    assert offsets == [expected_offset]
    crawler.get_aweme_media.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_repeated_page_fails_after_no_progress_limit(monkeypatch):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    offsets = []

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            offsets.append(offset)
            return {
                "has_more": 1,
                "data": [{"aweme_info": {"aweme_id": "same"}}],
                "extra": {"logid": f"log-{offset}"},
            }

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    for name, value in (
        ("KEYWORDS", "keyword"),
        ("START_PAGE", 1),
        ("STREAM_ITEMS", True),
        ("CRAWLER_MAX_NOTES_COUNT", 2),
        ("CRAWLER_MAX_SLEEP_SEC", 0),
        ("DY_SEARCH_PAGE_SIZE", 15),
        ("DY_SEARCH_MAX_PAGES", 10),
        ("DY_SEARCH_MAX_NO_PROGRESS_PAGES", 2),
        ("DY_SKIP_AWEME_IDS_FILE", ""),
        ("DY_REUSABLE_CONTENT_DB", ""),
        ("SEARCH_RESUME_KEYWORD", ""),
        ("SEARCH_RESUME_PAGE", -1),
    ):
        monkeypatch.setattr(config, name, value)

    with pytest.raises(DataFetchError, match="search_pagination_stalled"):
        await crawler.search()

    assert offsets == [0, 15, 30]
    assert crawler.get_aweme_media.await_count == 1


@pytest.mark.asyncio
async def test_search_page_budget_bounds_unique_pages(monkeypatch):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    offsets = []

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            offsets.append(offset)
            return {
                "has_more": 1,
                "data": [{"aweme_info": {"aweme_id": str(offset)}}],
                "extra": {"logid": f"log-{offset}"},
            }

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    for name, value in (
        ("KEYWORDS", "keyword"),
        ("START_PAGE", 1),
        ("STREAM_ITEMS", True),
        ("CRAWLER_MAX_NOTES_COUNT", 10),
        ("CRAWLER_MAX_SLEEP_SEC", 0),
        ("DY_SEARCH_PAGE_SIZE", 15),
        ("DY_SEARCH_MAX_PAGES", 2),
        ("DY_SEARCH_MAX_NO_PROGRESS_PAGES", 3),
        ("DY_SKIP_AWEME_IDS_FILE", ""),
        ("DY_REUSABLE_CONTENT_DB", ""),
        ("SEARCH_RESUME_KEYWORD", ""),
        ("SEARCH_RESUME_PAGE", -1),
    ):
        monkeypatch.setattr(config, name, value)

    with pytest.raises(DataFetchError, match="search_page_budget_exceeded"):
        await crawler.search()

    assert offsets == [0, 15]


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
async def test_multi_keyword_resume_page_only_applies_to_checkpoint_keyword(monkeypatch):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    offsets = []

    class FakeClient:
        async def search_info_by_keyword(self, *, keyword, offset, publish_time, search_id):
            offsets.append((keyword, offset))
            return {
                "data": [{"aweme_info": {"aweme_id": f"{keyword}-{offset}"}}],
                "extra": {"logid": f"log-{keyword}-{offset}"},
            }

    crawler.dy_client = FakeClient()
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    for name, value in (
        ("KEYWORDS", "词一,词二"),
        ("START_PAGE", 0),
        ("SEARCH_RESUME_KEYWORD", "词一"),
        ("SEARCH_RESUME_PAGE", 3),
        ("STREAM_ITEMS", True),
        ("CRAWLER_MAX_NOTES_COUNT", 1),
        ("CRAWLER_MAX_SLEEP_SEC", 0),
        ("DY_SEARCH_PAGE_SIZE", 15),
        ("DY_SKIP_AWEME_IDS_FILE", ""),
        ("DY_REUSABLE_CONTENT_DB", ""),
    ):
        monkeypatch.setattr(config, name, value)

    await crawler.search()

    assert offsets == [("词一", 45), ("词二", 0)]


@pytest.mark.asyncio
async def test_search_propagates_api_failure(monkeypatch):
    crawler = DouYinCrawler.__new__(DouYinCrawler)

    class FakeClient:
        async def search_info_by_keyword(self, **kwargs):
            raise DataFetchError("ACCOUNT_VERIFY")

    crawler.dy_client = FakeClient()
    monkeypatch.setattr(config, "KEYWORDS", "keyword")
    monkeypatch.setattr(config, "START_PAGE", 0)
    monkeypatch.setattr(config, "STREAM_ITEMS", True)
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 1)
    monkeypatch.setattr(config, "DY_SEARCH_PAGE_SIZE", 15)
    monkeypatch.setattr(config, "DY_SKIP_AWEME_IDS_FILE", "")
    monkeypatch.setattr(config, "DY_REUSABLE_CONTENT_DB", "")

    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await crawler.search()


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
    task_root = tmp_path / "outputs" / "task-a"
    payload_path = task_root / "raw_items" / "dy_1.json"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_text(json.dumps({"item": {
        "aweme_id": "1", "aweme_url": "https://www.douyin.com/video/1",
        "note_download_url": "", "video_download_url": "https://cdn/video",
    }}), encoding="utf-8")
    video_path = task_root / "crawler" / "douyin" / "videos" / "1" / "video.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"\x00\x00\x00\x18ftypisomfixture")
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


def test_reusable_aweme_requires_detail_raw_payload_and_intact_media(tmp_path):
    task_root = tmp_path / "outputs" / "task-a"
    payload_path = task_root / "raw_items" / "dy_1.json"
    payload_path.parent.mkdir(parents=True)
    item = {
        "aweme_id": "1", "aweme_url": "https://www.douyin.com/video/1",
        "note_download_url": "", "video_download_url": "https://cdn/video",
    }
    payload_path.write_text(json.dumps({"item": item}), encoding="utf-8")
    video_path = task_root / "crawler" / "douyin" / "videos" / "1" / "video.mp4"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"\x00\x00\x00\x18ftypisomfixture")

    assert reusable_aweme_is_complete(str(payload_path), "1")
    video_path.write_bytes(b"damaged")
    assert not reusable_aweme_is_complete(str(payload_path), "1")
    video_path.write_bytes(b"\x00\x00\x00\x18ftypisomfixture")
    payload_path.write_text(json.dumps({"item": {"aweme_id": "1"}}), encoding="utf-8")
    assert not reusable_aweme_is_complete(str(payload_path), "1")
    payload_path.write_text("{damaged", encoding="utf-8")
    assert not reusable_aweme_is_complete(str(payload_path), "1")
