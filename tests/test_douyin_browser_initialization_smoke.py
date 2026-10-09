"""Real Chromium SecurityError on a sandboxed document; all requests intercepted.

No platform traffic, credentials or persistent account profile are used.
"""
import os

import pytest
from playwright.async_api import async_playwright

import config
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import BrowserInitializationError


@pytest.mark.asyncio
@pytest.mark.parametrize('persistent', [False, True])
async def test_real_storage_denial_recovery_or_bounded_failure(monkeypatch, tmp_path, persistent):
    if os.getenv('BROWSER_INIT_SMOKE') != '1':
        pytest.skip('opt-in local Chromium smoke test')
    executable = os.getenv('BROWSER_INIT_CHROMIUM')
    monkeypatch.setattr(config, 'DY_REQUEST_SCHEDULER_DB', str(tmp_path / 'scheduler.sqlite3'))
    monkeypatch.delenv('MEDIACRAWLER_ACCOUNT_ID', raising=False)
    monkeypatch.setattr(config, 'DY_PAGE_LOAD_RETRIES', 2)
    monkeypatch.setattr(config, 'DY_PAGE_LOAD_RETRY_BACKOFF_SECONDS', 0)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
        try:
            context = await browser.new_context(service_workers='block')
            requests = []
            async def serve(route):
                requests.append(route.request.url)
                denied = persistent or len(requests) == 1
                headers = {'Content-Security-Policy': 'sandbox allow-scripts'} if denied else {}
                await route.fulfill(status=200, headers=headers, content_type='text/html',
                    body='<html><script>localStorage.setItem("HasUserLogin", "1")</script></html>')
            await context.route('**/*', serve)
            crawler = DouYinCrawler()
            crawler.browser_context = context
            await crawler._open_index_page()
            diagnostic = await crawler._page_diagnostic('smoke')
            assert diagnostic['origin'] == 'null'
            if persistent:
                with pytest.raises(BrowserInitializationError, match='local_storage_unavailable'):
                    await crawler._check_login_with_page_recovery(None)
                assert len(requests) == 3
            else:
                await crawler._check_login_with_page_recovery(None)
                assert len(requests) == 2
                assert await crawler.dy_client.pong(context)
            assert all(url == 'https://www.douyin.com/' for url in requests)
        finally:
            await browser.close()
