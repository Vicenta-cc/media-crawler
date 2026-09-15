# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/base/base_crawler.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import base64
import json
import os
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, BrowserType, Playwright

import config
from tools import utils
from tools.crawl_rate_limiter import ContentRateLimiter

ACCOUNT_AUTH_STATE_ENV = "MEDIACRAWLER_ACCOUNT_AUTH_STATE_B64"
ACCOUNT_AUTH_INVALID_MARKER = "ACCOUNT_AUTH_INVALID"


class AbstractCrawler(ABC):

    def has_account_auth_state(self) -> bool:
        return bool(os.getenv(ACCOUNT_AUTH_STATE_ENV, "").strip())

    def load_account_auth_state(self) -> dict | None:
        encoded = os.getenv(ACCOUNT_AUTH_STATE_ENV, "").strip()
        if not encoded:
            return None
        try:
            value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{ACCOUNT_AUTH_INVALID_MARKER}: injected storage state is invalid") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{ACCOUNT_AUTH_INVALID_MARKER}: injected storage state must be an object")
        if not isinstance(value.get("cookies", []), list) or not isinstance(value.get("origins", []), list):
            raise RuntimeError(f"{ACCOUNT_AUTH_INVALID_MARKER}: injected storage state has invalid fields")
        return value

    async def apply_account_auth_state(self, browser_context: BrowserContext) -> bool:
        state = self.load_account_auth_state()
        if state is None:
            return False

        await browser_context.clear_cookies()
        cookies = state.get("cookies") or []
        if cookies:
            await browser_context.add_cookies(cookies)

        storage_by_origin = {
            str(origin.get("origin") or ""): origin.get("localStorage") or []
            for origin in state.get("origins") or []
            if isinstance(origin, dict) and str(origin.get("origin") or "")
        }
        for origin in storage_by_origin:
            parsed = urlsplit(origin)
            if (parsed.scheme not in ("http", "https") or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in ("", "/")):
                raise RuntimeError(f"{ACCOUNT_AUTH_INVALID_MARKER}: invalid storage origin")

        if storage_by_origin:
            # Seed each origin once before opening the site. A context init
            # script would replay old credentials on every document/new tab,
            # including after the site refreshes or explicitly clears them.
            # This temporary page serves blank documents locally and leaves no
            # restoration script or sentinel key in the running site.
            storage_page = await browser_context.new_page()
            try:
                async def blank_document(route):
                    await route.fulfill(status=200, content_type="text/html",
                                        body="<!doctype html><html><head></head><body></body></html>")

                await storage_page.route("**/*", blank_document)
                for origin, values in storage_by_origin.items():
                    await storage_page.goto(origin.rstrip("/") + "/", wait_until="domcontentloaded")
                    await storage_page.evaluate("""values => {
                        window.localStorage.clear();
                        for (const item of values) {
                            if (item && typeof item.name === "string") {
                                window.localStorage.setItem(item.name, String(item.value ?? ""));
                            }
                        }
                    }""", values)
            finally:
                await storage_page.close()
        utils.logger.info(f"[{self.__class__.__name__}] Applied injected account storage state")
        return True

    def raise_account_auth_invalid(self) -> None:
        raise RuntimeError(f"{ACCOUNT_AUTH_INVALID_MARKER}: selected account login state is no longer valid")

    @asynccontextmanager
    async def content_request_slot(
        self,
        semaphore: asyncio.Semaphore,
        content_id: str = "",
    ) -> AsyncIterator[None]:
        """Acquire concurrency capacity before reserving a content start slot."""
        async with semaphore:
            await self.wait_for_content_slot(content_id)
            yield

    async def wait_for_content_slot(self, content_id: str = "") -> None:
        """Wait until the next configured primary-content start slot."""
        items_per_minute = config.CRAWLER_MAX_ITEMS_PER_MINUTE
        limiter = getattr(self, "_content_rate_limiter", None)
        if limiter is None or limiter.items_per_minute != items_per_minute:
            limiter = ContentRateLimiter(items_per_minute)
            self._content_rate_limiter = limiter

        wait_seconds = await limiter.acquire()
        if wait_seconds > 0:
            content_label = f" for {content_id}" if content_id else ""
            utils.logger.info(
                f"[{self.__class__.__name__}] Content rate limit: waited "
                f"{wait_seconds:.1f} seconds{content_label} "
                f"(max {items_per_minute}/min)"
            )

    @abstractmethod
    async def start(self):
        """
        start crawler
        """
        pass

    @abstractmethod
    async def search(self):
        """
        search
        """
        pass

    @abstractmethod
    async def launch_browser(self, chromium: BrowserType, playwright_proxy: Optional[Dict], user_agent: Optional[str], headless: bool = True) -> BrowserContext:
        """
        launch browser
        :param chromium: chromium browser
        :param playwright_proxy: playwright proxy
        :param user_agent: user agent
        :param headless: headless mode
        :return: browser context
        """
        pass

    async def launch_browser_with_cdp(self, playwright: Playwright, playwright_proxy: Optional[Dict], user_agent: Optional[str], headless: bool = True) -> BrowserContext:
        """
        Launch browser using CDP mode (optional implementation)
        :param playwright: playwright instance
        :param playwright_proxy: playwright proxy configuration
        :param user_agent: user agent
        :param headless: headless mode
        :return: browser context
        """
        # Default implementation: fallback to standard mode
        return await self.launch_browser(playwright.chromium, playwright_proxy, user_agent, headless)


class AbstractLogin(ABC):

    @abstractmethod
    async def begin(self):
        pass

    @abstractmethod
    async def login_by_qrcode(self):
        pass

    @abstractmethod
    async def login_by_mobile(self):
        pass

    @abstractmethod
    async def login_by_cookies(self):
        pass


class AbstractStore(ABC):

    @abstractmethod
    async def store_content(self, content_item: Dict):
        pass

    @abstractmethod
    async def store_comment(self, comment_item: Dict):
        pass

    # TODO support all platform
    # only xhs is supported, so @abstractmethod is commented
    @abstractmethod
    async def store_creator(self, creator: Dict):
        pass


class AbstractStoreImage(ABC):
    # TODO: support all platform
    # only weibo is supported
    # @abstractmethod
    async def store_image(self, image_content_item: Dict):
        pass


class AbstractStoreVideo(ABC):
    # TODO: support all platform
    # only weibo is supported
    # @abstractmethod
    async def store_video(self, video_content_item: Dict):
        pass


class AbstractApiClient(ABC):

    @abstractmethod
    async def request(self, method, url, **kwargs):
        pass

    @abstractmethod
    async def update_cookies(self, browser_context: BrowserContext):
        pass
