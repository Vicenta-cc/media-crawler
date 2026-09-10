import base64
import json
from unittest.mock import patch

import pytest

from base.base_crawler import ACCOUNT_AUTH_INVALID_MARKER, ACCOUNT_AUTH_STATE_ENV, AbstractCrawler


class TestCrawler(AbstractCrawler):
    async def start(self):
        return None

    async def search(self):
        return None

    async def launch_browser(self, chromium, playwright_proxy, user_agent, headless=True):
        return None


class FakeBrowserContext:
    def __init__(self):
        self.cookies_cleared = False
        self.cookies = []
        self.scripts = []

    async def clear_cookies(self):
        self.cookies_cleared = True

    async def add_cookies(self, cookies):
        self.cookies = cookies

    async def add_init_script(self, *, script):
        self.scripts.append(script)


def encode_state(state: dict) -> str:
    return base64.b64encode(json.dumps(state).encode("utf-8")).decode("ascii")


@pytest.mark.asyncio
async def test_applies_cookies_and_origin_local_storage_before_navigation():
    state = {
        "cookies": [{"name": "session", "value": "cookie-value", "domain": ".example.com", "path": "/"}],
        "origins": [
            {
                "origin": "https://www.example.com",
                "localStorage": [{"name": "token", "value": "storage-value"}],
            }
        ],
    }
    context = FakeBrowserContext()

    with patch.dict("os.environ", {ACCOUNT_AUTH_STATE_ENV: encode_state(state)}):
        applied = await TestCrawler().apply_account_auth_state(context)

    assert applied is True
    assert context.cookies_cleared is True
    assert context.cookies == state["cookies"]
    assert "window.localStorage.clear()" in context.scripts[0]
    assert "https://www.example.com" in context.scripts[0]
    assert "storage-value" in context.scripts[0]


def test_rejects_malformed_injected_state_with_account_marker():
    with patch.dict("os.environ", {ACCOUNT_AUTH_STATE_ENV: "not-base64"}):
        with pytest.raises(RuntimeError, match=ACCOUNT_AUTH_INVALID_MARKER):
            TestCrawler().load_account_auth_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_kind", ["about_blank", "srcdoc"])
async def test_inherited_origin_frame_preserves_parent_login_storage(frame_kind):
    from playwright.async_api import async_playwright

    state = {
        "cookies": [],
        "origins": [{
            "origin": "https://www.example.com",
            "localStorage": [{"name": "HasUserLogin", "value": "1"}],
        }],
    }
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()

            async def offline_page(route):
                await route.fulfill(content_type="text/html", body="<html><body>Fixture</body></html>")

            await context.route("**/*", offline_page)
            with patch.dict("os.environ", {ACCOUNT_AUTH_STATE_ENV: encode_state(state)}):
                await TestCrawler().apply_account_auth_state(context)
            page = await context.new_page()
            await page.goto("https://www.example.com/fixture")
            assert await page.evaluate("localStorage.getItem('HasUserLogin')") == "1"
            await page.evaluate("localStorage.setItem('live-session-update', 'keep')")
            await page.evaluate("""kind => new Promise(resolve => {
                const frame = document.createElement('iframe');
                frame.onload = resolve;
                if (kind === 'srcdoc') frame.srcdoc = '<html>Fixture frame</html>';
                document.body.appendChild(frame);
            })""", frame_kind)

            assert len(page.frames) == 2
            assert await page.frames[1].evaluate("location.origin") == "null"
            assert await page.evaluate("localStorage.getItem('HasUserLogin')") == "1"
            assert await page.evaluate("localStorage.getItem('live-session-update')") == "keep"
        finally:
            await browser.close()
