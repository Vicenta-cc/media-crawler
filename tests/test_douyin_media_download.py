import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

import config
from media_platform.douyin import client as client_module
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import DataFetchError, MediaDownloadError
from store import douyin as store


def item(*urls):
    return {'aweme_id': '123', 'video': {'play_addr': {'url_list': list(urls)}}}


def make_client():
    return client_module.DouYinClient(headers={
        'User-Agent': 'browser-test', 'Cookie': 'secret=abc',
        'Host': 'www.douyin.com', 'Authorization': 'private',
    }, playwright_page=None, cookie_dict={})


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(config, 'DY_MEDIA_REQUEST_INTERVAL', 0)
    monkeypatch.setattr(config, 'DY_MEDIA_MAX_URL_ATTEMPTS', 2)
    monkeypatch.setattr(config, 'DY_MEDIA_REFRESH_ON_FAILURE', True)
    monkeypatch.setattr(config, 'ENABLE_GET_MEIDAS', True)


def test_single_url_and_ordered_distinct_alternates():
    assert store._extract_video_download_url(item('https://cdn/only')) == 'https://cdn/only'
    value = item('https://cdn/backup', 'https://cdn/primary', 'https://cdn/primary')
    assert store._extract_video_download_urls(value) == ['https://cdn/primary', 'https://cdn/backup']


@pytest.mark.asyncio
async def test_headers_survive_redirect_without_leaking_api_credentials(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == 'www.douyin.com':
            return httpx.Response(302, headers={'Location': 'https://cdn.example/video'})
        return httpx.Response(200, content=b'video', headers={'Content-Type': 'video/mp4'})
    monkeypatch.setattr(client_module, 'make_async_client', lambda **kw:
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await make_client().get_aweme_media('https://www.douyin.com/play') == b'video'
    assert len(requests) == 2
    for request in requests:
        assert request.headers['User-Agent'] == 'browser-test'
        assert request.headers['Referer'] == 'https://www.douyin.com/'
        assert 'Cookie' not in request.headers and 'Authorization' not in request.headers
    assert requests[1].headers['Host'] == 'cdn.example'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,mime,body', [(403, 'text/html', b'denied'),
    (200, 'text/html', b'challenge'), (200, 'video/mp4', b'')])
async def test_failure_and_fake_success_are_not_saved(monkeypatch, status, mime, body):
    monkeypatch.setattr(client_module, 'make_async_client', lambda **kw:
        httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            httpx.Response(status, headers={'Content-Type': mime}, content=body))))
    client = make_client()
    assert await client.get_aweme_media('https://cdn.example/video') is None
    with pytest.raises(MediaDownloadError):
        await client.get_aweme_media('https://cdn.example/video', raise_on_error=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcomes,refreshed,downloads,saved', [
    ([MediaDownloadError('403', 403), b'backup'], False, 2, True),
    ([MediaDownloadError('403', 403)] * 2 + [b'fresh'], True, 3, True),
    ([MediaDownloadError('403', 403)] * 4, True, 4, False),
    ([MediaDownloadError('429', 429)], False, 1, False),
    ([MediaDownloadError('network')] * 2, False, 2, False),
])
async def test_bounded_candidates_refresh_and_rate_limit_stop(
        monkeypatch, outcomes, refreshed, downloads, saved):
    crawler = DouYinCrawler()
    crawler.dy_client = make_client()
    crawler.dy_client.get_aweme_media = AsyncMock(side_effect=outcomes)
    crawler.dy_client.get_video_by_id = AsyncMock(return_value=item('https://cdn/fresh2', 'https://cdn/fresh1'))
    crawler.dy_client.wait_for_media_slot = AsyncMock()
    sink = AsyncMock()
    monkeypatch.setattr(store, 'update_dy_aweme_video', sink)
    await crawler.get_aweme_video(item('https://cdn/backup', 'https://cdn/primary'))
    assert crawler.dy_client.get_aweme_media.await_count == downloads
    assert crawler.dy_client.get_video_by_id.await_count == int(refreshed)
    assert crawler.dy_client.wait_for_media_slot.await_count == int(refreshed)
    assert sink.await_count == int(saved)


@pytest.mark.asyncio
async def test_cancellation_propagates_without_refresh_or_save(monkeypatch):
    crawler = DouYinCrawler()
    crawler.dy_client = make_client()
    crawler.dy_client.get_aweme_media = AsyncMock(side_effect=asyncio.CancelledError)
    crawler.dy_client.get_video_by_id = AsyncMock()
    sink = AsyncMock()
    monkeypatch.setattr(store, 'update_dy_aweme_video', sink)
    with pytest.raises(asyncio.CancelledError):
        await crawler.get_aweme_video(item('https://cdn/primary'))
    sink.assert_not_called()
    crawler.dy_client.get_video_by_id.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('refresh_enabled', [True, False])
@pytest.mark.parametrize('error', [DataFetchError('API unavailable'), httpx.ReadTimeout('timeout')])
async def test_refresh_failure_or_opt_out_never_loops(monkeypatch, refresh_enabled, error):
    monkeypatch.setattr(config, 'DY_MEDIA_REFRESH_ON_FAILURE', refresh_enabled)
    crawler = DouYinCrawler()
    crawler.dy_client = make_client()
    crawler.dy_client.get_aweme_media = AsyncMock(side_effect=MediaDownloadError('403', 403))
    crawler.dy_client.get_video_by_id = AsyncMock(side_effect=error)
    sink = AsyncMock()
    monkeypatch.setattr(store, 'update_dy_aweme_video', sink)
    await crawler.get_aweme_video(item('https://cdn/primary'))
    assert crawler.dy_client.get_aweme_media.await_count == 1
    assert crawler.dy_client.get_video_by_id.await_count == int(refresh_enabled)
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_media_and_refresh_slots_share_spacing(monkeypatch):
    # Real concurrency with a tiny test interval: no burst when callers queue together.
    monkeypatch.setattr(config, 'DY_MEDIA_REQUEST_INTERVAL', 0.02)
    client = make_client()
    loop = asyncio.get_running_loop()
    async def reserve():
        await client.wait_for_media_slot()
        return loop.time()
    starts = sorted(await asyncio.gather(*(reserve() for _ in range(3))))
    assert all(b - a >= 0.018 for a, b in zip(starts, starts[1:]))
