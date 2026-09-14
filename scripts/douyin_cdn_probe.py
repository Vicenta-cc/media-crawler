"""Bounded live CDN comparison; injected auth stays in memory, URLs are redacted.

Run with MEDIACRAWLER_ACCOUNT_AUTH_STATE_B64 and the crawler's Python environment.
Successful comparisons read only a small prefix (ordinary GET, no Range header).
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.field import PublishTimeType
from tools.httpx_util import make_async_client


def candidates(item):
    result = []
    for field in ('play_addr_h264', 'play_addr_256', 'play_addr'):
        urls = item.get('video', {}).get(field, {}).get('url_list', [])
        for url in reversed(urls):
            if url and url not in result:
                result.append(url)
    return result


async def main(args):
    config.PLATFORM = 'dy'
    config.CRAWLER_TYPE = 'search'
    config.SAVE_LOGIN_STATE = False
    config.ENABLE_BACKGROUND_BROWSER_MODE = False
    config.ENABLE_CDP_MODE = False
    config.HEADLESS = True
    config.ENABLE_IP_PROXY = False
    config.ENABLE_GET_COMMENTS = False
    config.ENABLE_GET_MEIDAS = True
    config.CRAWLER_MAX_ITEMS_PER_MINUTE = 1
    config.SAVE_DATA_PATH = tempfile.mkdtemp(prefix='douyin-cdn-full-')
    records = []
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    next_request = 0.0

    def save(record):
        records.append(record)
        output.write_text(json.dumps(records, ensure_ascii=False, indent=2))
        print(json.dumps(record, ensure_ascii=False), flush=True)

    async def slot():
        nonlocal next_request
        await asyncio.sleep(max(0, next_request - time.monotonic()))
        next_request = time.monotonic() + args.interval

    async def probe(client, item, url, strategy, acquired, **headers):
        await slot()
        record = dict(post_id=item['aweme_id'], strategy=strategy,
                      host=urlsplit(url).hostname,
                      url_hash=hashlib.sha256(url.encode()).hexdigest()[:16],
                      url_age_seconds=round(time.monotonic() - acquired, 2) if acquired else None)
        try:
            async with make_async_client(proxy=client.proxy, follow_redirects=True, timeout=30) as http:
                async with http.stream('GET', url, headers=headers) as response:
                    record.update(status=response.status_code,
                                  content_type=response.headers.get('content-type', ''),
                                  final_host=response.url.host,
                                  response_headers={k: response.headers[k] for k in (
                                      'server', 'date', 'content-length', 'x-cache', 'x-cache-status',
                                      'x-tengine-error', 'x-swift-error', 'x-bdcdn-cache-status',
                                  ) if k in response.headers})
                    prefix = b''
                    async for chunk in response.aiter_bytes(chunk_size=1024):
                        prefix += chunk
                        if len(prefix) >= 1024:
                            break
                    record['prefix_hex'] = prefix[:16].hex()
                    if response.status_code == 403:
                        text = prefix.decode(errors='replace').lower()
                        record['denial_markers'] = [s for s in (
                            'expired', 'signature', 'referer', 'denied', 'forbidden', 'verify'
                        ) if s in text]
        except Exception as exc:
            record['error'] = type(exc).__name__
        save(record)
        return record.get('status') == 200 and not record.get('content_type', '').startswith('text/')

    class ProbeCrawler(DouYinCrawler):
        async def search(self):
            if args.mode == 'historical':
                # Read a previously failed direct CDN URL locally; never print the signature.
                text = Path(args.historical_log).read_text()
                matches = re.findall(r'HTTPStatusError for (https://\S+)', text)
                if not matches:
                    raise RuntimeError('No recorded failed media URL found')
                item = {'aweme_id': args.post_id}
                headers = {'User-Agent': self.dy_client.headers['User-Agent'],
                           'Referer': 'https://www.douyin.com/'}
                await probe(self.dy_client, item, matches[-1], 'historical_ua_referer', None, **headers)
                await slot()
                fresh = await self.dy_client.get_video_by_id(args.post_id)
                acquired = time.monotonic()
                urls = candidates(fresh or {})
                save(dict(event='historical_refresh', post_id=args.post_id, candidate_count=len(urls)))
                for url in urls[:2]:
                    if await probe(self.dy_client, item, url, 'fresh_detail_ua_referer', acquired, **headers):
                        break
                return
            for keyword in args.keyword.split(','):
                await self.probe_keyword(keyword)

        async def probe_keyword(self, keyword):
            client = self.dy_client
            await slot()
            result = await client.search_info_by_keyword(
                keyword=keyword, offset=0, publish_time=PublishTimeType.UNLIMITED, search_id='')
            acquired = time.monotonic()
            items = [(row.get('aweme_info') or {}) for row in result.get('data', []) or []]
            items = [item for item in items if candidates(item) and not item.get('images')][:args.posts]
            save(dict(event='search', keyword=keyword, count=len(items), response_keys=sorted(result),
                      verification_present=any(result.get(k) for k in ('verify', 'captcha', 'verify_info'))))
            ua = client.headers['User-Agent']
            for index, item in enumerate(items):
                if args.mode == 'full':
                    await self.wait_for_content_slot(item['aweme_id'])
                    started = time.monotonic()
                    await self.get_aweme_video(item)
                    path = Path(config.SAVE_DATA_PATH) / 'douyin/videos' / item['aweme_id'] / 'video.mp4'
                    content = path.read_bytes() if path.exists() else b''
                    # Walk MP4 top-level boxes: catches truncation (not a decoder check).
                    offset, boxes = 0, []
                    while offset + 8 <= len(content):
                        length = int.from_bytes(content[offset:offset + 4], 'big')
                        kind = content[offset + 4:offset + 8].decode('ascii', errors='replace')
                        if length == 1:
                            length = int.from_bytes(content[offset + 8:offset + 16], 'big')
                        elif length == 0:
                            length = len(content) - offset
                        if length < 8 or offset + length > len(content):
                            break
                        boxes.append(kind)
                        offset += length
                    save(dict(event='full_download', keyword=keyword, post_id=item['aweme_id'],
                              bytes=len(content), path=str(path),
                              sha256=hashlib.sha256(content).hexdigest(),
                              seconds=round(time.monotonic() - started, 2),
                              mp4_complete=bool(content and offset == len(content)
                                                and {'ftyp', 'moov', 'mdat'} <= set(boxes))))
                    continue
                urls = candidates(item)
                browser_headers = {'User-Agent': ua, 'Referer': 'https://www.douyin.com/'}
                strategies = [('bare', {}), ('ua', {'User-Agent': ua}), ('ua_referer', browser_headers)]
                if index % 2:
                    strategies.reverse()
                outcomes = {}
                for name, headers in strategies:
                    outcomes[name] = await probe(client, item, urls[0], name, acquired, **headers)
                if not outcomes['ua_referer']:
                    recovered = False
                    for url in urls[1:3]:
                        recovered = await probe(client, item, url, 'alternate', acquired, **browser_headers)
                        if recovered:
                            break
                    # A single fresh detail call distinguishes stale search URL from host/header issues.
                    await slot()
                    fresh = await client.get_video_by_id(item['aweme_id'])
                    fresh_time = time.monotonic()
                    fresh_urls = candidates(fresh or {})
                    save(dict(event='refresh', post_id=item['aweme_id'], candidate_count=len(fresh_urls),
                              changed=bool(fresh_urls and fresh_urls[0] != urls[0]), alternate_recovered=recovered))
                    for url in fresh_urls[:2]:
                        if await probe(client, item, url, 'refreshed', fresh_time, **browser_headers):
                            break

    crawler = ProbeCrawler()
    if not crawler.has_account_auth_state():
        raise RuntimeError('Injected auth state is required; probe never opens interactive login')
    # start() owns async_playwright and tears down its browser on exit.
    await crawler.start()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--keyword', default='博彩')
    parser.add_argument('--posts', type=int, choices=range(1, 5), default=3)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--mode', choices=('matrix', 'full', 'historical'), default='matrix')
    parser.add_argument('--historical-log')
    parser.add_argument('--post-id')
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('--interval must be at least one second')
    if args.mode == 'historical' and not (args.historical_log and args.post_id):
        parser.error('historical mode requires --historical-log and --post-id')
    asyncio.run(main(args))
