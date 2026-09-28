"""Opening the Douyin index page retries transient network failures (e.g. net::ERR_NETWORK_CHANGED)."""

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError

import config
from media_platform.douyin.core import DouYinCrawler


class FakePage:
    def __init__(self, failures):
        self.failures = list(failures)
        self.urls = []

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
    with pytest.raises(PlaywrightError, match="ERR_NETWORK_CHANGED"):
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
    with pytest.raises(PlaywrightError):
        await crawler._open_index_page()
    assert len(page.urls) == 1
