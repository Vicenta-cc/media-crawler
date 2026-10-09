import json
from unittest.mock import AsyncMock

import httpx
import pytest

import config
from media_platform.douyin import client as module
from media_platform.douyin.exception import DataFetchError


def client():
    value = module.DouYinClient.__new__(module.DouYinClient)
    value.headers = {"Cookie": "private-cookie", "Origin": "https://www.douyin.com/", "uifid": "stale"}
    value.cookie_dict = {"UIFID": "current", "UIFID_TEMP": "fallback", "s_v_web_id": "current-fp"}
    value._host = "https://www.douyin.com"
    value._DouYinClient__process_req_params = AsyncMock()
    return value


@pytest.mark.asyncio
async def test_creator_pages_refresh_identity_preserve_cursor_and_deduplicate(monkeypatch):
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 3)
    value = client()
    original_headers = value.headers.copy()
    calls = []

    async def send(method, url, **kwargs):
        calls.append((kwargs["params"].copy(), kwargs["headers"].copy()))
        if len(calls) == 1:
            value.cookie_dict = {"UIFID_TEMP": "refreshed", "s_v_web_id": "refreshed-fp"}
            payload = {"status_code": 0, "aweme_list": [{"aweme_id": "1"}], "has_more": 1, "max_cursor": 42}
        else:
            payload = {"status_code": 0, "aweme_list": [{"aweme_id": "1"}, {"aweme_id": "2"}], "has_more": 0}
        return httpx.Response(200, json=payload)

    value._send = send
    callback = AsyncMock()
    rows = await value.get_all_user_aweme_posts("creator-id", callback)
    assert [x["aweme_id"] for x in rows] == ["1", "2"]
    assert callback.await_count == 2
    assert calls[0][0]["max_cursor"] == ""
    assert calls[1][0]["max_cursor"] == 42
    for (params, headers), identity, fp in zip(calls, ["current", "refreshed"], ["current-fp", "refreshed-fp"]):
        assert params["uifid"] == headers["uifid"] == identity
        assert params["verifyFp"] == params["fp"] == fp
        assert headers["Referer"] == "https://www.douyin.com/user/creator-id"
        assert headers["x-tt-argus"] == "1"
        assert "Origin" not in headers
    assert value.headers == original_headers


@pytest.mark.asyncio
async def test_creator_does_not_overwrite_existing_argus_header():
    value = client()
    value.headers["x-tt-argus"] = "existing-value"
    value.get = AsyncMock(return_value={})
    await value.get_user_aweme_posts("creator-id")
    assert value.get.await_args.kwargs["headers"]["x-tt-argus"] == "existing-value"


@pytest.mark.asyncio
async def test_missing_identity_does_not_reuse_stale_header_or_make_one_up():
    value = client()
    value.cookie_dict = {}
    value.get = AsyncMock(return_value={})
    await value.get_user_aweme_posts("creator/id?token=private")
    params = value.get.await_args.args[1]
    headers = value.get.await_args.kwargs["headers"]
    assert not {"uifid", "verifyFp", "fp"}.intersection(params)
    assert "uifid" not in headers
    assert headers["Referer"].endswith("creator%2Fid%3Ftoken%3Dprivate")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,reason", [
    (403, "Blocked by ArgusSecurityPlugin Uifid Not Found", "gateway_missing_uifid"),
    (403, "Blocked by ArgusSecurityPlugin Signature Not Found", "gateway_missing_signature"),
    (403, "denied private-cookie secret-token", "http_forbidden"),
    (401, "unauthorized private-cookie", "http_unauthorized"),
    (200, "<html>captcha private-cookie</html>", "verification_signal"),
])
async def test_creator_denial_stops_without_retry_or_delivering_empty_success(status, body, reason):
    value = client()
    value._send = AsyncMock(return_value=httpx.Response(status, text=body))
    callback = AsyncMock()
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY") as error:
        await value.get_all_user_aweme_posts("private-creator", callback)
    value._send.assert_awaited_once()
    callback.assert_not_awaited()
    diagnostic = json.loads(str(error.value).split(": ", 1)[1])
    assert diagnostic["reason"] == reason
    assert diagnostic["endpoint"] == "creator_posts"
    assert diagnostic["http_status"] == status
    assert not any(x in str(error.value) for x in ["private-cookie", "secret-token", "private-creator"])


def test_diagnostic_never_copies_untrusted_status_or_url_query():
    result = module.DouYinClient._rejection_diagnostic(
        httpx.Response(403, text="private-response"),
        {"status_code": "secret-token"},
        "https://www.douyin.com/user/private-creator?token=secret-token",
    )
    assert result["platform_status"] is None
    assert result["endpoint"] == "other"
    assert "private" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
