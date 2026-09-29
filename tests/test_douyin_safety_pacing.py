"""Account-safety pacing for Douyin: per-account gate, jitter, silent risk, no retry."""

import asyncio
import logging
from typing import Dict, List
from unittest.mock import AsyncMock

import httpx
import pytest

import config
from cmd_arg import parse_cmd
from media_platform.douyin import client as client_module
from media_platform.douyin import pacing
from media_platform.douyin.client import DouYinClient
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import DataFetchError, PlatformRateLimitedError
from tools import persistent_request_gate as gate_module
from tools.persistent_request_gate import RequestScheduler, jitter_factor


def make_client():
    return DouYinClient(headers={"User-Agent": "test"}, playwright_page=None, cookie_dict={})


def install_transport(monkeypatch, handler):
    monkeypatch.setattr(
        client_module,
        "make_async_client",
        lambda **_kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# --- 1. per-account gate -------------------------------------------------------

def test_account_gate_is_absent_without_account_id(monkeypatch):
    monkeypatch.delenv("MEDIACRAWLER_ACCOUNT_ID", raising=False)
    assert make_client()._account_gate is None
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT_ID", "  ")
    assert make_client()._account_gate is None


def test_account_gate_uses_account_key_and_run_limits_win(monkeypatch):
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT_ID", "acc-1")
    monkeypatch.setattr(config, "DY_ACCOUNT_MIN_INTERVAL", 3.0)
    monkeypatch.setattr(config, "DY_ACCOUNT_REQUESTS_PER_MINUTE", 20)
    first = make_client()
    state = first._account_gate.state
    assert state.platform == "dy:account:acc-1"
    assert state.db_path == first._persistent_gate.state.db_path
    assert first._persistent_gate.state.platform == "dy"

    # A later run for a new / recently-blocked account passes lower limits.
    monkeypatch.setattr(config, "DY_ACCOUNT_MIN_INTERVAL", 8.0)
    monkeypatch.setattr(config, "DY_ACCOUNT_REQUESTS_PER_MINUTE", 6)
    second = make_client()
    policy = second._account_gate.state.policy()
    assert policy[0] == 8.0 and policy[1] == 6


def test_platform_policy_is_still_fail_closed(tmp_path):
    RequestScheduler(tmp_path / "gate.sqlite3")
    with pytest.raises(ValueError, match="policy differs"):
        RequestScheduler(tmp_path / "gate.sqlite3", min_interval=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["api", "media"])
async def test_every_request_passes_both_gates(monkeypatch, operation):
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT_ID", "acc-1")
    install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"}),
    )
    c = make_client()
    await c._send("GET", "https://fixture.invalid/x", operation=operation)
    await c._send("GET", "https://fixture.invalid/y", operation=operation)
    assert c._account_gate.state.snapshot()["request_count_last_minute"] == 2
    assert c._persistent_gate.state.snapshot()["request_count_last_minute"] == 2


