# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/core.py
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
import functools
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
import os
import sys
from contextlib import asynccontextmanager
from asyncio import Task
from typing import Any, Dict, List, Optional

import httpx

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
)

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import douyin as douyin_store
from tools import utils
from tools.reusable_content import reusable_aweme_is_complete
from tools.browser_state import has_saved_cookie_state
from tools.collection_route import CollectionRouteError
from tools.bound_proxy import initialize_bound_route, BoundProxySession
from tools.cdp_browser import CDPBrowserManager
from tools.collection_status import (
    CollectionIncompleteError,
    failure_reason,
    write_collection_status,
)
from var import crawler_type_var, source_keyword_var

from .client import DouYinClient
from .exception import BrowserInitializationError, DataFetchError, MediaDownloadError, PlatformRateLimitedError
from .field import PublishTimeType, SearchSortType
from .help import parse_video_info_from_url, parse_creator_info_from_url
from .pacing import jittered_sleep, record_content_ok, record_silent_risk, set_single_detail_target


@functools.lru_cache(maxsize=8)
def _compiled_skip_profile_regex(pattern: str) -> Optional["re.Pattern[str]"]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        utils.logger.warning(
            f"[DouYinCrawler] invalid DY_SKIP_PROFILE_VERIFY_REGEX ignored: {exc}"
        )
        return None




