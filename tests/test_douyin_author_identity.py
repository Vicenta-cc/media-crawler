"""Douyin author identity fields (verification + follower count) reach the store."""

from typing import Dict, List

import pytest

from database.models import DouyinAweme
from store import douyin as douyin_store

IDENTITY_FIELDS = (
    "custom_verify",
    "enterprise_verify_reason",
    "follower_count",
    "verification_type",
)


class RecordingStore:
    def __init__(self, sink: List[Dict]):
        self.sink = sink

    async def store_content(self, content_item: Dict):
        self.sink.append(content_item)


@pytest.fixture
def stored(monkeypatch) -> List[Dict]:
    sink: List[Dict] = []
    monkeypatch.setattr(
        douyin_store.DouyinStoreFactory,
        "create_store",
        staticmethod(lambda: RecordingStore(sink)),
    )
    return sink


def aweme(author: Dict) -> Dict:
    return {
        "aweme_id": "7412345678901234567",
        "aweme_type": 0,
        "desc": "测试作品",
        "create_time": 1758470400,
        "author": author,
        "statistics": {"digg_count": 12, "collect_count": 3, "comment_count": 4, "share_count": 5},
    }


@pytest.mark.asyncio
async def test_author_identity_fields_are_stored(stored):
    await douyin_store.update_douyin_aweme(
        aweme(
            {
                "uid": "u1",
                "sec_uid": "sec1",
                "nickname": "某某网",
                "custom_verify": "知名媒体人",
                "enterprise_verify_reason": "某某网络科技有限公司",
                "follower_count": 1708,
                "verification_type": 1,
            }
        )
    )

    item = stored[0]
    assert item["custom_verify"] == "知名媒体人"
    assert item["enterprise_verify_reason"] == "某某网络科技有限公司"
    assert item["follower_count"] == "1708"
    assert item["verification_type"] == "1"


@pytest.mark.asyncio
async def test_author_without_identity_fields_yields_empty_strings(stored):
    await douyin_store.update_douyin_aweme(aweme({"uid": "u2", "nickname": "路人"}))

    item = stored[0]
    for field in IDENTITY_FIELDS:
        assert item[field] == "", field


@pytest.mark.asyncio
async def test_missing_author_object_does_not_raise(stored):
    payload = aweme({})
    payload.pop("author")

    await douyin_store.update_douyin_aweme(payload)

    item = stored[0]
    for field in IDENTITY_FIELDS:
        assert item[field] == "", field


@pytest.mark.asyncio
async def test_stored_item_maps_onto_the_db_model(stored):
    """The DB backend does DouyinAweme(**content_item), so every key needs a column."""
    await douyin_store.update_douyin_aweme(
        aweme({"uid": "u3", "custom_verify": "黄V", "follower_count": 600000})
    )

    row = DouyinAweme(**stored[0])

    assert row.custom_verify == "黄V"
    assert row.follower_count == "600000"
