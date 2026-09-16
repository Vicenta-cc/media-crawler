import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import config
from media_platform.douyin.client import DouYinClient
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import (
    DataFetchError,
    MediaDownloadError,
    PlatformRateLimitedError,
)
from store import douyin as store
from tools.collection_status import CollectionIncompleteError


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    values = {
        "SAVE_DATA_PATH": str(tmp_path),
        "ENABLE_GET_MEIDAS": True,
        "ENABLE_GET_SUB_COMMENTS": True,
        "CRAWLER_MAX_NOTES_COUNT": 3,
        "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES": 3,
        "CRAWLER_MAX_SLEEP_SEC": 0,
        "KEYWORDS": "test",
        "START_PAGE": 0,
        "DY_SKIP_AWEME_IDS_FILE": "",
        "DY_REUSABLE_CONTENT_DB": "",
    }
    for name, value in values.items():
        monkeypatch.setattr(config, name, value)


def collection_status(tmp_path, stage):
    paths = (tmp_path / "douyin" / "collection_status").glob(f"{stage}-*.json")
    return [json.loads(path.read_text()) for path in paths]


def crawler():
    value = DouYinCrawler()
    value.dy_client = DouYinClient.__new__(DouYinClient)
    value.wait_for_content_slot = AsyncMock()
    value.batch_get_note_comments = AsyncMock()
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_failed_image_never_emits_content_and_keeps_source_indices(
    monkeypatch, tmp_path, stream
):
    monkeypatch.setattr(config, "STREAM_ITEMS", stream)
    value = crawler()
    post = {
        "aweme_id": "123",
        "images": [
            {"url_list": ["https://example.invalid/a"]},
            {"url_list": ["https://example.invalid/b"]},
        ],
    }
    value.dy_client.search_info_by_keyword = AsyncMock(
        return_value={"data": [{"aweme_info": post}]}
    )
    value.dy_client.get_aweme_media = AsyncMock(
        side_effect=[MediaDownloadError("403", 403), b"ok"]
    )
    sink = AsyncMock()
    images = AsyncMock()
    monkeypatch.setattr(store, "update_douyin_aweme", sink)
    monkeypatch.setattr(store, "update_dy_aweme_image", images)

    with pytest.raises(CollectionIncompleteError, match="COLLECTION_INCOMPLETE"):
        await value.search()

    sink.assert_not_awaited()
    value.batch_get_note_comments.assert_not_awaited()
    assert images.await_args.args == ("123", b"ok", "001.jpeg")
    failure = collection_status(tmp_path, "media")[0]
    assert failure["status"] == "failed"
    assert failure["failed_indices"] == [0]
    assert failure["expected"] == 2
    assert failure["succeeded"] == 1
    assert "https://" not in json.dumps(failure)

    value.dy_client.get_aweme_media = AsyncMock(return_value=b"ok")
    await value.get_aweme_media(post)
    assert collection_status(tmp_path, "media")[0]["status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", [DataFetchError("HTTP 503"), {}, {"aweme_id": "wrong"}]
)
async def test_detail_failure_is_recorded_and_never_falls_back(
    monkeypatch, tmp_path, outcome
):
    value = crawler()
    options = (
        {"side_effect": outcome}
        if isinstance(outcome, Exception)
        else {"return_value": outcome}
    )
    value.dy_client.get_video_by_id = AsyncMock(**options)
    value.get_aweme_media = AsyncMock()
    sink = AsyncMock()
    monkeypatch.setattr(store, "update_douyin_aweme", sink)

    with pytest.raises(CollectionIncompleteError):
        await value.process_creator_aweme_stream_item(
            {"aweme_id": "123"}, asyncio.Semaphore(1)
        )

    assert collection_status(tmp_path, "detail")[0]["status"] == "failed"
    value.get_aweme_media.assert_not_awaited()
    sink.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        PlatformRateLimitedError("PLATFORM_RATE_LIMITED"),
        asyncio.CancelledError(),
        DataFetchError("ACCOUNT_VERIFY"),
    ],
)
async def test_detail_keeps_control_errors(tmp_path, error):
    value = crawler()
    value.dy_client.get_video_by_id = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        await value.get_aweme_detail("123", asyncio.Semaphore(1))

    assert collection_status(tmp_path, "detail")[0]["status"] in {
        "failed",
        "interrupted",
    }


@pytest.mark.asyncio
async def test_creator_preserves_first_success_before_later_failure(
    monkeypatch, tmp_path
):
    value = crawler()
    value.dy_client.get_video_by_id = AsyncMock(
        side_effect=[{"aweme_id": "1"}, DataFetchError("503")]
    )
    value.get_aweme_media = AsyncMock()
    sink = AsyncMock()
    monkeypatch.setattr(store, "update_douyin_aweme", sink)

    with pytest.raises(CollectionIncompleteError):
        await value.fetch_creator_video_detail(
            [{"aweme_id": "1"}, {"aweme_id": "2"}, {"aweme_id": "3"}]
        )

    assert [
        call.kwargs["aweme_item"]["aweme_id"] for call in sink.await_args_list
    ] == ["1"]
    assert value.dy_client.get_video_by_id.await_count == 2