class DouYinCrawler(AbstractCrawler):
    _bound_proxy_session = None
    INDEX_NAVIGATION_TIMEOUT_SECONDS = 60
    LOGIN_READINESS_TIMEOUT_SECONDS = 60
    context_page: Page
    dy_client: DouYinClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.douyin.com"
        self.cookie_urls = [
            "https://douyin.com",
            self.index_url,
            "https://creator.douyin.com",
            "https://douhot.douyin.com",
            "https://live.douyin.com",
        ]
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self._camoufox_profile = None
        self._bound_proxy_session = None

    async def apply_account_auth_state(self, browser_context) -> bool:
        # A Firefox profile must authenticate in place. Chromium-era DB Cookie
        # snapshots cannot initialize a new browser identity.
        return False

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        snapshot = initialize_bound_route(platform='dy')
        if snapshot is not None:
            self._bound_proxy_session = await asyncio.to_thread(BoundProxySession, snapshot)
            playwright_proxy_format = snapshot.playwright_proxy()
            httpx_proxy_format = snapshot.httpx_proxy()
        elif config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with self._browser_lifetime() as playwright:
            background_browser_mode = self._should_use_background_browser_mode()
            background_headless = self._resolve_background_headless() if background_browser_mode else False

            await self._launch_browser_context(
                playwright=playwright,
                playwright_proxy=playwright_proxy_format,
                use_background_mode=background_browser_mode,
                background_headless=background_headless,
            )
            await self.apply_account_auth_state(self.browser_context)
            self._page_retry_count = 0
            await self._open_index_page()
            await self._check_login_with_page_recovery(httpx_proxy_format)
            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_awemes()
            elif config.CRAWLER_TYPE == "creator":
                # Get the information and comments of the specified creator
                await self.get_creators_and_videos()

            utils.logger.info("[DouYinCrawler.start] Douyin Crawler finished ...")

    @asynccontextmanager
    async def _browser_lifetime(self):
        from tools.account_display import account_display
        with account_display():
            try:
                yield None  # The Camoufox manager owns the sole driver/browser.
            except BaseException:
                try:
                    await self._close_current_browser()
                except Exception as cleanup_error:
                    utils.logger.warning(f"Browser cleanup failed: {type(cleanup_error).__name__}")
                raise
            else:
                await self._close_current_browser()

    def _should_use_background_browser_mode(self) -> bool:
        return bool(
            getattr(config, "ENABLE_BACKGROUND_BROWSER_MODE", False)
            and config.SAVE_LOGIN_STATE
        )

    def _headless_was_explicitly_set(self) -> bool:
        return bool(getattr(config, "HEADLESS_EXPLICITLY_SET", False))

    def _resolve_background_headless(self) -> bool:
        if self._headless_was_explicitly_set():
            return bool(config.HEADLESS)
        return True

    def _has_saved_login_state(self) -> bool:
        return has_saved_cookie_state(
            cookie_names=("LOGIN_STATUS", "sessionid", "sid_guard"),
            domains=("douyin.com",),
            platform=config.PLATFORM,
        )

    async def _launch_browser_context(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        use_background_mode: bool,
        background_headless: bool,
    ) -> None:
        if os.getenv("MEDIACRAWLER_DY_BROWSER_ENGINE", "camoufox") != "camoufox":
            raise ValueError("Douyin requires Camoufox; legacy browser fallback is disabled")
        from tools.camoufox_browser import launch_account_context
        root = Path(os.getenv("MEDIACRAWLER_CAMOUFOX_PROFILE_ROOT", config.DY_CAMOUFOX_PROFILE_ROOT))
        self.browser_context, self._camoufox_profile = await launch_account_context(
            os.getenv("MEDIACRAWLER_ACCOUNT_ID", ""), root,
            headless=sys.platform == "darwin",
            proxy=playwright_proxy,
            **({'route_snapshot': self._bound_proxy_session.snapshot,
                'bound_proxy_session': self._bound_proxy_session}
               if self._bound_proxy_session is not None else {}),
        )

    # Chromium network-layer failures that a fresh attempt normally clears
    # (interface/IP/proxy change, reset connection, DNS hiccup).
    _TRANSIENT_NET_ERRORS = (
        "ERR_NETWORK_CHANGED",
        "ERR_CONNECTION_RESET",
        "ERR_CONNECTION_CLOSED",
        "ERR_CONNECTION_REFUSED",
        "ERR_CONNECTION_TIMED_OUT",
        "ERR_TIMED_OUT",
        "ERR_INTERNET_DISCONNECTED",
        "ERR_NAME_NOT_RESOLVED",
        "ERR_NETWORK_IO_SUSPENDED",
        "ERR_ADDRESS_UNREACHABLE",
        "ERR_EMPTY_RESPONSE",
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_TUNNEL_CONNECTION_FAILED",
    )

    @classmethod
    def _is_transient_page_load_error(cls, exc: Exception) -> bool:
        if isinstance(exc, PlaywrightTimeoutError):
            return True
        text = str(exc)
        return any(code in text for code in cls._TRANSIENT_NET_ERRORS)

    async def _retry_page_initialization(self) -> bool:
        # Navigation and login-check recovery share ONE budget for this startup.
        retries = max(0, int(getattr(config, "DY_PAGE_LOAD_RETRIES", 2)))
        used = getattr(self, "_page_retry_count", 0)
        if used >= retries:
            return False
        self._page_retry_count = used + 1
        backoff = max(0.0, float(getattr(config, "DY_PAGE_LOAD_RETRY_BACKOFF_SECONDS", 3.0)))
        await asyncio.sleep(backoff * self._page_retry_count)
        return True

    async def _page_diagnostic(self, reason: str) -> dict:
        # Never log query strings, fragments, page text, cookies or storage values.
        page = self.context_page
        url = urlsplit(str(page.url))
        path = url.path.lower()
        restricted = any(word in path for word in ("captcha", "verify", "challenge", "login", "passport"))
        data = {
            "reason": reason, "scheme": url.scheme, "host": url.hostname,
            "page_kind": "restricted" if restricted else "home" if path in ("", "/") else "other",
            "http_status": getattr(self, "_index_http_status", None),
            "retries_used": getattr(self, "_page_retry_count", 0),
        }
        try:
            state = await page.evaluate("() => ({origin: window.origin, readyState: document.readyState})")
            if isinstance(state, dict):
                origin = str(state.get("origin", ""))
                parsed = urlsplit(origin)
                data["origin"] = "null" if origin == "null" else f"{parsed.scheme}://{parsed.hostname}"
                ready = state.get("readyState")
                data["ready_state"] = ready if ready in ("loading", "interactive", "complete") else "unknown"
        except PlaywrightError:
            data["diagnostic_unavailable"] = True
        utils.logger.warning("BROWSER_INIT_DIAGNOSTIC: " + json.dumps(data, ensure_ascii=False))
        return data

    async def _open_index_page(self, *, reuse_page: bool = False) -> None:
        if not reuse_page:
            self.context_page = await self.browser_context.new_page()
        while True:
            self._index_http_status = None
            try:
                if self._bound_proxy_session is not None:
                    async with self._bound_proxy_session.slot(
                        "navigate", timeout=self.INDEX_NAVIGATION_TIMEOUT_SECONDS + 5
                    ):
                        response = await self.context_page.goto(
                            self.index_url,
                            wait_until="domcontentloaded",
                            timeout=self.INDEX_NAVIGATION_TIMEOUT_SECONDS * 1000,
                        )
                else:
                    response = await self.context_page.goto(
                        self.index_url,
                        wait_until="domcontentloaded",
                        timeout=self.INDEX_NAVIGATION_TIMEOUT_SECONDS * 1000,
                    )
                self._index_http_status = response.status if response else None
                if self._index_http_status == 429:
                    raise PlatformRateLimitedError("PLATFORM_RATE_LIMITED: homepage HTTP 429")
                if self._index_http_status in (401, 403):
                    raise DataFetchError("ACCOUNT_VERIFY: homepage access denied")
                return
            except PlaywrightTimeoutError:
                # A slow document is not proof that the fixed proxy is dead.
                raise RuntimeError('PAGE_INITIALIZATION_TIMEOUT') from None
            except PlaywrightError as exc:
                if self._bound_proxy_session is not None:
                    raise self._bound_proxy_session.fail("PROXY_UNAVAILABLE") from None
                if not self._is_transient_page_load_error(exc):
                    raise
                reason = next((code for code in self._TRANSIENT_NET_ERRORS if code in str(exc)), type(exc).__name__)
                await self._page_diagnostic(reason)
                if not await self._retry_page_initialization():
                    raise BrowserInitializationError("navigation_retry_exhausted") from None

    async def _check_login_with_page_recovery(self, httpx_proxy_format) -> None:
        while True:
            try:
                self.dy_client = await self.create_douyin_client(httpx_proxy_format)
                if not await self._wait_for_login_readiness():
                    # Account management owns login; background jobs never open a
                    # second browser or switch to an interactive login flow.
                    self.raise_account_auth_invalid()
                return
            except PlaywrightError as exc:
                # Narrow recovery: do not swallow closed browsers, cancellation,
                # arbitrary JS errors, or account/platform rejection signals.
                message = str(exc)
                storage_denied = "localStorage" in message and (
                    "SecurityError" in message
                    or "Access is denied for this document" in message
                    or ("document is sandboxed" in message and "allow-same-origin" in message)
                )
                if not storage_denied:
                    raise
                data = await self._page_diagnostic("local_storage_security_error")
                known_page = (
                    data["scheme"] == "https" and data["host"] in ("www.douyin.com", "douyin.com")
                    and data["page_kind"] == "home"
                ) or data["scheme"] in ("about", "chrome-error")
                if (not known_page or data.get("diagnostic_unavailable")
                        or data["http_status"] in (401, 403, 429)
                        or not await self._retry_page_initialization()):
                    raise BrowserInitializationError("local_storage_unavailable") from None
                await self._open_index_page(reuse_page=True)

    async def _wait_for_login_readiness(self) -> bool:
        """Wait independently for a ready first-party document and existing auth.

        Platform risk/route errors propagate; only an unready login is polled.
        Background resources need not finish and no second browser is opened.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.LOGIN_READINESS_TIMEOUT_SECONDS
        async with asyncio.timeout(self.LOGIN_READINESS_TIMEOUT_SECONDS + 5):
            while True:
                if self._bound_proxy_session is not None:
                    await self._bound_proxy_session.check_stop_async()
                ready = await asyncio.wait_for(self.context_page.evaluate("""() => {
                    const host = location.hostname;
                    return location.protocol === 'https:' &&
                        (host === 'douyin.com' || host.endsWith('.douyin.com')) &&
                        document.readyState !== 'loading';
                }"""), 5)
                if ready and await asyncio.wait_for(
                    self.dy_client.pong(browser_context=self.browser_context), 5
                ):
                    return True
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(.5, remaining))

    async def _close_current_browser(self) -> None:
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
            return

        if getattr(self, "browser_context", None):
            context, self.browser_context = self.browser_context, None
            await context.close()

    async def search(self) -> None:
        utils.logger.info("[DouYinCrawler.search] Begin search douyin keywords")
        dy_limit_count = max(1, int(getattr(config, "DY_SEARCH_PAGE_SIZE", 15)))
        skip_aweme_ids: set[str] = set()
        skip_file = str(getattr(config, "DY_SKIP_AWEME_IDS_FILE", "") or "").strip()
        authority = getattr(self._bound_proxy_session, 'authority', None)
        managed = bool(getattr(getattr(self._bound_proxy_session, 'snapshot', None), 'managed', False)
                       or os.getenv('MEDIACRAWLER_MANAGED_ROUTE') == '1')
        if managed and authority is None:
            raise RuntimeError('PROXY_CONFIG_INVALID')
        if skip_file and not managed:
            try:
                skip_aweme_ids = {
                    line.strip() for line in Path(skip_file).read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
                utils.logger.info(f"[DouYinCrawler.search] loaded {len(skip_aweme_ids)} reusable aweme IDs")
            except OSError as exc:
                utils.logger.warning(f"[DouYinCrawler.search] cannot read reusable ID file: {exc}")
        reusable_db = str(getattr(config, "DY_REUSABLE_CONTENT_DB", "") or "").strip()
        reusable_conn = None
        if reusable_db and not managed:
            try:
                reusable_conn = sqlite3.connect(reusable_db)
            except sqlite3.Error as exc:
                utils.logger.warning(f"[DouYinCrawler.search] cannot open reusable content DB: {exc}")
        configured_start_page = max(0, int(config.START_PAGE))
        # The CLI and product settings are one-based. Douyin's search offset is
        # zero-based; keep 0 as a backwards-compatible alias for the first page.
        start_page = configured_start_page - 1 if configured_start_page > 0 else 0
        page_limit = max(1, int(getattr(config, "DY_SEARCH_MAX_PAGES", 1000)))
        no_progress_limit = max(
            1, int(getattr(config, "DY_SEARCH_MAX_NO_PROGRESS_PAGES", 3))
        )
        resume_keyword = str(getattr(config, "SEARCH_RESUME_KEYWORD", "") or "").strip()
        resume_page = int(getattr(config, "SEARCH_RESUME_PAGE", -1))
        for raw_keyword in config.KEYWORDS.split(","):
            keyword = raw_keyword.strip()
            if not keyword:
                continue
            source_keyword_var.set(keyword)
            utils.logger.info(f"[DouYinCrawler.search] Current keyword: {keyword}")
            current_task_ids: set[str] = set()
            current_task_id = str(getattr(config, "CURRENT_TASK_ID", "") or "").strip()
            if authority is not None:
                current_task_ids.update(await self._owner_reusable_ids(keyword))
            elif reusable_conn and current_task_id:
                try:
                    rows = reusable_conn.execute(
                        "SELECT c.content_key, c.raw_item_path "
                        "FROM contents c JOIN content_matches m ON m.content_id = c.id "
                        "WHERE c.platform='dy' AND c.collection_status='complete' "
                        "AND m.task_id=? AND m.keyword=?",
                        (current_task_id, keyword),
                    ).fetchall()
                    for content_key, raw_item_path in rows:
                        if reusable_aweme_is_complete(raw_item_path, content_key):
                            current_task_ids.add(str(content_key))
                except sqlite3.Error as exc:
                    utils.logger.warning(f"[DouYinCrawler.search] current task lookup failed: {exc}")
            aweme_list: List[str] = sorted(current_task_ids)
            if aweme_list:
                utils.logger.info(
                    f"[DouYinCrawler.search] resume task with {len(aweme_list)} complete awemes"
                )
            seen_aweme_ids: set[str] = set()
            keyword_start_page = (
                max(start_page, resume_page)
                if resume_keyword and keyword == resume_keyword and resume_page >= 0
                else start_page
            )
            page = keyword_start_page
            requested_pages = 0
            no_progress_pages = 0
            dy_search_id = ""
            previous_has_more = False
            while len(aweme_list) < config.CRAWLER_MAX_NOTES_COUNT:
                if requested_pages >= page_limit:
                    raise DataFetchError(
                        "COLLECTION_INCOMPLETE: search_page_budget_exceeded"
                    )
                requested_pages += 1
                try:
                    utils.logger.info(f"[DouYinCrawler.search] search douyin keyword: {keyword}, page: {page}")
                    search_kwargs = {
                        "keyword": keyword,
                        "offset": page * dy_limit_count,
                        "publish_time": PublishTimeType(config.PUBLISH_TIME_TYPE),
                        "search_id": dy_search_id,
                    }
                    search_sort = {
                        "general": SearchSortType.GENERAL,
                        "most_liked": SearchSortType.MOST_LIKE,
                        "latest": SearchSortType.LATEST,
                    }.get(str(config.DY_SEARCH_SORT), SearchSortType.GENERAL)
                    if search_sort is not SearchSortType.GENERAL:
                        search_kwargs["sort_type"] = search_sort
                    posts_res = await self.dy_client.search_info_by_keyword(
                        **search_kwargs,
                    )
                    if posts_res.get("data") is None or posts_res.get("data") == []:
                        utils.logger.info(f"[DouYinCrawler.search] search douyin keyword: {keyword}, page: {page} is empty,{posts_res.get('data')}`")
                        # A keyword may have no results; emptiness only signals
                        # risk after the previous page promised more.
                        if previous_has_more and posts_res.get("status_code") in (None, 0, "0"):
                            record_silent_risk("search")
                        break
                    record_content_ok()
                except DataFetchError as exc:
                    utils.logger.error(
                        f"[DouYinCrawler.search] search douyin keyword: {keyword} failed: {exc}"
                    )
                    raise

                if "data" not in posts_res:
                    utils.logger.error(f"[DouYinCrawler.search] search douyin keyword: {keyword} failed，账号也许被风控了。")
                    break
                dy_search_id = posts_res.get("extra", {}).get("logid", "")
                page_aweme_list = []
                page_ids = []
                for post_item in posts_res.get("data"):
                    if len(aweme_list) >= config.CRAWLER_MAX_NOTES_COUNT:
                        break
                    try:
                        aweme_info: Dict = (post_item.get("aweme_info") or post_item.get("aweme_mix_info", {}).get("mix_items")[0])
                    except TypeError:
                        continue
                    aweme_id = aweme_info.get("aweme_id", "")
                    if not aweme_id or aweme_id in seen_aweme_ids:
                        continue
                    seen_aweme_ids.add(aweme_id)
                    page_ids.append(aweme_id)
                new_unique_count = len(page_ids)
                if authority is not None and page_ids:
                    skip_aweme_ids.update(await self._owner_reusable_ids(keyword, ids=page_ids))
                elif reusable_conn and page_ids:
                    placeholders = ",".join("?" for _ in page_ids)
                    try:
                        rows = reusable_conn.execute(
                            f"SELECT content_key, raw_item_path FROM contents WHERE platform='dy' AND collection_status='complete' AND content_key IN ({placeholders})",
                            page_ids,
                        ).fetchall()
                        for content_key, raw_item_path in rows:
                            if reusable_aweme_is_complete(raw_item_path, content_key):
                                skip_aweme_ids.add(str(content_key))
                    except sqlite3.Error as exc:
                        utils.logger.warning(f"[DouYinCrawler.search] reusable DB lookup failed: {exc}")
                for post_item in posts_res.get("data"):
                    if len(aweme_list) >= config.CRAWLER_MAX_NOTES_COUNT:
                        break
                    try:
                        aweme_info: Dict = (post_item.get("aweme_info") or post_item.get("aweme_mix_info", {}).get("mix_items")[0])
                    except TypeError:
                        continue
                    aweme_id = aweme_info.get("aweme_id", "")
                    if not aweme_id or aweme_id not in page_ids:
                        continue
                    page_ids.remove(aweme_id)
                    if aweme_id in skip_aweme_ids:
                        utils.logger.info(f"[DouYinCrawler.search] reuse existing aweme: {aweme_id}")
                        continue
                    await self.wait_for_content_slot(aweme_id)
                    aweme_list.append(aweme_id)
                    if config.STREAM_ITEMS:
                        await self.get_aweme_media(aweme_item=aweme_info)
                        await self.batch_get_note_comments([aweme_id])
                        await self._enrich_author_profile(aweme_info)
                        await self._attach_route_provenance(aweme_info)
                        await douyin_store.update_douyin_aweme(aweme_item=aweme_info)
                    else:
                        page_aweme_list.append(aweme_info.get("aweme_id", ""))
                        await self.get_aweme_media(aweme_item=aweme_info)
                        await self._enrich_author_profile(aweme_info)
                        await self._attach_route_provenance(aweme_info)
                        await douyin_store.update_douyin_aweme(aweme_item=aweme_info)
                
                # Batch get note comments for the current page
                if not config.STREAM_ITEMS:
                    await self.batch_get_note_comments(page_aweme_list)

                # Sleep after each page navigation
                completed_page = page
                page += 1
                slept = await jittered_sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[DouYinCrawler.search] Sleeping for {slept:.2f} seconds after page {completed_page}")
                utils.logger.info(f"[DouYinCrawler.search] keyword:{keyword}, aweme_list:{aweme_list}")
                has_more = posts_res.get("has_more")
                previous_has_more = bool(has_more)
                if has_more is not None and not bool(has_more):
                    break
                no_progress_pages = (
                    0 if new_unique_count else no_progress_pages + 1
                )
                if no_progress_pages >= no_progress_limit:
                    raise DataFetchError(
                        "COLLECTION_INCOMPLETE: search_pagination_stalled"
                    )
        if reusable_conn:
            reusable_conn.close()

    async def _owner_reusable_ids(self, keyword, *, ids=None):
        """The App owner resolves cache DB/files; a child sends bounded IDs only."""
        authority = getattr(self._bound_proxy_session, 'authority', None)
        if authority is None:
            raise RuntimeError('PROXY_CONFIG_INVALID')
        found = set()
        batches = ([ids[start:start + 100] for start in range(0, len(ids), 100)]
                   if ids is not None else [None])
        for batch in batches:
            offset = 0
            while True:
                payload = ({'keyword': keyword, 'ids': batch} if batch is not None
                           else {'keyword': keyword, 'offset': offset, 'limit': 128})
                value = await authority.acall('cache_lookup', payload)
                if (not isinstance(value, dict) or set(value) != {'ids', 'next_offset'} or
                        not isinstance(value['ids'], list) or
                        any(not isinstance(item, str) or not item or len(item) > 128 for item in value['ids'])):
                    raise RuntimeError('PROXY_CONFIG_INVALID')
                if batch is not None and not set(value['ids']) <= set(batch):
                    raise RuntimeError('PROXY_CONFIG_INVALID')
                found.update(value['ids'])
                next_offset = value['next_offset']
                if next_offset is None:
                    break
                if batch is not None or type(next_offset) is not int or not offset < next_offset <= 1000000:
                    raise RuntimeError('PROXY_CONFIG_INVALID')
                offset = next_offset
        return found

    async def _enrich_author_profile(self, aweme_info: Dict) -> None:
        """Fill a search result's slimmed author from the user profile API.

        Search responses report ``follower_count`` 0 and omit ``signature`` even
        for accounts with millions of followers, so the profile endpoint is the
        only source for the identity fields downstream triage scores on.  One
        request per author per run; the shared request scheduler paces the call,
        so no extra sleep belongs here.
        """
        if not getattr(config, "DY_FETCH_AUTHOR_PROFILE", False):
            return
        author = aweme_info.get("author") or {}
        sec_uid = str(author.get("sec_uid") or "").strip()
        if not sec_uid:
            return
        skip_pattern = str(getattr(config, "DY_SKIP_PROFILE_VERIFY_REGEX", "") or "")
        if skip_pattern:
            # The app discards official accounts anyway; spare the profile request.
            regex = _compiled_skip_profile_regex(skip_pattern)
            reason = str(author.get("enterprise_verify_reason") or "")
            if regex is not None and reason and regex.search(reason):
                utils.logger.info(f"[DouYinCrawler.search] skip author profile for official account {sec_uid}")
                return

        cache = getattr(self, "_author_profile_cache", None)
        if cache is None:
            cache = {}
            self._author_profile_cache = cache

        profile = cache.get(sec_uid)
        if profile is None:
            try:
                res = await self.dy_client.get_user_info(sec_uid)
            except asyncio.CancelledError:
                raise
            except PlatformRateLimitedError:
                raise
            except DataFetchError as ex:
                if "ACCOUNT_VERIFY" in str(ex) or "ACCOUNT_AUTH_INVALID" in str(ex):
                    raise
                utils.logger.warning(
                    f"[DouYinCrawler.search] author profile fetch failed for {sec_uid}: {ex}"
                )
                cache[sec_uid] = {}
                return
            except CollectionRouteError:
                raise
            except Exception as ex:
                utils.logger.warning(
                    f"[DouYinCrawler.search] author profile fetch failed for {sec_uid}: {ex}"
                )
                cache[sec_uid] = {}
                return
            profile = (res or {}).get("user") or {}
            cache[sec_uid] = profile
            utils.logger.info(
                f"[DouYinCrawler.search] author profile: "
                f"{profile.get('nickname') or author.get('nickname')} "
                f"followers={profile.get('follower_count')}"
            )

        for field in (
            "follower_count",
            "max_follower_count",
            "signature",
            "custom_verify",
            "enterprise_verify_reason",
            "verification_type",
            "following_count",
            "total_favorited",
            "aweme_count",
            "ip_location",
        ):
            if field in profile:
                author[field] = profile[field]
        aweme_info["author"] = author

    async def get_specified_awemes(self):
        """Get the information and comments of the specified post from URLs or IDs"""
        utils.logger.info("[DouYinCrawler.get_specified_awemes] Parsing video URLs...")
        aweme_id_list = []
        for video_url in config.DY_SPECIFIED_ID_LIST:
            try:
                video_info = parse_video_info_from_url(video_url)

                # Handling short links
                if video_info.url_type == "short":
                    utils.logger.info(f"[DouYinCrawler.get_specified_awemes] Resolving short link: {video_url}")
                    resolved_url = await self.dy_client.resolve_short_url(video_url)
                    if resolved_url:
                        # Extract video ID from parsed URL
                        video_info = parse_video_info_from_url(resolved_url)
                        utils.logger.info(f"[DouYinCrawler.get_specified_awemes] Short link resolved to aweme ID: {video_info.aweme_id}")
                    else:
                        utils.logger.error(f"[DouYinCrawler.get_specified_awemes] Failed to resolve short link: {video_url}")
                        continue

                aweme_id_list.append(video_info.aweme_id)
                utils.logger.info(f"[DouYinCrawler.get_specified_awemes] Parsed aweme ID: {video_info.aweme_id} from {video_url}")
            except ValueError as e:
                utils.logger.error(f"[DouYinCrawler.get_specified_awemes] Failed to parse video URL: {e}")
                continue

        set_single_detail_target(len(set(aweme_id_list)) == 1)
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            asyncio.create_task(self.get_aweme_detail(aweme_id=aweme_id, semaphore=semaphore))
            for aweme_id in aweme_id_list
        ]
        try:
            aweme_details = await asyncio.gather(*task_list)
        except BaseException:
            # A risk error must stop every other post, not only its own.
            for task in task_list:
                task.cancel()
            await asyncio.gather(*task_list, return_exceptions=True)
            raise
        for aweme_detail in aweme_details:
            if aweme_detail is not None:
                await self._attach_route_provenance(aweme_detail)
                await douyin_store.update_douyin_aweme(aweme_item=aweme_detail)
                await self.get_aweme_media(aweme_item=aweme_detail)
        await self.batch_get_note_comments(aweme_id_list)

    _RISK_MARKERS = ("ACCOUNT_VERIFY", "ACCOUNT_AUTH_INVALID", "PLATFORM_RATE_LIMITED")

    def _record_risk_halt(self, ex: BaseException) -> None:
        """Remember an account/platform risk error so sibling tasks never send another request."""
        if isinstance(ex, PlatformRateLimitedError) or any(m in str(ex) for m in self._RISK_MARKERS):
            self._risk_halt = ex

    def _raise_if_risk_halted(self) -> None:
        ex = getattr(self, "_risk_halt", None)
        if ex is not None:
            raise type(ex)(str(ex))

    async def get_aweme_detail(self, aweme_id: str, semaphore: asyncio.Semaphore) -> Any:
        """Get note detail"""
        async with self.content_request_slot(semaphore, aweme_id):
            self._raise_if_risk_halted()
            try:
                result = await self.dy_client.get_video_by_id(aweme_id)
                if not isinstance(result, dict) or str(result.get("aweme_id") or "") != str(aweme_id):
                    raise CollectionIncompleteError(
                        "COLLECTION_INCOMPLETE: missing_or_mismatched_detail"
                    )
                # Sleep after fetching aweme detail
                slept = await jittered_sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[DouYinCrawler.get_aweme_detail] Sleeping for {slept:.2f} seconds after fetching aweme {aweme_id}")
                write_collection_status(aweme_id, "detail", "complete")
                return result
            except asyncio.CancelledError as ex:
                write_collection_status(
                    aweme_id, "detail", "interrupted", reason=failure_reason(ex)
                )
                raise
            except PlatformRateLimitedError as ex:
                self._record_risk_halt(ex)
                write_collection_status(
                    aweme_id, "detail", "interrupted", reason=failure_reason(ex)
                )
                raise
            except DataFetchError as ex:
                utils.logger.error(f"[DouYinCrawler.get_aweme_detail] Get aweme detail error: {ex}")
                write_collection_status(
                    aweme_id, "detail", "failed", reason=failure_reason(ex)
                )
                if "ACCOUNT_VERIFY" in str(ex) or "ACCOUNT_AUTH_INVALID" in str(ex):
                    self._record_risk_halt(ex)
                    raise
                raise CollectionIncompleteError(
                    f"COLLECTION_INCOMPLETE: {failure_reason(ex)}"
                ) from ex
            except (KeyError, CollectionIncompleteError) as ex:
                utils.logger.error(f"[DouYinCrawler.get_aweme_detail] have not fund note detail aweme_id:{aweme_id}, err: {ex}")
                write_collection_status(
                    aweme_id, "detail", "failed", reason=failure_reason(ex)
                )
                if isinstance(ex, CollectionIncompleteError):
                    raise
                raise CollectionIncompleteError(
                    "COLLECTION_INCOMPLETE: missing_or_mismatched_detail"
                ) from ex

    async def batch_get_note_comments(self, aweme_list: List[str]) -> None:
        """
        Batch get note comments
        """
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[DouYinCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        task_list: List[Task] = []
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        for aweme_id in aweme_list:
            task = asyncio.create_task(self.get_comments(aweme_id, semaphore), name=aweme_id)
            task_list.append(task)
        if len(task_list) > 0:
            try:
                await asyncio.gather(*task_list)
            except BaseException:
                for task in task_list:
                    task.cancel()
                await asyncio.gather(*task_list, return_exceptions=True)
                raise

    async def get_comments(self, aweme_id: str, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            self._raise_if_risk_halted()
            try:
                # Pass the list of keywords to the get_aweme_all_comments method
                # Use fixed crawling interval
                crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
                await self.dy_client.get_aweme_all_comments(
                    aweme_id=aweme_id,
                    crawl_interval=crawl_interval,
                    is_fetch_sub_comments=config.ENABLE_GET_SUB_COMMENTS,
                    callback=douyin_store.batch_update_dy_aweme_comments,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
                )
                # Sleep after fetching comments
                slept = await jittered_sleep(crawl_interval)
                utils.logger.info(f"[DouYinCrawler.get_comments] Sleeping for {slept:.2f} seconds after fetching comments for aweme {aweme_id}")
                utils.logger.info(f"[DouYinCrawler.get_comments] aweme_id: {aweme_id} comments have all been obtained and filtered ...")
            except DataFetchError as e:
                utils.logger.error(f"[DouYinCrawler.get_comments] aweme_id: {aweme_id} get comments failed, error: {e}")
                self._record_risk_halt(e)
                raise

    async def get_creators_and_videos(self) -> None:
        """
        Get the information and videos of the specified creator from URLs or IDs
        """
        utils.logger.info("[DouYinCrawler.get_creators_and_videos] Begin get douyin creators")
        utils.logger.info("[DouYinCrawler.get_creators_and_videos] Parsing creator URLs...")

        for creator_url in config.DY_CREATOR_ID_LIST:
            try:
                creator_info_parsed = parse_creator_info_from_url(creator_url)
                user_id = creator_info_parsed.sec_user_id
                utils.logger.info(f"[DouYinCrawler.get_creators_and_videos] Parsed sec_user_id: {user_id} from {creator_url}")
            except ValueError as e:
                utils.logger.error(f"[DouYinCrawler.get_creators_and_videos] Failed to parse creator URL: {e}")
                continue

            creator_info: Dict = await self.dy_client.get_user_info(user_id)
            if creator_info:
                await douyin_store.save_creator(user_id, creator=creator_info)

            # Get all video information of the creator
            all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail)

            if not config.STREAM_ITEMS:
                video_ids = [video_item.get("aweme_id") for video_item in all_video_list]
                await self.batch_get_note_comments(video_ids)

    async def fetch_creator_video_detail(self, video_list: List[Dict]):
        """
        Concurrently obtain the specified post list and save the data
        """
        if config.STREAM_ITEMS:
            semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
            # Commit each fully collected item before starting the next one.  A
            # later failure therefore preserves earlier successes without
            # starting work that cannot be accounted for after the exception.
            for post_item in video_list:
                await self.process_creator_aweme_stream_item(post_item, semaphore)
            return

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        for post_item in video_list:
            aweme_item = await self.get_aweme_detail(post_item.get("aweme_id"), semaphore)
            await self.get_aweme_media(aweme_item=aweme_item)
            await self._attach_route_provenance(aweme_item)
            await douyin_store.update_douyin_aweme(aweme_item=aweme_item)

    async def process_creator_aweme_stream_item(self, post_item: Dict, semaphore: asyncio.Semaphore) -> None:
        """Process one creator aweme fully, then persist it for streaming consumers."""
        aweme_id = post_item.get("aweme_id")
        if not aweme_id:
            return

        aweme_item = await self.get_aweme_detail(aweme_id, semaphore)
        await self.get_aweme_media(aweme_item=aweme_item)
        await self.batch_get_note_comments([aweme_id])
        await self._attach_route_provenance(aweme_item)
        await douyin_store.update_douyin_aweme(aweme_item=aweme_item)

    async def _attach_route_provenance(self, item):
        session = self._bound_proxy_session
        if session is not None and isinstance(item, dict):
            from uuid import uuid4
            item['collection_route_provenance'] = await session.provenance_async(
                observation_id=uuid4().hex,
                source_task_id=os.getenv('MEDIACRAWLER_SOURCE_TASK_ID', 'standalone'),
                source_batch_id=os.getenv('MEDIACRAWLER_SOURCE_BATCH_ID', 'standalone'),
            )

    async def create_douyin_client(self, httpx_proxy: Optional[str]) -> DouYinClient:
        """Create douyin client"""
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )  # type: ignore
        douyin_client = DouYinClient(
            proxy=httpx_proxy,
            headers={
                "User-Agent": await self.context_page.evaluate("() => navigator.userAgent"),
                "Cookie": cookie_str,
                "Host": "www.douyin.com",
                "Origin": "https://www.douyin.com/",
                "Referer": "https://www.douyin.com/",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
            **({'bound_proxy_session': self._bound_proxy_session}
               if self._bound_proxy_session is not None else {}),
        )
        return douyin_client

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        raise RuntimeError("Douyin requires the account-bound Camoufox launcher; legacy browser fallback is disabled")

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = False,
    ) -> BrowserContext:
        raise RuntimeError("Douyin requires the account-bound Camoufox launcher; CDP fallback is disabled")

    async def close(self) -> None:
        """Close browser context"""
        # If you use CDP mode, special processing is required
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[DouYinCrawler.close] Browser context closed ...")

    async def get_aweme_media(self, aweme_item: Dict):
        """
        获取抖音媒体，自动判断媒体类型是短视频还是帖子图片并下载

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not config.ENABLE_GET_MEIDAS:
            utils.logger.info(f"[DouYinCrawler.get_aweme_media] Crawling image mode is not enabled")
            return
        aweme_id = str(aweme_item.get("aweme_id") or "")
        # List of note urls. If it is a short video type, an empty list will be returned.
        note_download_url: List[str] = douyin_store._extract_note_image_list(aweme_item)
        try:
            if note_download_url:
                await self.get_aweme_images(aweme_item)
            else:
                await self.get_aweme_video(aweme_item)
        except asyncio.CancelledError as ex:
            write_collection_status(
                aweme_id, "media", "interrupted", reason=failure_reason(ex)
            )
            raise
        except (PlatformRateLimitedError, DataFetchError) as ex:
            write_collection_status(
                aweme_id, "media", "interrupted", reason=failure_reason(ex)
            )
            raise
        except CollectionIncompleteError:
            raise
        except (MediaDownloadError, KeyError, TypeError, ValueError) as ex:
            write_collection_status(
                aweme_id, "media", "failed", reason=failure_reason(ex)
            )
            raise CollectionIncompleteError(
                f"COLLECTION_INCOMPLETE: {failure_reason(ex)}"
            ) from ex
        write_collection_status(aweme_id, "media", "complete")

    async def get_aweme_images(self, aweme_item: Dict):
        """
        get aweme images. please use get_aweme_media

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        aweme_id = aweme_item.get("aweme_id")
        # List of note urls. If it is a short video type, an empty list will be returned.
        note_download_url: List[str] = douyin_store._extract_note_image_list(aweme_item)

        if not note_download_url:
            return
        failed_indices = []
        succeeded = 0
        for source_index, url in enumerate(note_download_url):
            if not url:
                failed_indices.append(source_index)
                continue
            try:
                content = await self._download_media_with_retry(url, [2])
            except MediaDownloadError:
                failed_indices.append(source_index)
                continue
            if not content:
                failed_indices.append(source_index)
                continue
            extension_file_name = f"{source_index:>03d}.jpeg"
            await douyin_store.update_dy_aweme_image(aweme_id, content, extension_file_name)
            succeeded += 1
        if failed_indices:
            write_collection_status(
                aweme_id,
                "media",
                "failed",
                reason="incomplete_images",
                expected=len(note_download_url),
                succeeded=succeeded,
                failed_indices=failed_indices,
            )
            raise CollectionIncompleteError("COLLECTION_INCOMPLETE: incomplete_images")

    async def _download_media_with_retry(self, url: str, budget: list[int]):
        """At most one transient retry per URL; callers share the total budget."""
        for attempt in range(2):
            if budget[0] <= 0:
                raise MediaDownloadError("media_retry_budget_exhausted")
            budget[0] -= 1
            try:
                return await self.dy_client.get_aweme_media(url, raise_on_error=True)
            except MediaDownloadError as exc:
                if exc.status_code == 429:
                    raise PlatformRateLimitedError("PLATFORM_RATE_LIMITED: media HTTP 429") from exc
                if not exc.retryable or attempt or budget[0] <= 0:
                    raise
                utils.logger.warning(
                    f"MEDIA_TRANSIENT_RETRY reason={exc} retry=1 remaining_budget={budget[0]}"
                )
                # The normal account/IP request gate still applies to the next request.
                await asyncio.sleep(1.0)

    async def get_aweme_video(self, aweme_item: Dict):
        """
        get aweme videos. please use get_aweme_media

        Args:
            aweme_item (Dict): 抖音作品详情
        """
        if not config.ENABLE_GET_MEIDAS:
            return
        aweme_id = aweme_item.get("aweme_id")

        # The video URL will always exist, but when it is a short video type, the file is actually an audio file.
        urls = douyin_store._extract_video_download_urls(aweme_item)
        if not urls:
            raise MediaDownloadError("missing_video_url")
        limit = max(1, min(3, int(config.DY_MEDIA_MAX_URL_ATTEMPTS)))
        # Shared by primary/alternate URLs, transient retries and the detail refresh.
        budget = [5]
        for generation in range(2):
            refresh_needed = False
            for url in urls[:limit]:
                if budget[0] <= 0:
                    break
                try:
                    content = await self._download_media_with_retry(url, budget)
                except MediaDownloadError as exc:
                    if exc.status_code == 429:
                        raise PlatformRateLimitedError(
                            "PLATFORM_RATE_LIMITED: media HTTP 429"
                        ) from exc
                    refresh_needed |= exc.status_code in {403, 410}
                    continue
                if content:
                    await douyin_store.update_dy_aweme_video(aweme_id, content, "video.mp4")
                    return
            if generation or not refresh_needed or not config.DY_MEDIA_REFRESH_ON_FAILURE or budget[0] <= 1:
                break
            # Refresh only after candidate exhaustion, at most once. Reuse the same account.
            try:
                budget[0] -= 1
                fresh = await self.dy_client.get_video_by_id(
                    aweme_id, operation="media_refresh"
                )
            except PlatformRateLimitedError:
                raise
            except DataFetchError as ex:
                utils.logger.warning(f"[DouYinCrawler.get_aweme_video] post={aweme_id} refresh_failed")
                if "ACCOUNT_VERIFY" in str(ex) or "ACCOUNT_AUTH_INVALID" in str(ex):
                    raise
                break
            except httpx.HTTPError:
                utils.logger.warning(f"[DouYinCrawler.get_aweme_video] post={aweme_id} refresh_failed")
                break
            urls = douyin_store._extract_video_download_urls(fresh or {})
        utils.logger.warning(f"[DouYinCrawler.get_aweme_video] post={aweme_id} media_download_failed")
        raise MediaDownloadError("media_download_failed")
