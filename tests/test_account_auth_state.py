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


def encode_state(state: dict) -> str:
    return base64.b64encode(json.dumps(state).encode("utf-8")).decode("ascii")


@pytest.mark.asyncio
async def test_applies_cookies_and_origin_local_storage_before_navigation():
    from playwright.async_api import async_playwright

    state = {
        "cookies": [{"name": "session", "value": "cookie-value", "domain": ".example.com", "path": "/"}],
        "origins": [
            {
                "origin": "https://www.example.com",
                "localStorage": [{"name": "token", "value": "storage-value"}],
            }
        ],
    }
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(offline=True, service_workers="block")
            await context.add_cookies([{"name": "old-account", "value": "remove",
                                       "domain": ".example.com", "path": "/"}])
            with patch.dict("os.environ", {ACCOUNT_AUTH_STATE_ENV: encode_state(state)}):
                applied = await TestCrawler().apply_account_auth_state(context)
            assert applied is True
            assert context.pages == []
            restored = await context.storage_state()
            assert [(cookie["name"], cookie["value"]) for cookie in restored["cookies"]] == [
                ("session", "cookie-value")]
            assert restored["origins"] == state["origins"]
        finally:
            await browser.close()


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


@pytest.mark.asyncio
async def test_restored_storage_survives_new_documents_tabs_and_explicit_clear():
    from playwright.async_api import async_playwright

    origin = "https://www.example.com"
    other_origin = "https://other.example.com"
    state = {
        "cookies": [],
        "origins": [
            {"origin": origin, "localStorage": [{"name": "session", "value": "old"}]},
            {"origin": other_origin, "localStorage": [{"name": "session", "value": "other-old"}]},
        ],
    }
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers="block", offline=True)

            async def offline_page(route):
                await route.fulfill(content_type="text/html", body="""<html><body>
                  <script>window.firstSession = localStorage.getItem('session')</script>
                </body></html>""")

            await context.route("**/*", offline_page)
            with patch.dict("os.environ", {ACCOUNT_AUTH_STATE_ENV: encode_state(state)}):
                await TestCrawler().apply_account_auth_state(context)
            assert context.pages == []
            page = await context.new_page()
            await page.goto(origin + "/first")
            assert await page.evaluate("window.firstSession") == "old"
            await page.evaluate("localStorage.setItem('session', 'fresh'); localStorage.setItem('new-key', 'keep')")
            await page.goto(origin + "/second")
            assert await page.evaluate("window.firstSession") == "fresh"
            await page.reload()
            assert await page.evaluate("localStorage.getItem('new-key')") == "keep"

            other_tab = await context.new_page()
            await other_tab.goto(origin + "/tab")
            assert await other_tab.evaluate("window.firstSession") == "fresh"
            await page.goto(other_origin + "/first")
            assert await page.evaluate("window.firstSession") == "other-old"
            await page.evaluate("localStorage.setItem('session', 'other-fresh')")
            await page.goto(origin + "/back")
            assert await page.evaluate("window.firstSession") == "fresh"
            await page.goto(other_origin + "/back")
            assert await page.evaluate("window.firstSession") == "other-fresh"

            # A logout/clear must not resurrect credentials on the next document.
            await other_tab.evaluate("localStorage.clear()")
            await other_tab.reload()
            assert await other_tab.evaluate("localStorage.length") == 0
            await page.goto(origin + "/after-clear")
            assert await page.evaluate("window.firstSession") is None
        finally:
            await browser.close()