@pytest.mark.asyncio
async def test_video_missing_url_is_incomplete(tmp_path):
    value = crawler()
    with pytest.raises(CollectionIncompleteError):
        await value.get_aweme_media({"aweme_id": "123"})
    assert collection_status(tmp_path, "media")[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_creator_pagination_counts_unique_ids_and_keeps_final_page():
    client = DouYinClient.__new__(DouYinClient)
    client.get_user_aweme_posts = AsyncMock(
        side_effect=[
            {"has_more": 1, "max_cursor": 1, "aweme_list": [{"aweme_id": "a"}]},
            {
                "has_more": 0,
                "max_cursor": 2,
                "aweme_list": [{"aweme_id": "a"}, {"aweme_id": "b"}],
            },
        ]
    )
    callback = AsyncMock()

    result = await client.get_all_user_aweme_posts("creator", callback)

    assert [post["aweme_id"] for post in result] == ["a", "b"]
    assert [
        post["aweme_id"]
        for call in callback.await_args_list
        for post in call.args[0]
    ] == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages",
    [
        [{"has_more": 1, "max_cursor": "", "aweme_list": []}],
        [
            {"has_more": 1, "max_cursor": index, "aweme_list": []}
            for index in range(1, 4)
        ],
        [
            {
                "has_more": 1,
                "max_cursor": index,
                "aweme_list": [{"aweme_id": "a"}],
            }
            for index in (1, 2, 1)
        ],
    ],
)
async def test_creator_stall_fails_in_bounded_requests(pages):
    client = DouYinClient.__new__(DouYinClient)
    client.get_user_aweme_posts = AsyncMock(side_effect=pages)
    with pytest.raises(DataFetchError, match="pagination_stalled"):
        await client.get_all_user_aweme_posts("creator")
    assert client.get_user_aweme_posts.await_count == len(pages)


@pytest.mark.asyncio
async def test_creator_page_budget_and_exact_quota(monkeypatch):
    client = DouYinClient.__new__(DouYinClient)
    client.get_user_aweme_posts = AsyncMock(
        return_value={
            "has_more": 1,
            "max_cursor": 1,
            "aweme_list": [{"aweme_id": "a"}],
        }
    )
    monkeypatch.setattr(config, "DY_CREATOR_MAX_PAGES", 1)
    with pytest.raises(DataFetchError, match="page_budget"):
        await client.get_all_user_aweme_posts("creator")

    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 1)
    assert len(await client.get_all_user_aweme_posts("creator")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 2])
async def test_comment_budget_stops_requests_and_callbacks(limit):
    client = DouYinClient.__new__(DouYinClient)
    root = client.get_aweme_comments = AsyncMock(
        return_value={
            "has_more": 1,
            "cursor": 1,
            "comments": [{"cid": "r", "reply_comment_total": 10}],
        }
    )
    sub = client.get_sub_comments = AsyncMock(
        return_value={
            "has_more": 1,
            "cursor": 1,
            "comments": [{"cid": "s1"}, {"cid": "s2"}],
        }
    )
    callback = AsyncMock()

    result = await client.get_aweme_all_comments(
        "post", is_fetch_sub_comments=True, max_count=limit, callback=callback
    )

    assert len(result) == limit
    assert sum(len(call.args[1]) for call in callback.await_args_list) == limit
    assert root.await_count == int(limit > 0)
    assert sub.await_count == int(limit > 1)


@pytest.mark.asyncio
async def test_media_verification_and_cancellation_are_not_reclassified(
    tmp_path, monkeypatch
):
    value = crawler()
    value.dy_client.get_aweme_media = AsyncMock(
        side_effect=MediaDownloadError("403", 403)
    )
    value.dy_client.get_video_by_id = AsyncMock(
        side_effect=DataFetchError("ACCOUNT_VERIFY")
    )
    monkeypatch.setattr(config, "DY_MEDIA_REFRESH_ON_FAILURE", True)
    post = {
        "aweme_id": "123",
        "video": {
            "play_addr": {"url_list": ["https://example.invalid/video"]}
        },
    }

    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await value.get_aweme_media(post)

    value.dy_client.get_aweme_media = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await value.get_aweme_media(post)
    assert collection_status(tmp_path, "media")[0]["status"] == "interrupted"


def test_failure_reason_never_keeps_arbitrary_response_text():
    from tools.collection_status import failure_reason

    assert failure_reason(DataFetchError("private_token_123")) == "DataFetchError"
    assert failure_reason(DataFetchError("HTTP 503")) == "HTTP 503"
    assert (
        failure_reason(
            DataFetchError("COLLECTION_INCOMPLETE: creator_pagination_stalled")
        )
        == "creator_pagination_stalled"
    )


@pytest.mark.asyncio
async def test_creator_invalid_page_is_failure():
    client = DouYinClient.__new__(DouYinClient)
    client.get_user_aweme_posts = AsyncMock(return_value={"has_more": False})
    with pytest.raises(DataFetchError, match="invalid_creator_page"):
        await client.get_all_user_aweme_posts("creator")
