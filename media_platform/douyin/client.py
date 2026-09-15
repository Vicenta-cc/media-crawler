# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/client.py
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
import copy
import json
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Callable, Dict, Union, Optional

import httpx
from playwright.async_api import BrowserContext

import config
from base.base_crawler import AbstractApiClient
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils
from tools.httpx_util import make_async_client
from tools.persistent_request_gate import PersistentRequestGate
from var import request_keyword_var

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import *
from .field import *
from .help import *


class DouYinClient(AbstractApiClient, ProxyRefreshMixin):

    _VERIFICATION_MARKERS = ("verify", "captcha", "验证", "风控")
    _VERIFICATION_CONTROL_KEYS = (
        "status_msg",
        "status_message",
        "message",
        "error_msg",
        "error_message",
        "prompts",
    )

    def __init__(
        self,
        timeout=60,  # If the crawl media option is turned on, Douyin’s short videos will require a longer timeout.
        proxy=None,
        *,
        headers: Dict,
        playwright_page: Optional[Page],
        cookie_dict: Dict,
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://www.douyin.com"
        self.cookie_urls = [
            "https://douyin.com",
            self._host,
            "https://creator.douyin.com",
            "https://douhot.douyin.com",
            "https://live.douyin.com",
        ]
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._media_lock = asyncio.Lock()
        self._next_media_request = 0.0
        gate_db = str(getattr(config, "DY_REQUEST_SCHEDULER_DB", "") or "").strip()
        self._persistent_gate = (
            PersistentRequestGate(
                gate_db,
                min_interval=float(getattr(config, "DY_REQUEST_MIN_INTERVAL", 2.0)),
                per_minute=int(getattr(config, "DY_REQUESTS_PER_MINUTE", 30)),
            ) if gate_db else None
        )
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)

    async def __process_req_params(
        self,
        uri: str,
        params: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        request_method="GET",
    ):

        if not params:
            return
        headers = headers or self.headers
        local_storage: Dict = await self.playwright_page.evaluate("() => window.localStorage")  # type: ignore
        common_params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "version_code": "190600",
            "version_name": "19.6.0",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "cookie_enabled": "true",
            "browser_language": "zh-CN",
            "browser_platform": "MacIntel",
            "browser_name": "Chrome",
            "browser_version": "125.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "os_name": "Mac OS",
            "os_version": "10.15.7",
            "cpu_core_num": "8",
            "device_memory": "8",
            "engine_version": "109.0",
            "platform": "PC",
            "screen_width": "2560",
            "screen_height": "1440",
            'effective_type': '4g',
            "round_trip_time": "50",
            "webid": get_web_id(),
            "msToken": local_storage.get("xmst"),
        }
        params.update(common_params)
        query_string = urllib.parse.urlencode(params)

        # 20240927 a-bogus update (JS version)
        post_data = {}
        if request_method == "POST":
            post_data = params

        if "/v1/web/general/search" not in uri:
            a_bogus = await get_a_bogus(uri, query_string, post_data, headers["User-Agent"], self.playwright_page)
            params["a_bogus"] = a_bogus

    @classmethod
    def _requires_verification(cls, response: httpx.Response, payload: Any) -> bool:
        if response.status_code in {401, 403}:
            return True

        if not isinstance(payload, dict):
            body = response.text.strip().lower()
            return body == "blocked" or any(marker in body for marker in cls._VERIFICATION_MARKERS)

        # Normal Douyin post payloads contain fields such as custom_verify and may
        # contain the word "verify" in user-authored text. Inspect only response
        # control fields so valid content cannot be mistaken for a challenge.
        for key in ("verify", "captcha", "verification", "need_verify", "verify_data", "captcha_data"):
            if payload.get(key):
                return True

        # Search can return HTTP 200 / status_code=0 / data=[] for a challenge.
        # These are explicit control values, not matches in user-authored text.
        nil_info = payload.get("search_nil_info")
        if isinstance(nil_info, dict):
            challenge_signals = ("verify_check", "verify_required", "captcha_required")
            if any(
                nil_info.get(key) in challenge_signals
                for key in ("search_nil_type", "search_nil_item")
            ):
                return True

        status_code = payload.get("status_code")
        if status_code in (None, 0, "0"):
            return False

        control_values = [payload.get(key) for key in cls._VERIFICATION_CONTROL_KEYS]
        extra = payload.get("extra")
        if isinstance(extra, dict):
            control_values.extend(extra.get(key) for key in cls._VERIFICATION_CONTROL_KEYS)
        control_text = json.dumps(control_values, ensure_ascii=False).lower()
        return any(marker in control_text for marker in cls._VERIFICATION_MARKERS)

    async def request(self, method, url, **kwargs):
        if self._persistent_gate:
            await self._persistent_gate.acquire()
        # Check whether the proxy has expired before each request
        await self._refresh_proxy_if_expired()

        async with make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = None

        if self._requires_verification(response, payload):
            utils.logger.error("ACCOUNT_VERIFY: platform request requires verification")
            raise DataFetchError("ACCOUNT_VERIFY")
        if response.status_code >= 400:
            raise DataFetchError(f"HTTP {response.status_code}")
        if payload is None:
            raise DataFetchError("platform response is empty or not valid JSON")
        return payload

    async def get(self, uri: str, params: Optional[Dict] = None, headers: Optional[Dict] = None):
        """
        GET请求
        """
        await self.__process_req_params(uri, params, headers)
        headers = headers or self.headers
        return await self.request(method="GET", url=f"{self._host}{uri}", params=params, headers=headers)

    async def post(self, uri: str, data: dict, headers: Optional[Dict] = None):
        await self.__process_req_params(uri, data, headers)
        headers = headers or self.headers
        return await self.request(method="POST", url=f"{self._host}{uri}", data=data, headers=headers)

    async def pong(self, browser_context: BrowserContext) -> bool:
        local_storage = await self.playwright_page.evaluate("() => window.localStorage")
        if local_storage.get("HasUserLogin", "") == "1":
            return True

        _, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=self.cookie_urls,
        )
        return cookie_dict.get("LOGIN_STATUS") == "1"

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def search_info_by_keyword(
        self,
        keyword: str,
        offset: int = 0,
        search_channel: SearchChannelType = SearchChannelType.GENERAL,
        sort_type: SearchSortType = SearchSortType.GENERAL,
        publish_time: PublishTimeType = PublishTimeType.UNLIMITED,
        search_id: str = "",
    ):
        """
        DouYin Web Search API
        :param keyword:
        :param offset:
        :param search_channel:
        :param sort_type:
        :param publish_time: ·
        :param search_id: ·
        :return:
        """
        query_params = {
            'search_channel': search_channel.value,
            'enable_history': '1',
            'keyword': keyword,
            'search_source': 'tab_search',
            'query_correct_type': '1',
            'is_filter_search': '0',
            'from_group_id': '7378810571505847586',
            'offset': offset,
            'count': '15',
            'need_filter_settings': '1',
            'list_type': 'multi',
            'search_id': search_id,
        }
        if sort_type.value != SearchSortType.GENERAL.value or publish_time.value != PublishTimeType.UNLIMITED.value:
            query_params["filter_selected"] = json.dumps({"sort_type": str(sort_type.value), "publish_time": str(publish_time.value)})
            query_params["is_filter_search"] = 1
            query_params["search_source"] = "tab_search"
        referer_url = f"https://www.douyin.com/search/{keyword}?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general"
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get("/aweme/v1/web/general/search/single/", query_params, headers=headers)

    async def get_video_by_id(self, aweme_id: str) -> Any:
        """
        DouYin Video Detail API
        :param aweme_id:
        :return:
        """
        params = {"aweme_id": aweme_id}
        headers = copy.copy(self.headers)
        del headers["Origin"]
        res = await self.get("/aweme/v1/web/aweme/detail/", params, headers)
        return res.get("aweme_detail", {})

    async def get_aweme_comments(self, aweme_id: str, cursor: int = 0):
        """get note comments

        """
        uri = "/aweme/v1/web/comment/list/"
        params = {"aweme_id": aweme_id, "cursor": cursor, "count": 20, "item_type": 0}
        keywords = request_keyword_var.get()
        referer_url = "https://www.douyin.com/search/" + keywords + '?aid=3a3cec5a-9e27-4040-b6aa-ef548c2c1138&publish_time=0&sort_type=0&source=search_history&type=general'
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get(uri, params)

    async def get_sub_comments(self, aweme_id: str, comment_id: str, cursor: int = 0):
        """
            获取子评论
        """
        uri = "/aweme/v1/web/comment/list/reply/"
        params = {
            'comment_id': comment_id,
            "cursor": cursor,
            "count": 20,
            "item_type": 0,
            "item_id": aweme_id,
        }
        keywords = request_keyword_var.get()
        referer_url = "https://www.douyin.com/search/" + keywords + '?aid=3a3cec5a-9e27-4040-b6aa-ef548c2c1138&publish_time=0&sort_type=0&source=search_history&type=general'
        headers = copy.copy(self.headers)
        headers["Referer"] = urllib.parse.quote(referer_url, safe=':/')
        return await self.get(uri, params)

    async def get_aweme_all_comments(
        self,
        aweme_id: str,
        crawl_interval: float = 1.0,
        is_fetch_sub_comments=False,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ):
        """
        获取帖子的所有评论，包括子评论
        :param aweme_id: 帖子ID
        :param crawl_interval: 抓取间隔
        :param is_fetch_sub_comments: 是否抓取子评论
        :param callback: 回调函数，用于处理抓取到的评论
        :param max_count: 一次帖子爬取的最大评论数量
        :return: 评论列表
        """
        result = []
        seen_comment_ids: set[str] = set()
        comments_has_more = 1
        comments_cursor = 0
        previous_cursor = None
        while comments_has_more and len(result) < max_count:
            comments_res = await self.get_aweme_comments(aweme_id, comments_cursor)
            comments_has_more = comments_res.get("has_more", 0)
            next_cursor = comments_res.get("cursor", 0)
            comments = comments_res.get("comments", [])
            if not comments:
                if next_cursor == comments_cursor:
                    break
                previous_cursor, comments_cursor = comments_cursor, next_cursor
                if comments_cursor == previous_cursor:
                    break
                continue
            comments = [
                comment for comment in comments
                if self._take_unique_comment(comment, seen_comment_ids)
            ]
            if len(result) + len(comments) > max_count:
                comments = comments[:max_count - len(result)]
            result.extend(comments)
            if callback:  # If there is a callback function, execute the callback function
                await callback(aweme_id, comments)

            await asyncio.sleep(crawl_interval)
            previous_cursor, comments_cursor = comments_cursor, next_cursor
            if comments_cursor == previous_cursor and comments_has_more:
                break
            if not is_fetch_sub_comments:
                continue
            # Get secondary reviews
            for comment in comments:
                reply_comment_total = comment.get("reply_comment_total")

                if reply_comment_total > 0:
                    comment_id = comment.get("cid")
                    sub_comments_has_more = 1
                    sub_comments_cursor = 0

                    while sub_comments_has_more:
                        sub_comments_res = await self.get_sub_comments(aweme_id, comment_id, sub_comments_cursor)
                        sub_comments_has_more = sub_comments_res.get("has_more", 0)
                        next_sub_comments_cursor = sub_comments_res.get("cursor", 0)
                        sub_comments = sub_comments_res.get("comments", [])

                        if not sub_comments:
                            if next_sub_comments_cursor == sub_comments_cursor:
                                break
                            sub_comments_cursor = next_sub_comments_cursor
                            continue
                        sub_comments = [
                            comment for comment in sub_comments
                            if self._take_unique_comment(comment, seen_comment_ids)
                        ]
                        if len(result) + len(sub_comments) > max_count:
                            sub_comments = sub_comments[:max_count - len(result)]
                        result.extend(sub_comments)
                        if callback:  # If there is a callback function, execute the callback function
                            await callback(aweme_id, sub_comments)
                        await asyncio.sleep(crawl_interval)
                        if next_sub_comments_cursor == sub_comments_cursor and sub_comments_has_more:
                            break
                        sub_comments_cursor = next_sub_comments_cursor
        return result

    @staticmethod
    def _take_unique_comment(comment: Dict, seen_comment_ids: set[str]) -> bool:
        """Return True once per platform comment ID; retain records without an ID."""
        comment_id = str((comment or {}).get("cid") or "").strip()
        if not comment_id:
            return True
        if comment_id in seen_comment_ids:
            return False
        seen_comment_ids.add(comment_id)
        return True

    async def get_user_info(self, sec_user_id: str):
        uri = "/aweme/v1/web/user/profile/other/"
        params = {
            "sec_user_id": sec_user_id,
            "publish_video_strategy_type": 2,
            "personal_center_strategy": 1,
        }
        return await self.get(uri, params)

    async def get_user_aweme_posts(self, sec_user_id: str, max_cursor: str = "") -> Dict:
        uri = "/aweme/v1/web/aweme/post/"
        params = {
            "sec_user_id": sec_user_id,
            "count": 18,
            "max_cursor": max_cursor,
            "locate_query": "false",
            "publish_video_strategy_type": 2,
        }
        return await self.get(uri, params)

    async def get_all_user_aweme_posts(self, sec_user_id: str, callback: Optional[Callable] = None):
        posts_has_more = 1
        max_cursor = ""
        result = []
        while posts_has_more == 1 and len(result) < config.CRAWLER_MAX_NOTES_COUNT:
            aweme_post_res = await self.get_user_aweme_posts(sec_user_id, max_cursor)
            posts_has_more = aweme_post_res.get("has_more", 0)
            max_cursor = aweme_post_res.get("max_cursor")
            aweme_list = aweme_post_res.get("aweme_list") if aweme_post_res.get("aweme_list") else []
            remaining = config.CRAWLER_MAX_NOTES_COUNT - len(result)
            aweme_list = aweme_list[:remaining]
            utils.logger.info(f"[DouYinClient.get_all_user_aweme_posts] get sec_user_id:{sec_user_id} video len : {len(aweme_list)}")
            if callback:
                await callback(aweme_list)
            result.extend(aweme_list)
        return result

    async def wait_for_media_slot(self):
        async with self._media_lock:
            await asyncio.sleep(max(0.0, self._next_media_request - time.monotonic()))
            interval = max(0.0, float(config.DY_MEDIA_REQUEST_INTERVAL))
            self._next_media_request = time.monotonic() + interval

    async def get_aweme_media(self, url: str, *, raise_on_error: bool = False) -> Union[bytes, None]:
        if self._persistent_gate:
            await self._persistent_gate.acquire()
        await self.wait_for_media_slot()
        # Do not forward API Host, Cookie or Authorization to another CDN domain.
        headers = {"Referer": "https://www.douyin.com/"}
        if self.headers.get("User-Agent"):
            headers["User-Agent"] = self.headers["User-Agent"]
        host = urllib.parse.urlsplit(url).hostname
        final_host = host
        try:
            async with make_async_client(proxy=self.proxy) as client:
                response = await client.request("GET", url, headers=headers,
                                                timeout=self.timeout, follow_redirects=True)
            final_host = response.url.host
            if response.status_code != 200:
                raise MediaDownloadError("media_http_error", response.status_code)
            mime = response.headers.get("content-type", "").split(";")[0].lower()
            if not response.content or mime.startswith("text/") or mime in {
                "application/json", "application/xml", "application/xhtml+xml"
            }:
                raise MediaDownloadError("invalid_media_response", response.status_code)
            return response.content
        except (httpx.HTTPError, MediaDownloadError) as exc:
            status = getattr(exc, "status_code", None)
            # Signed URLs, cookies and response bodies never go into failure logs.
            utils.logger.warning(
                f"[DouYinClient.get_aweme_media] host={host} final_host={final_host} "
                f"status={status} error={str(exc) if isinstance(exc, MediaDownloadError) else type(exc).__name__}"
            )
            if raise_on_error:
                if isinstance(exc, MediaDownloadError):
                    raise
                raise MediaDownloadError(type(exc).__name__) from None
            return None

    async def resolve_short_url(self, short_url: str) -> str:
        """
        解析抖音短链接,获取重定向后的真实URL
        Args:
            short_url: 短链接,如 https://v.douyin.com/iF12345ABC/
        Returns:
            重定向后的完整URL
        """
        async with make_async_client(proxy=self.proxy, follow_redirects=False) as client:
            try:
                utils.logger.info(f"[DouYinClient.resolve_short_url] Resolving short URL: {short_url}")
                response = await client.get(short_url, timeout=10)

                # Short links usually return a 302 redirect
                if response.status_code in [301, 302, 303, 307, 308]:
                    redirect_url = response.headers.get("Location", "")
                    utils.logger.info(f"[DouYinClient.resolve_short_url] Resolved to: {redirect_url}")
                    return redirect_url
                else:
                    utils.logger.warning(f"[DouYinClient.resolve_short_url] Unexpected status code: {response.status_code}")
                    return ""
            except Exception as e:
                utils.logger.error(f"[DouYinClient.resolve_short_url] Failed to resolve short URL: {e}")
                return ""
