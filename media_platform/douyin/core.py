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
import sqlite3
import json
from pathlib import Path
import os
import random
from contextlib import asynccontextmanager
from asyncio import Task
from typing import Any, Dict, List, Optional, Tuple

import httpx

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import douyin as douyin_store
from tools import utils
from tools.browser_state import has_saved_cookie_state
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import DouYinClient
from .exception import DataFetchError, MediaDownloadError
from .field import PublishTimeType
from .help import parse_video_info_from_url, parse_creator_info_from_url
from .login import DouYinLogin


class DouYinCrawler(AbstractCrawler):
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
        self._cloak_profile = None

    async def apply_account_auth_state(self, browser_context) -> bool:
        if self._cloak_profile is None:
            return await super().apply_account_auth_state(browser_context)
        marker = self._cloak_profile["directory"] / "auth-imported"
        if marker.exists():
            return False  # Preserve newer state in this account's persistent profile.
        restored = await super().apply_account_auth_state(browser_context)
        if restored:
            marker.touch(mode=0o600)
        return restored

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
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
            await self._open_index_page()

            self.dy_client = await self.create_douyin_client(httpx_proxy_format)
            need_login = not await self.dy_client.pong(browser_context=self.browser_context)
            if need_login:
                # Account management owns login; background jobs never reopen a
                # different browser or silently switch to an interactive login.
                self.raise_account_auth_invalid()
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
        async with async_playwright() as playwright:
            try:
                yield playwright
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
        if os.getenv("MEDIACRAWLER_DY_BROWSER_ENGINE", "cloakbrowser") != "cloakbrowser":
            raise ValueError("Douyin requires CloakBrowser; legacy browser fallback is disabled")
        from tools.cloak_browser import launch_account_context
        root = Path(os.getenv("MEDIACRAWLER_CLOAK_PROFILE_ROOT", config.DY_CLOAK_PROFILE_ROOT))
        self.browser_context, self._cloak_profile = await launch_account_context(
            os.getenv("MEDIACRAWLER_ACCOUNT_ID", ""), root,
            headless=bool(config.HEADLESS) if self._headless_was_explicitly_set() else True,
            proxy=playwright_proxy,
        )

    async def _open_index_page(self) -> None:
        self.context_page = await self.browser_context.new_page()
        await self.context_page.goto(self.index_url)

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
        if skip_file:
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
        if reusable_db:
            try:
                reusable_conn = sqlite3.connect(reusable_db)
            except sqlite3.Error as exc:
                utils.logger.warning(f"[DouYinCrawler.search] cannot open reusable content DB: {exc}")
        if not config.STREAM_ITEMS and config.CRAWLER_MAX_NOTES_COUNT < dy_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = dy_limit_count
        start_page = config.START_PAGE  # start page number
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[DouYinCrawler.search] Current keyword: {keyword}")
            aweme_list: List[str] = []
            seen_aweme_ids: set[str] = set()
            page = 0
            dy_search_id = ""
            while len(aweme_list) < config.CRAWLER_MAX_NOTES_COUNT:
                if page < start_page:
                    utils.logger.info(f"[DouYinCrawler.search] Skip {page}")
                    page += 1
                    continue
                try:
                    utils.logger.info(f"[DouYinCrawler.search] search douyin keyword: {keyword}, page: {page}")
                    posts_res = await self.dy_client.search_info_by_keyword(
                        keyword=keyword,
                        offset=page * dy_limit_count,
                        publish_time=PublishTimeType(config.PUBLISH_TIME_TYPE),
                        search_id=dy_search_id,
                    )
                    if posts_res.get("data") is None or posts_res.get("data") == []:
                        utils.logger.info(f"[DouYinCrawler.search] search douyin keyword: {keyword}, page: {page} is empty,{posts_res.get('data')}`")
                        break
                except DataFetchError as exc:
                    utils.logger.error(
                        f"[DouYinCrawler.search] search douyin keyword: {keyword} failed: {exc}"
                    )
                    raise

                page += 1
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
                if reusable_conn and page_ids:
                    placeholders = ",".join("?" for _ in page_ids)
                    try:
                        rows = reusable_conn.execute(
                            f"SELECT content_key, raw_item_path FROM contents WHERE platform='dy' AND collection_status='complete' AND content_key IN ({placeholders})",
                            page_ids,
                        ).fetchall()
                        for content_key, raw_item_path in rows:
                            try:
                                payload = json.loads(Path(str(raw_item_path)).read_text(encoding="utf-8"))
                                item = payload.get("item") if isinstance(payload, dict) else None
                                if isinstance(item, dict) and str(item.get("aweme_id") or "") == str(content_key):
                                    skip_aweme_ids.add(str(content_key))
                            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                                continue
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
                        await douyin_store.update_douyin_aweme(aweme_item=aweme_info)
                    else:
                        page_aweme_list.append(aweme_info.get("aweme_id", ""))
                        await douyin_store.update_douyin_aweme(aweme_item=aweme_info)
                        await self.get_aweme_media(aweme_item=aweme_info)
                
                # Batch get note comments for the current page
                if not config.STREAM_ITEMS:
                    await self.batch_get_note_comments(page_aweme_list)

                # Sleep after each page navigation
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[DouYinCrawler.search] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {page-1}")
                utils.logger.info(f"[DouYinCrawler.search] keyword:{keyword}, aweme_list:{aweme_list}")
        if reusable_conn:
            reusable_conn.close()

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

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_aweme_detail(aweme_id=aweme_id, semaphore=semaphore) for aweme_id in aweme_id_list]
        aweme_details = await asyncio.gather(*task_list)
        for aweme_detail in aweme_details:
            if aweme_detail is not None:
                await douyin_store.update_douyin_aweme(aweme_item=aweme_detail)
                await self.get_aweme_media(aweme_item=aweme_detail)
        await self.batch_get_note_comments(aweme_id_list)

    async def get_aweme_detail(self, aweme_id: str, semaphore: asyncio.Semaphore) -> Any:
        """Get note detail"""
        async with self.content_request_slot(semaphore, aweme_id):
            try:
                result = await self.dy_client.get_video_by_id(aweme_id)
                # Sleep after fetching aweme detail
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[DouYinCrawler.get_aweme_detail] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching aweme {aweme_id}")
                return result
            except DataFetchError as ex:
                utils.logger.error(f"[DouYinCrawler.get_aweme_detail] Get aweme detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[DouYinCrawler.get_aweme_detail] have not fund note detail aweme_id:{aweme_id}, err: {ex}")
                return None

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
                await asyncio.sleep(crawl_interval)
                utils.logger.info(f"[DouYinCrawler.get_comments] Sleeping for {crawl_interval} seconds after fetching comments for aweme {aweme_id}")
                utils.logger.info(f"[DouYinCrawler.get_comments] aweme_id: {aweme_id} comments have all been obtained and filtered ...")
            except DataFetchError as e:
                utils.logger.error(f"[DouYinCrawler.get_comments] aweme_id: {aweme_id} get comments failed, error: {e}")
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
            task_list = [
                self.process_creator_aweme_stream_item(post_item, semaphore)
                for post_item in video_list
            ]
            if task_list:
                await asyncio.gather(*task_list)
            return

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_aweme_detail(post_item.get("aweme_id"), semaphore) for post_item in video_list]

        note_details = await asyncio.gather(*task_list)
        for aweme_item in note_details:
            if aweme_item is not None:
                await douyin_store.update_douyin_aweme(aweme_item=aweme_item)
                await self.get_aweme_media(aweme_item=aweme_item)

    async def process_creator_aweme_stream_item(self, post_item: Dict, semaphore: asyncio.Semaphore) -> None:
        """Process one creator aweme fully, then persist it for streaming consumers."""
        aweme_id = post_item.get("aweme_id")
        if not aweme_id:
            return

        aweme_item = await self.get_aweme_detail(aweme_id, semaphore)
        if aweme_item is None:
            aweme_item = post_item

        await self.get_aweme_media(aweme_item=aweme_item)
        await self.batch_get_note_comments([aweme_id])
        await douyin_store.update_douyin_aweme(aweme_item=aweme_item)

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
        )
        return douyin_client

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        if config.SAVE_LOGIN_STATE:
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={
                    "width": 1920,
                    "height": 1080
                },
                user_agent=user_agent,
            )  # type: ignore
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        使用CDP模式启动浏览器
        """
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Add anti-detection script
            await self.cdp_manager.add_stealth_script()

            # Show browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[DouYinCrawler] CDP浏览器信息: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[DouYinCrawler] CDP模式启动失败，回退到标准模式: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

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
        # List of note urls. If it is a short video type, an empty list will be returned.
        note_download_url: List[str] = douyin_store._extract_note_image_list(aweme_item)
        # The video URL will always exist, but when it is a short video type, the file is actually an audio file.
        video_download_url: str = douyin_store._extract_video_download_url(aweme_item)
        # TODO: Douyin does not adopt the audio and video separation strategy, so the audio can be separated from the original video and will not be extracted for the time being.
        if note_download_url:
            await self.get_aweme_images(aweme_item)
        else:
            await self.get_aweme_video(aweme_item)

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
        picNum = 0
        for url in note_download_url:
            if not url:
                continue
            content = await self.dy_client.get_aweme_media(url)
            await asyncio.sleep(random.random())
            if content is None:
                continue
            extension_file_name = f"{picNum:>03d}.jpeg"
            picNum += 1
            await douyin_store.update_dy_aweme_image(aweme_id, content, extension_file_name)

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
            return
        limit = max(1, min(3, int(config.DY_MEDIA_MAX_URL_ATTEMPTS)))
        for generation in range(2):
            refresh_needed = False
            for url in urls[:limit]:
                try:
                    content = await self.dy_client.get_aweme_media(url, raise_on_error=True)
                except MediaDownloadError as exc:
                    if exc.status_code == 429:
                        utils.logger.warning(f"[DouYinCrawler.get_aweme_video] post={aweme_id} media_rate_limited")
                        return
                    refresh_needed |= exc.status_code in {403, 410}
                    continue
                if content:
                    await douyin_store.update_dy_aweme_video(aweme_id, content, "video.mp4")
                    return
            if generation or not refresh_needed or not config.DY_MEDIA_REFRESH_ON_FAILURE:
                break
            # Refresh only after candidate exhaustion, at most once. Reuse the same account.
            await self.dy_client.wait_for_media_slot()
            try:
                fresh = await self.dy_client.get_video_by_id(aweme_id)
            except httpx.HTTPError:
                utils.logger.warning(f"[DouYinCrawler.get_aweme_video] post={aweme_id} refresh_failed")
                break
            urls = douyin_store._extract_video_download_urls(fresh or {})
        utils.logger.warning(f"[DouYinCrawler.get_aweme_video] post={aweme_id} media_download_failed")
