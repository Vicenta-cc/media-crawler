"""Opening the Douyin index page retries transient network failures (e.g. net::ERR_NETWORK_CHANGED)."""

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

import config
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import BrowserInitializationError


class FakePage:
    def __init__(self, failures):
        self.failures = list(failures)
        self.urls = []
        self.url = "https://www.douyin.com/"

    async def evaluate(self, expression):
        return {"origin": "https://www.douyin.com", "readyState": "complete"}

    async def goto(self, url, **kwargs):
        self.urls.append(url)
        if self.failures:
            raise self.failures.pop(0)


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        return self.page


def _crawler(monkeypatch, failures, retries=2):
    crawler = DouYinCrawler.__new__(DouYinCrawler)
    crawler.index_url = "https://www.douyin.com"
    page = FakePage(failures)
    crawler.browser_context = FakeContext(page)
    monkeypatch.setattr(config, "DY_PAGE_LOAD_RETRIES", retries, raising=False)
    monkeypatch.setattr(config, "DY_PAGE_LOAD_RETRY_BACKOFF_SECONDS", 0, raising=False)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("media_platform.douyin.core.asyncio.sleep", fake_sleep)
    return crawler, page, sleeps


def network_changed():
    return PlaywrightError("Page.goto: net::ERR_NETWORK_CHANGED at https://www.douyin.com/")


@pytest.mark.asyncio
async def test_transient_network_error_is_retried_then_succeeds(monkeypatch):
    crawler, page, sleeps = _crawler(monkeypatch, [network_changed()])
    await crawler._open_index_page()
    assert len(page.urls) == 2
    assert crawler.context_page is page
    assert crawler.browser_context.new_page_calls == 1


@pytest.mark.asyncio
async def test_gives_up_after_the_configured_retries(monkeypatch):
    crawler, page, _ = _crawler(monkeypatch, [network_changed() for _ in range(5)], retries=2)
    with pytest.raises(BrowserInitializationError, match="navigation_retry_exhausted"):
        await crawler._open_index_page()
    assert len(page.urls) == 3  # first try + 2 retries


@pytest.mark.asyncio
async def test_non_network_error_is_not_retried(monkeypatch):
    crawler, page, _ = _crawler(monkeypatch, [PlaywrightError("Target page, context or browser has been closed")])
    with pytest.raises(PlaywrightError, match="has been closed"):
        await crawler._open_index_page()
    assert len(page.urls) == 1


@pytest.mark.asyncio
async def test_zero_retries_keeps_the_old_single_attempt(monkeypatch):
    crawler, page, _ = _crawler(monkeypatch, [network_changed()], retries=0)
    with pytest.raises(BrowserInitializationError):
        await crawler._open_index_page()
    assert len(page.urls) == 1


from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from media_platform.douyin.exception import DataFetchError, PlatformRateLimitedError


def storage_denied():
    return PlaywrightError("Page.evaluate: SecurityError: Failed to read the 'localStorage' property from 'Window': Access is denied for this document.")


@pytest.mark.asyncio
async def test_storage_recovery_reopens_home_and_rebuilds_client(monkeypatch):
    crawler, page, _ = _crawler(monkeypatch, [])
    first = SimpleNamespace(pong=AsyncMock(side_effect=storage_denied()))
    second = SimpleNamespace(pong=AsyncMock(return_value=True))
    crawler.create_douyin_client = AsyncMock(side_effect=[first, second])
    await crawler._open_index_page()
    await crawler._check_login_with_page_recovery(None)
    assert len(page.urls) == 2
    assert crawler.dy_client is second
    assert crawler.browser_context.new_page_calls == 1


@pytest.mark.asyncio
async def test_navigation_and_storage_share_budget(monkeypatch):
    crawler, page, _ = _crawler(monkeypatch, [network_changed()])
    client = SimpleNamespace(pong=AsyncMock(side_effect=storage_denied()))
    crawler.create_douyin_client = AsyncMock(return_value=client)
    await crawler._open_index_page()
    with pytest.raises(BrowserInitializationError):
        await crawler._check_login_with_page_recovery(None)
    assert len(page.urls) == 3  # NOT 3 navigation tries times 3 login tries
    assert client.pong.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [PlaywrightError("Target page has been closed"),
                                   DataFetchError("ACCOUNT_VERIFY"), asyncio.CancelledError()])
