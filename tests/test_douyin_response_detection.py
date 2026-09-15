import httpx
import pytest

import config
from media_platform.douyin import client as client_module
from media_platform.douyin.exception import DataFetchError


def make_client():
    return client_module.DouYinClient(
        headers={"User-Agent": "browser-test"},
        playwright_page=None,
        cookie_dict={},
    )


@pytest.fixture(autouse=True)
def disable_persistent_gate(monkeypatch):
    monkeypatch.setattr(config, "DY_REQUEST_SCHEDULER_DB", "")


def install_response(monkeypatch, *, status=200, payload=None, text=None):
    def handler(_request):
        if payload is not None:
            return httpx.Response(status, json=payload)
        return httpx.Response(status, text=text or "")

    monkeypatch.setattr(
        client_module,
        "make_async_client",
        lambda **_kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_valid_post_verification_fields_do_not_trigger_account_verify(monkeypatch):
    payload = {
        "status_code": 0,
        "data": [{
            "aweme_info": {
                "desc": "user-authored text mentioning verify",
                "author": {
                    "custom_verify": "verified creator",
                    "enterprise_verify_reason": "business",
                },
            },
        }],
    }
    install_response(monkeypatch, payload=payload)

    assert await make_client().request("GET", "https://www.douyin.com/search") == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "text"),
    [
        (403, {"status_code": 0}, None),
        (200, {"status_code": 1105, "status_msg": "captcha verification required"}, None),
        (200, {
            "status_code": 0, "cursor": 0, "has_more": 0, "data": [],
            "search_nil_info": {
                "search_nil_type": "verify_check", "is_load_more": "first_flush",
                "search_nil_item": "verify_check", "text_type": 9,
            },
        }, None),
        (200, None, "<html>captcha verify</html>"),
    ],
)
async def test_protected_responses_emit_account_verify(monkeypatch, status, payload, text):
    install_response(monkeypatch, status=status, payload=payload, text=text)

    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await make_client().request("GET", "https://www.douyin.com/search")


@pytest.mark.asyncio
async def test_rate_limit_is_not_mislabeled_as_account_verification(monkeypatch):
    install_response(monkeypatch, status=429, payload={"status_code": 0})

    with pytest.raises(DataFetchError, match="HTTP 429"):
        await make_client().request("GET", "https://www.douyin.com/search")


@pytest.mark.asyncio
async def test_normal_empty_search_is_not_mislabeled_as_verification(monkeypatch):
    payload = {
        "status_code": 0, "cursor": 0, "has_more": 0, "data": [],
        "search_nil_info": {"search_nil_type": "no_result", "text": "验证方法"},
    }
    install_response(monkeypatch, payload=payload)

    assert await make_client().request("GET", "https://www.douyin.com/search") == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["search_nil_type", "search_nil_item"])
async def test_each_explicit_nil_challenge_field_is_sufficient(monkeypatch, field):
    install_response(monkeypatch, payload={
        "status_code": 0, "data": [], "search_nil_info": {field: "verify_check"},
    })

    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await make_client().request("GET", "https://www.douyin.com/search")