@pytest.mark.asyncio
async def test_account_limit_blocks_even_when_platform_is_free(monkeypatch):
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT_ID", "acc-1")
    monkeypatch.setattr(config, "DY_ACCOUNT_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(config, "DY_ACCOUNT_REQUESTS_PER_MINUTE", 1)
    install_transport(monkeypatch, lambda _req: httpx.Response(200, json={}))
    c = make_client()
    await c._send("GET", "https://fixture.invalid/x")
    with pytest.raises(RuntimeError, match="REQUEST_SCHEDULER_WAIT_EXCEEDED"):
        await asyncio.wait_for(c._account_gate._reserve("api", max_wait=0.1), 2)


# --- 2. jitter ---------------------------------------------------------------

@pytest.mark.parametrize("jitter", [0.1, 0.4, 0.8])
def test_jitter_factor_bounds(jitter):
    samples = [jitter_factor(jitter) for _ in range(500)]
    assert min(samples) >= max(0.5, 1 - jitter)
    assert max(samples) <= 1 + jitter
    assert len(set(samples)) > 1


def test_zero_jitter_is_deterministic():
    assert {jitter_factor(0) for _ in range(20)} == {1.0}


@pytest.mark.parametrize(("uniform", "expected"), [(0.6, 1.2), (1.4, 2.8), (0.1, 1.0)])
def test_gate_min_interval_wait_is_jittered(tmp_path, monkeypatch, uniform, expected):
    monkeypatch.setattr(gate_module.random, "uniform", lambda a, b: uniform)
    now = [100.0]
    state = RequestScheduler(
        tmp_path / "gate.sqlite3", min_interval=2, per_minute=1000, jitter=0.9 if uniform < 0.5 else 0.4,
        clock=lambda: now[0],
    )
    assert state.try_acquire()[0] == 0
    assert state.try_acquire()[0] == pytest.approx(expected)


def test_gate_without_jitter_keeps_exact_interval(tmp_path):
    now = [100.0]
    state = RequestScheduler(tmp_path / "gate.sqlite3", min_interval=2, per_minute=1000, clock=lambda: now[0])
    state.try_acquire()
    assert state.try_acquire()[0] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_jittered_sleep(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(pacing.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(gate_module.random, "uniform", lambda a, b: b)
    monkeypatch.setattr(config, "DY_PACING_JITTER", 0.4)
    assert await pacing.jittered_sleep(2) == pytest.approx(2.8)
    monkeypatch.setattr(config, "DY_PACING_JITTER", 0)
    assert await pacing.jittered_sleep(2) == 2
    assert slept == [pytest.approx(2.8), 2]


# --- 3. silent risk ------------------------------------------------------------

def bare_client(payload):
    c = DouYinClient.__new__(DouYinClient)
    c.cookie_dict = {}
    c.headers = {}
    c.get = AsyncMock(return_value=payload)
    return c


@pytest.mark.asyncio
async def test_detail_without_aweme_detail_twice_is_verification(caplog):
    c = bare_client({"status_code": 0})
    with caplog.at_level(logging.WARNING):
        assert await c.get_video_by_id("1") == {}
    assert "aweme_detail" in caplog.text
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY: silent empty responses"):
        await c.get_video_by_id("2")


@pytest.mark.asyncio
async def test_profile_without_user_counts_and_mixes_with_detail():
    empty_profile = bare_client({"status_code": 0})
    await empty_profile.get_user_info("sec")
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await bare_client({"status_code": 0, "aweme_detail": None}).get_video_by_id("1")


@pytest.mark.asyncio
async def test_normal_response_resets_streak():
    await bare_client({"status_code": 0}).get_video_by_id("1")
    await bare_client({"status_code": 0, "user": {"uid": "u"}}).get_user_info("sec")
    await bare_client({"status_code": 0}).get_video_by_id("2")
    assert pacing.silent_risk_streak() == 1


@pytest.mark.asyncio
async def test_comment_list_empty_is_not_a_signal():
    c = DouYinClient.__new__(DouYinClient)
    c.get_aweme_comments = AsyncMock(return_value={"status_code": 0, "comments": [], "has_more": 0, "cursor": 0})
    for _ in range(3):
        assert await c.get_aweme_all_comments("1") == []
    assert pacing.silent_risk_streak() == 0


def search_crawler(monkeypatch, pages: List[Dict]):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    crawler.dy_client = type("FakeClient", (), {})()
    crawler.dy_client.search_info_by_keyword = AsyncMock(side_effect=pages)
    crawler.wait_for_content_slot = AsyncMock()
    crawler.get_aweme_media = AsyncMock()
    crawler.batch_get_note_comments = AsyncMock()
    crawler._enrich_author_profile = AsyncMock()
    monkeypatch.setattr("media_platform.douyin.core.douyin_store.update_douyin_aweme", AsyncMock())
    for name, value in (
        ("KEYWORDS", "keyword"), ("START_PAGE", 0), ("STREAM_ITEMS", True),
        ("CRAWLER_MAX_NOTES_COUNT", 50), ("CRAWLER_MAX_SLEEP_SEC", 0), ("DY_SEARCH_PAGE_SIZE", 15),
        ("DY_SKIP_AWEME_IDS_FILE", ""), ("DY_REUSABLE_CONTENT_DB", ""),
        ("SEARCH_RESUME_KEYWORD", ""), ("SEARCH_RESUME_PAGE", -1),
    ):
        monkeypatch.setattr(config, name, value)
    return crawler


def search_page(ids, has_more):
    return {"status_code": 0, "has_more": has_more, "extra": {"logid": "l"},
            "data": [{"aweme_info": {"aweme_id": i, "author": {}}} for i in ids]}


@pytest.mark.asyncio
async def test_search_first_page_empty_is_not_a_signal(monkeypatch):
    crawler = search_crawler(monkeypatch, [{"status_code": 0, "data": []}])
    await crawler.search()
    assert pacing.silent_risk_streak() == 0


@pytest.mark.asyncio
async def test_search_empty_after_has_more_is_a_signal(monkeypatch):
    crawler = search_crawler(monkeypatch, [search_page(["1"], 1), {"status_code": 0, "data": [], "has_more": 0}])
    await crawler.search()
    assert pacing.silent_risk_streak() == 1
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await bare_client({"status_code": 0}).get_video_by_id("9")


@pytest.mark.asyncio
async def test_search_signal_after_prior_signal_raises(monkeypatch):
    crawler = search_crawler(monkeypatch, [search_page(["1"], 1), {"status_code": 0, "data": []}])
    crawler.batch_get_note_comments = AsyncMock(side_effect=lambda ids: pacing.record_silent_risk("aweme_detail"))
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY: silent empty responses"):
        await crawler.search()


@pytest.mark.asyncio
async def test_search_non_empty_page_resets_streak(monkeypatch):
    pacing.record_silent_risk("aweme_detail")
    crawler = search_crawler(monkeypatch, [search_page(["1"], 0)])
    await crawler.search()
    assert pacing.silent_risk_streak() == 0


# --- 4. no retry after a risk signal --------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    DataFetchError("ACCOUNT_VERIFY: silent empty responses"),
    DataFetchError("ACCOUNT_VERIFY"),
    PlatformRateLimitedError("PLATFORM_RATE_LIMITED"),
])
async def test_detail_mode_stops_other_posts_after_risk(monkeypatch, tmp_path, error):
    monkeypatch.setattr(config, "SAVE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "DY_SPECIFIED_ID_LIST", ["1", "2", "3"])
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(config, "CRAWLER_MAX_SLEEP_SEC", 0)
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    crawler.dy_client = type("FakeClient", (), {})()
    crawler.dy_client.get_video_by_id = AsyncMock(side_effect=error)
    crawler.batch_get_note_comments = AsyncMock()
    crawler.wait_for_content_slot = AsyncMock()
    with pytest.raises(type(error)):
        await crawler.get_specified_awemes()
    for _ in range(20):  # let any leaked sibling task run
        await asyncio.sleep(0)
    assert crawler.dy_client.get_video_by_id.await_count == 1
    crawler.batch_get_note_comments.assert_not_awaited()


@pytest.mark.asyncio
async def test_comment_batch_stops_other_posts_after_risk(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_GET_COMMENTS", True)
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    crawler.dy_client = type("FakeClient", (), {})()
    crawler.dy_client.get_aweme_all_comments = AsyncMock(side_effect=DataFetchError("ACCOUNT_VERIFY"))
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await crawler.batch_get_note_comments(["1", "2", "3"])
    for _ in range(20):
        await asyncio.sleep(0)
    assert crawler.dy_client.get_aweme_all_comments.await_count == 1


@pytest.mark.asyncio
async def test_author_profile_propagates_silent_risk(monkeypatch):
    monkeypatch.setattr(config, "DY_FETCH_AUTHOR_PROFILE", True)
    monkeypatch.setattr(config, "DY_SKIP_PROFILE_VERIFY_REGEX", "")
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    crawler.dy_client = type("FakeClient", (), {})()
    crawler.dy_client.get_user_info = AsyncMock(side_effect=DataFetchError("ACCOUNT_VERIFY: silent empty responses"))
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await crawler._enrich_author_profile({"author": {"sec_uid": "s"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_http_401_403_is_verification(monkeypatch, status):
    install_transport(monkeypatch, lambda _req: httpx.Response(status, json={"status_code": 0}))
    with pytest.raises(DataFetchError, match="ACCOUNT_VERIFY"):
        await make_client().request("GET", "https://fixture.invalid/api")


# --- 5. skip profile for official accounts --------------------------------------

def profile_crawler(get_user_info):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    crawler.dy_client = type("FakeClient", (), {})()
    crawler.dy_client.get_user_info = get_user_info
    return crawler


@pytest.mark.asyncio
async def test_profile_skipped_when_verify_reason_matches(monkeypatch):
    monkeypatch.setattr(config, "DY_FETCH_AUTHOR_PROFILE", True)
    monkeypatch.setattr(config, "DY_SKIP_PROFILE_VERIFY_REGEX", "人民日报|新华社")
    fetch = AsyncMock(return_value={"user": {"follower_count": 5}})
    crawler = profile_crawler(fetch)
    await crawler._enrich_author_profile({"author": {"sec_uid": "a", "enterprise_verify_reason": "新华社官方账号"}})
    fetch.assert_not_awaited()
    await crawler._enrich_author_profile({"author": {"sec_uid": "b", "enterprise_verify_reason": "某公司"}})
    await crawler._enrich_author_profile({"author": {"sec_uid": "c"}})
    assert [call.args[0] for call in fetch.await_args_list] == ["b", "c"]


@pytest.mark.asyncio
async def test_invalid_skip_regex_warns_once_and_fetches(monkeypatch, caplog):
    monkeypatch.setattr(config, "DY_FETCH_AUTHOR_PROFILE", True)
    monkeypatch.setattr(config, "DY_SKIP_PROFILE_VERIFY_REGEX", "(unclosed")
    fetch = AsyncMock(return_value={"user": {}})
    crawler = profile_crawler(fetch)
    with caplog.at_level(logging.WARNING):
        for sec in ("a", "b"):
            await crawler._enrich_author_profile({"author": {"sec_uid": sec, "enterprise_verify_reason": "(unclosed"}})
    assert fetch.await_count == 2
    assert caplog.text.count("DY_SKIP_PROFILE_VERIFY_REGEX") == 1


# --- CLI ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cli_flags(monkeypatch):
    for name in [n for n in dir(config) if n.isupper()]:
        monkeypatch.setattr(config, name, getattr(config, name))
    await parse_cmd(["--platform", "dy"])
    assert config.DY_ACCOUNT_REQUESTS_PER_MINUTE == 20
    assert config.DY_ACCOUNT_MIN_INTERVAL == 3.0
    assert config.DY_SKIP_PROFILE_VERIFY_REGEX == ""
    result = await parse_cmd([
        "--platform", "dy",
        "--account_requests_per_minute", "8",
        "--account_min_interval", "6.5",
        "--dy_skip_profile_verify_regex", "官方|新闻",
    ])
    assert config.DY_ACCOUNT_REQUESTS_PER_MINUTE == 8
    assert config.DY_ACCOUNT_MIN_INTERVAL == 6.5
    assert config.DY_SKIP_PROFILE_VERIFY_REGEX == "官方|新闻"
    assert result.account_requests_per_minute == 8
    assert result.account_min_interval == 6.5
    assert result.dy_skip_profile_verify_regex == "官方|新闻"


# --- Fix round 1 -------------------------------------------------------------------

@pytest.mark.parametrize("uniform", ["low", "high"])
def test_platform_gate_jitter_never_shortens_min_interval(tmp_path, monkeypatch, uniform):
    monkeypatch.setattr(gate_module.random, "uniform", lambda a, b: a if uniform == "low" else b)
    monkeypatch.setattr(config, "DY_REQUEST_SCHEDULER_DB", "")
    monkeypatch.setenv("MEDIACRAWLER_REQUEST_SCHEDULER_DB", str(tmp_path / "platform.sqlite3"))
    gate = gate_module.configured_gate(min_interval=2.0, per_minute=1000, media_interval=0, jitter=0.4)
    now = [100.0]
    gate.state._clock = lambda: now[0]
    assert gate.state.try_acquire()[0] == 0
    delay = gate.state.try_acquire()[0]
    assert delay >= 2.0
    assert delay == pytest.approx(2.0 if uniform == "low" else 2.8)


def test_platform_gate_wait_is_always_at_least_min_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIACRAWLER_REQUEST_SCHEDULER_DB", str(tmp_path / "platform.sqlite3"))
    gate = gate_module.configured_gate(min_interval=2.0, per_minute=100000, media_interval=0, jitter=0.4)
    now = [100.0]
    gate.state._clock = lambda: now[0]
    for _ in range(200):
        assert gate.state.try_acquire()[0] == 0
        delay = gate.state.try_acquire()[0]
        assert 2.0 <= delay <= 2.8
        now[0] += delay


def test_lengthen_only_jitter_factor_bounds():
    samples = [jitter_factor(0.4, lengthen_only=True) for _ in range(500)]
    assert min(samples) >= 1.0 and max(samples) <= 1.4


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"status_code": 0, "filter_detail": {"filter_reason": "deleted", "aweme_id": "1"}},
    {"status_code": 0, "aweme_detail": None, "filter_reason": "private"},
    {"status_code": 0, "filter_list": [{"aweme_id": "1", "filter_reason": "unavailable"}]},
    {"status_code": 0, "status_msg": "作品已删除"},
])
async def test_detail_unavailable_post_is_not_a_signal(payload):
    for aweme_id in ("1", "2", "3"):
        assert await bare_client(payload).get_video_by_id(aweme_id) == {}
    assert pacing.silent_risk_streak() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"status_code": 0},
    {"status_code": 0, "aweme_detail": None, "filter_list": [], "status_msg": ""},
])
async def test_bare_empty_detail_is_still_a_signal(payload):
    await bare_client(payload).get_video_by_id("1")
    assert pacing.silent_risk_streak() == 1