async def test_other_failures_and_cancellation_never_retry(monkeypatch, error):
    crawler, page, _ = _crawler(monkeypatch, [])
    crawler.create_douyin_client = AsyncMock(return_value=SimpleNamespace(pong=AsyncMock(side_effect=error)))
    await crawler._open_index_page()
    with pytest.raises(type(error)):
        await crawler._check_login_with_page_recovery(None)
    assert len(page.urls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://www.douyin.com/verify?token=SECRET", "https://passport.douyin.com/login", "https://unknown.invalid/"])
async def test_restricted_or_unknown_page_is_not_retried(monkeypatch, url):
    crawler, page, _ = _crawler(monkeypatch, [])
    page.url = url
    crawler.create_douyin_client = AsyncMock(return_value=SimpleNamespace(pong=AsyncMock(side_effect=storage_denied())))
    await crawler._open_index_page()
    with pytest.raises(BrowserInitializationError):
        await crawler._check_login_with_page_recovery(None)
    assert len(page.urls) == 1
    assert "SECRET" not in str(await crawler._page_diagnostic("test"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(401, DataFetchError), (403, DataFetchError), (429, PlatformRateLimitedError)])
async def test_http_rejection_not_retried(monkeypatch, status, expected):
    crawler, page, _ = _crawler(monkeypatch, [])
    page.goto = AsyncMock(return_value=SimpleNamespace(status=status))
    with pytest.raises(expected):
        await crawler._open_index_page()
    assert page.goto.await_count == 1


@pytest.mark.asyncio
async def test_invalid_login_still_uses_original_signal(monkeypatch):
    crawler, page, _ = _crawler(monkeypatch, [])
    crawler.LOGIN_READINESS_TIMEOUT_SECONDS = 0
    crawler.create_douyin_client = AsyncMock(return_value=SimpleNamespace(pong=AsyncMock(return_value=False)))
    await crawler._open_index_page()
    with pytest.raises(RuntimeError, match="ACCOUNT_AUTH_INVALID"):
        await crawler._check_login_with_page_recovery(None)
    assert len(page.urls) == 1


@pytest.mark.asyncio
async def test_bound_navigation_waits_for_dom_with_inner_deadline_first(monkeypatch):
    from contextlib import asynccontextmanager
    crawler, page, _ = _crawler(monkeypatch, [])
    calls=[]
    @asynccontextmanager
    async def slot(operation, **kwargs):
        calls.append((operation,kwargs))
        yield
    async def goto(url, **kwargs):
        assert kwargs['wait_until']=='domcontentloaded'
        assert kwargs['timeout'] < calls[-1][1]['timeout']*1000
    page.goto=goto
    crawler._bound_proxy_session=SimpleNamespace(slot=slot,fail=Mock())
    await crawler._open_index_page()
    assert calls[0][0]=='navigate'
    crawler._bound_proxy_session.fail.assert_not_called()


@pytest.mark.asyncio
async def test_navigation_timeout_does_not_mark_proxy_dead(monkeypatch):
    from playwright.async_api import TimeoutError
    crawler, _, _ = _crawler(monkeypatch,[TimeoutError('pending background resource')])
    with pytest.raises(RuntimeError,match='PAGE_INITIALIZATION_TIMEOUT'):
        await crawler._open_index_page()


@pytest.mark.asyncio
async def test_login_readiness_polls_only_ready_document_and_propagates_risk():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    crawler=DouYinCrawler()
    crawler.browser_context=SimpleNamespace()
    crawler.LOGIN_READINESS_TIMEOUT_SECONDS=.1
    crawler.context_page=SimpleNamespace(evaluate=AsyncMock(side_effect=[False,True]))
    crawler.dy_client=SimpleNamespace(pong=AsyncMock(return_value=True))
    assert await crawler._wait_for_login_readiness()
    crawler.dy_client.pong.assert_awaited_once()
    crawler.context_page.evaluate=AsyncMock(return_value=True)
    crawler.dy_client.pong=AsyncMock(side_effect=RuntimeError('ACCOUNT_VERIFY'))
    with pytest.raises(RuntimeError,match='ACCOUNT_VERIFY'):
        await crawler._wait_for_login_readiness()


@pytest.mark.asyncio
async def test_login_readiness_deadline_and_stop_remain_enforced():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    crawler=DouYinCrawler()
    crawler.browser_context=SimpleNamespace()
    crawler.LOGIN_READINESS_TIMEOUT_SECONDS=0
    crawler.context_page=SimpleNamespace(evaluate=AsyncMock(return_value=False))
    crawler.dy_client=SimpleNamespace(pong=AsyncMock(return_value=True))
    assert not await crawler._wait_for_login_readiness()
    crawler.dy_client.pong.assert_not_awaited()
    crawler._bound_proxy_session=SimpleNamespace(check_stop_async=AsyncMock(side_effect=RuntimeError('PROXY_EXIT_CHANGED')))
    with pytest.raises(RuntimeError,match='PROXY_EXIT_CHANGED'):
        await crawler._wait_for_login_readiness()
