import json
from unittest.mock import AsyncMock

import pytest

from media_platform.douyin.client import DouYinClient
from media_platform.douyin.field import SearchSortType


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_type", "expected_filter"),
    [
        (SearchSortType.GENERAL, None),
        (SearchSortType.MOST_LIKE, {"sort_type": "1", "publish_time": "0"}),
        (SearchSortType.LATEST, {"sort_type": "2", "publish_time": "0"}),
    ],
)
async def test_douyin_search_sort_reaches_filter_selected(sort_type, expected_filter):
    client = DouYinClient.__new__(DouYinClient)
    client.headers = {"User-Agent": "test"}
    client.get = AsyncMock(return_value={"data": []})

    await client.search_info_by_keyword(keyword="bc料", sort_type=sort_type)

    query_params = client.get.await_args.args[1]
    if expected_filter is None:
        assert "filter_selected" not in query_params
        assert query_params["is_filter_search"] == "0"
    else:
        assert json.loads(query_params["filter_selected"]) == expected_filter
        assert query_params["is_filter_search"] == 1
