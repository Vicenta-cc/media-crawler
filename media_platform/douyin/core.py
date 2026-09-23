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
from tools.collection_status import (
    CollectionIncompleteError,
    failure_reason,
    write_collection_status,
)
from var import crawler_type_var, source_keyword_var

from .client import DouYinClient
from .exception import DataFetchError, MediaDownloadError, PlatformRateLimitedError
from .field import PublishTimeType, SearchSortType
from .help import parse_video_info_from_url, parse_creator_info_from_url
from .login import DouYinLogin


def reusable_aweme_is_complete(raw_item_path: str, content_key: str) -> bool:
    """Validate the indexed payload and every media file it promises."""
    try:
        path = Path(str(raw_item_path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        item = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item, dict) or str(item.get("aweme_id") or "") != str(content_key):
            return False
        if not str(item.get("aweme_url") or "").strip():
            return False
        if path.parent.name != "raw_items":
            return False
        output_root = path.parent.parent
        note_urls = item.get("note_download_url") or ""
        if isinstance(note_urls, str):
            note_urls = [value for value in note_urls.split(",") if value.strip()]
        elif isinstance(note_urls, list):
            note_urls = [value for value in note_urls if str(value).strip()]
        else:
            return False
        if note_urls:
            image_root = output_root / "crawler" / "douyin" / "images" / str(content_key)
            for index in range(len(note_urls)):
                media = image_root / f"{index:03d}.jpeg"
                if not media.is_file() or media.stat().st_size == 0:
                    return False
                if media.read_bytes()[:2] != b"\xff\xd8":
                    return False
            return True
        if not str(item.get("video_download_url") or "").strip():
            return False
        media = output_root / "crawler" / "douyin" / "videos" / str(content_key) / "video.mp4"
        if not media.is_file() or media.stat().st_size == 0:
            return False
        return b"ftyp" in media.read_bytes()[:32]
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return False


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
            if reusable_conn and current_task_id:
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
                        break
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
                if reusable_conn and page_ids:
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
                        await douyin_store.update_douyin_aweme(aweme_item=aweme_info)
                    else:
                        page_aweme_list.append(aweme_info.get("aweme_id", ""))
                        await self.get_aweme_media(aweme_item=aweme_info)
                        await self._enrich_author_profile(aweme_info)
                        await douyin_store.update_douyin_aweme(aweme_item=aweme_info)
                
                # Batch get note comments for the current page
                if not config.STREAM_ITEMS:
                    await self.batch_get_note_comments(page_aweme_list)

                # Sleep after each page navigation
                completed_page = page
                page += 1
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[DouYinCrawler.search] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {completed_page}")
                utils.logger.info(f"[DouYinCrawler.search] keyword:{keyword}, aweme_list:{aweme_list}")
                has_more = posts_res.get("has_more")
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
                if not isinstance(result, dict) or str(result.get("aweme_id") or "") != str(aweme_id):
                    raise CollectionIncompleteError(
                        "COLLECTION_INCOMPLETE: missing_or_mismatched_detail"
                    )
                # Sleep after fetching aweme detail
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[DouYinCrawler.get_aweme_detail] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching aweme {aweme_id}")
                write_collection_status(aweme_id, "detail", "complete")
                return result
            except asyncio.CancelledError as ex:
                write_collection_status(
                    aweme_id, "detail", "interrupted", reason=failure_reason(ex)
                )
                raise
            except PlatformRateLimitedError as ex:
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
            await douyin_store.update_douyin_aweme(aweme_item=aweme_item)

    async def process_creator_aweme_stream_item(self, post_item: Dict, semaphore: asyncio.Semaphore) -> None:
        """Process one creator aweme fully, then persist it for streaming consumers."""
        aweme_id = post_item.get("aweme_id")
        if not aweme_id:
            return

        aweme_item = await self.get_aweme_detail(aweme_id, semaphore)
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
                content = await self.dy_client.get_aweme_media(url, raise_on_error=True)
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
        for generation in range(2):
            refresh_needed = False
            for url in urls[:limit]:
                try:
                    content = await self.dy_client.get_aweme_media(url, raise_on_error=True)
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
            if generation or not refresh_needed or not config.DY_MEDIA_REFRESH_ON_FAILURE:
                break
            # Refresh only after candidate exhaustion, at most once. Reuse the same account.
            try:
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
