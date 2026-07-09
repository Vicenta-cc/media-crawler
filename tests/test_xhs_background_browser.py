# -*- coding: utf-8 -*-

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from media_platform.xhs import core as xhs_core
from media_platform.xhs.core import XiaoHongShuCrawler
from tools.browser_state import has_saved_cookie_state


def _cookies_path(tmp_path: Path) -> Path:
    return tmp_path / "browser_data" / "xhs_user_data_dir" / "Default" / "Cookies"


def test_xhs_saved_login_state_reads_web_session_cookie(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "PLATFORM", "xhs")
    monkeypatch.setattr(config, "USER_DATA_DIR", "%s_user_data_dir")
    monkeypatch.setattr(config, "XHS_INTERNATIONAL", False)

    cookies_path = _cookies_path(tmp_path)
    cookies_path.parent.mkdir(parents=True)
    with sqlite3.connect(cookies_path) as conn:
        conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
        conn.execute(
            "INSERT INTO cookies (host_key, name) VALUES (?, ?)",
            (".xiaohongshu.com", "web_session"),
        )

    assert has_saved_cookie_state(("web_session",), ("xiaohongshu.com",), "xhs") is True


def test_xhs_saved_login_state_missing_cookie_is_false(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "PLATFORM", "xhs")
    monkeypatch.setattr(config, "USER_DATA_DIR", "%s_user_data_dir")

    assert has_saved_cookie_state(("web_session",), ("xiaohongshu.com",), "xhs") is False


def test_xhs_saved_login_state_sqlite_error_falls_back_to_file_presence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "PLATFORM", "xhs")
    monkeypatch.setattr(config, "USER_DATA_DIR", "%s_user_data_dir")

    cookies_path = _cookies_path(tmp_path)
    cookies_path.parent.mkdir(parents=True)
    cookies_path.write_text("browser is still writing this file", encoding="utf-8")

    assert has_saved_cookie_state(("web_session",), ("xiaohongshu.com",), "xhs") is True


def test_xhs_background_headless_uses_saved_login_state(monkeypatch):
    crawler = XiaoHongShuCrawler()
    monkeypatch.setattr(config, "HEADLESS_EXPLICITLY_SET", False, raising=False)
    monkeypatch.setattr(crawler, "_has_saved_login_state", lambda: True)

    assert crawler._resolve_background_headless() is True


@pytest.mark.asyncio
async def test_xhs_background_mode_reopens_visible_browser_when_login_expired(monkeypatch):
    crawler = XiaoHongShuCrawler()
    launches = []
    closed_contexts = []

    class FakePlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=object())

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_launch_browser_context(playwright, playwright_proxy, use_background_mode, background_headless):
        launches.append((use_background_mode, background_headless))
        context = AsyncMock()
        context.close = AsyncMock(side_effect=lambda: closed_contexts.append(context))
        context.new_page = AsyncMock(return_value=AsyncMock())
        context.add_init_script = AsyncMock()
        crawler.browser_context = context

    async def fake_open_index_page():
        crawler.context_page = AsyncMock()

    clients = [
        SimpleNamespace(pong=AsyncMock(return_value=False)),
        SimpleNamespace(
            pong=AsyncMock(return_value=False),
            update_cookies=AsyncMock(),
        ),
    ]

    monkeypatch.setattr(config, "ENABLE_IP_PROXY", False)
    monkeypatch.setattr(config, "SAVE_LOGIN_STATE", True)
    monkeypatch.setattr(config, "ENABLE_BACKGROUND_BROWSER_MODE", True, raising=False)
    monkeypatch.setattr(config, "HEADLESS_EXPLICITLY_SET", False, raising=False)
    monkeypatch.setattr(config, "LOGIN_TYPE", "qrcode")
    monkeypatch.setattr(config, "CRAWLER_TYPE", "noop")
    monkeypatch.setattr(crawler, "_has_saved_login_state", lambda: True)
    monkeypatch.setattr(crawler, "_launch_browser_context", fake_launch_browser_context)
    monkeypatch.setattr(crawler, "_open_index_page", fake_open_index_page)
    monkeypatch.setattr(crawler, "create_xhs_client", AsyncMock(side_effect=clients))
    monkeypatch.setattr(xhs_core, "async_playwright", lambda: FakePlaywrightContext())
    login_cls = MagicMock(return_value=SimpleNamespace(begin=AsyncMock()))
    monkeypatch.setattr(xhs_core, "XiaoHongShuLogin", login_cls)

    await crawler.start()

    assert launches == [(True, True), (True, False)]
    assert len(closed_contexts) == 1
    login_cls.assert_called_once()
    clients[1].update_cookies.assert_awaited_once()
