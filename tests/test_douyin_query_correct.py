import pytest

import config
from tests.test_douyin_response_detection import make_client


@pytest.mark.asyncio
async def test_search_uses_configured_query_correct_type(monkeypatch):
    seen = {}
    client = make_client()

    async def fake_get(uri, params=None, headers=None, **kwargs):
        seen["params"] = dict(params or {})
        return {"data": [], "extra": {}}

    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(config, "DY_QUERY_CORRECT_TYPE", 0, raising=False)
    await client.search_info_by_keyword(keyword="上分", offset=0)
    assert seen["params"]["query_correct_type"] == "0"
