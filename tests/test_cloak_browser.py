import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from media_platform.douyin.core import DouYinCrawler
from tools.cloak_browser import launch_account_context, profile_for


def test_accounts_get_distinct_stable_profiles_and_versions(tmp_path):
    a = profile_for("account-a", tmp_path, "145.0.7632.109.2")
    b = profile_for("account-b", tmp_path, "145.0.7632.109.2")
    assert a["directory"] != b["directory"]
    assert a["seed"] != b["seed"]
    assert profile_for("account-a", tmp_path, "999.0.0.1") == a
    with pytest.raises(ValueError, match="ACCOUNT_ID"):
        profile_for("", tmp_path, "145.0.7632.109.2")


@pytest.mark.asyncio
async def test_cloak_route_bypasses_cdp_and_rejects_overlapping_account(monkeypatch, tmp_path):
    import cloakbrowser
    import config

    launcher = AsyncMock(return_value=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", launcher)
    monkeypatch.setenv("MEDIACRAWLER_DY_BROWSER_ENGINE", "cloakbrowser")
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT_ID", "account-a")
    monkeypatch.setenv("MEDIACRAWLER_CLOAK_PROFILE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "ENABLE_CDP_MODE", True)
    crawler = DouYinCrawler()
    await crawler._launch_browser_context(None, None, False, False)
    with pytest.raises(RuntimeError, match="already in use"):
        await launch_account_context("account-a", tmp_path, headless=True)
    assert launcher.call_count == 1
    assert launcher.call_args.kwargs["humanize"] is False
    assert launcher.call_args.kwargs["headless"] is True
    await crawler._close_current_browser()
    context, _ = await launch_account_context("account-a", tmp_path, headless=True)
    await context.close()


@pytest.mark.asyncio
async def test_legacy_browser_override_does_not_fallback(monkeypatch):
    monkeypatch.setenv("MEDIACRAWLER_DY_BROWSER_ENGINE", "playwright")
    with pytest.raises(ValueError, match="fallback is disabled"):
        await DouYinCrawler()._launch_browser_context(None, None, False, False)


@pytest.mark.asyncio
async def test_missing_login_closes_profile_without_opening_second_browser(monkeypatch):
    from contextlib import asynccontextmanager
    from media_platform.douyin import core
    @asynccontextmanager
    async def driver():
        yield None
    monkeypatch.setattr(core, 'async_playwright', driver)
    monkeypatch.setattr(core.config, 'ENABLE_IP_PROXY', False)
    crawler = DouYinCrawler()
    context = SimpleNamespace(close=AsyncMock())
    async def launch(**kwargs):
        crawler.browser_context = context
    crawler._launch_browser_context = AsyncMock(side_effect=launch)
    crawler._open_index_page = AsyncMock()
    crawler.apply_account_auth_state = AsyncMock()
    crawler.create_douyin_client = AsyncMock(return_value=SimpleNamespace(pong=AsyncMock(return_value=False)))
    with pytest.raises(RuntimeError, match='ACCOUNT_AUTH_INVALID'):
        await crawler.start()
    assert crawler._launch_browser_context.await_count == 1
    context.close.assert_awaited_once()
    assert crawler.browser_context is None


@pytest.mark.asyncio
async def test_restart_never_reimports_old_cookie_snapshot(monkeypatch, tmp_path):
    state = {"cookies": [{"name": "fixture", "value": "old"}], "origins": []}
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT_AUTH_STATE_B64", base64.b64encode(json.dumps(state).encode()).decode())
    profile = profile_for("account-a", tmp_path, "145.0.7632.109.2")
    first = DouYinCrawler()
    first._cloak_profile = profile
    context = SimpleNamespace(clear_cookies=AsyncMock(), add_cookies=AsyncMock())
    assert await first.apply_account_auth_state(context)
    second = DouYinCrawler()
    second._cloak_profile = profile
    assert not await second.apply_account_auth_state(context)
    assert context.clear_cookies.call_count == 1
    assert context.add_cookies.call_count == 1


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("CLOAK_BROWSER_SMOKE") != "1", reason="requires downloaded CloakBrowser binary")
@pytest.mark.parametrize("headless", [False, True])
async def test_actual_browser_restart_and_account_storage_isolation(tmp_path, headless):
    async def open_account(account):
        context, profile = await launch_account_context(account, tmp_path, headless=headless)
        await context.route("**/*", lambda route: route.fulfill(body="<html><body>Local fixture</body></html>", content_type="text/html"))
        page = await context.new_page()
        await page.goto("https://fixture.invalid/")
        return context, profile, page

    expression = """async () => {
        const c = document.createElement('canvas'); c.width=200; c.height=50;
        const x = c.getContext('2d');
        x.fillStyle='#ff6600'; x.fillRect(1, 1, 160, 30);
        x.fillStyle='#123abc'; x.font='18px Arial'; x.fillText('account fixture 123', 10, 30);
        const gl=document.createElement('canvas').getContext('webgl');
        const ext=gl && gl.getExtension('WEBGL_debug_renderer_info');
        const ac=new OfflineAudioContext(1, 4410, 44100);
        const oscillator=ac.createOscillator(); oscillator.type='triangle'; oscillator.frequency.value=1000;
        const compressor=ac.createDynamicsCompressor();
        oscillator.connect(compressor); compressor.connect(ac.destination); oscillator.start();
        const audio=Array.from((await ac.startRendering()).getChannelData(0).slice(4000, 4010));
        return {ua:navigator.userAgent, canvas:c.toDataURL(), cpu:navigator.hardwareConcurrency,
                gpu:ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null, audio};
    }"""
    a, first_profile, page = await open_account("a")
    try:
        first_fingerprint = await page.evaluate(expression)
        await page.evaluate("localStorage.setItem('fixture', 'fresh')")
        await a.add_cookies([{"name": "fixture", "value": "a", "url": "https://fixture.invalid/", "expires": 2000000000}])
    finally:
        await a.close()
    a, restarted_profile, page = await open_account("a")
    try:
        assert restarted_profile == first_profile
        assert await page.evaluate(expression) == first_fingerprint
        assert await page.evaluate("localStorage.getItem('fixture')") == "fresh"
        assert any(c["name"] == "fixture" and c["value"] == "a" for c in await a.cookies())
    finally:
        await a.close()
    samples = [first_fingerprint]
    for account in ("b", "c"):
        other, other_profile, page = await open_account(account)
        try:
            assert other_profile["seed"] != first_profile["seed"]
            sample = await page.evaluate(expression)
            assert all(sample != previous for previous in samples)
            samples.append(sample)
            print(json.dumps({"headless": headless, "account": account,
                              "different_fields": [key for key in sample if sample[key] != first_fingerprint[key]]}))
            assert await page.evaluate("localStorage.getItem('fixture')") is None
            assert not any(c["name"] == "fixture" for c in await other.cookies())
        finally:
            await other.close()
