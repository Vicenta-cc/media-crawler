from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import httpx
import pytest

import config
from media_platform.douyin import core
from media_platform.douyin.exception import MediaDownloadError, PlatformRateLimitedError
from tests.test_douyin_media_download import make_client, item


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_GET_MEIDAS', True)
    monkeypatch.setattr(config, 'DY_MEDIA_MAX_URL_ATTEMPTS', 2)
    monkeypatch.setattr(config, 'DY_MEDIA_REFRESH_ON_FAILURE', True)
    sleep, sink = AsyncMock(), AsyncMock()
    monkeypatch.setattr(core.asyncio, 'sleep', sleep)
    monkeypatch.setattr(core.douyin_store, 'update_dy_aweme_video', sink)
    crawler = core.DouYinCrawler()
    crawler.dy_client = SimpleNamespace(get_aweme_media=AsyncMock(), get_video_by_id=AsyncMock())
    return crawler, sleep, sink


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [httpx.RemoteProtocolError('disconnect'), httpx.ReadTimeout('timeout'), httpx.ConnectError('connect')])
async def test_real_client_classifies_and_retries_transient_error(setup, error):
    crawler, sleep, sink = setup
    client = make_client()
    client._send = AsyncMock(side_effect=[error, httpx.Response(200, content=b'video', headers={'content-type': 'video/mp4'}, request=httpx.Request('GET', 'https://cdn.test/video'))])
    crawler.dy_client = client
    await crawler.get_aweme_video(item('https://cdn.test/video'))
    assert client._send.await_count == 2
    sleep.assert_awaited_once_with(1.0)
    sink.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistent_network_error_tries_each_url_twice_then_stops(setup):
    crawler, sleep, sink = setup
    crawler.dy_client.get_aweme_media.side_effect = MediaDownloadError('RemoteProtocolError', retryable=True)
    with pytest.raises(MediaDownloadError, match='media_download_failed'):
        await crawler.get_aweme_video(item('https://cdn/backup', 'https://cdn/main'))
    assert crawler.dy_client.get_aweme_media.await_count == 4
    assert sleep.await_count == 2
    crawler.dy_client.get_video_by_id.assert_not_called()
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_and_retries_share_budget(setup):
    crawler, sleep, sink = setup
    temporary = MediaDownloadError('timeout', retryable=True)
    crawler.dy_client.get_aweme_media.side_effect = [temporary, MediaDownloadError('403', 403), MediaDownloadError('410', 410), temporary, b'should-not-run']
    crawler.dy_client.get_video_by_id.return_value = item('https://cdn/fresh')
    with pytest.raises(MediaDownloadError):
        await crawler.get_aweme_video(item('https://cdn/backup', 'https://cdn/main'))
    assert crawler.dy_client.get_aweme_media.await_count + crawler.dy_client.get_video_by_id.await_count == 5
    sink.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [PlatformRateLimitedError('429'), asyncio.CancelledError(), ValueError('programming error')])
async def test_limits_cancellation_and_programming_errors_never_retry(setup, error):
    crawler, sleep, sink = setup
    crawler.dy_client.get_aweme_media.side_effect = error
    with pytest.raises(type(error)):
        await crawler.get_aweme_video(item('https://cdn/main'))
    assert crawler.dy_client.get_aweme_media.await_count == 1
    sleep.assert_not_called()
    sink.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,retryable', [(403,False),(404,False),(410,False),(500,True),(502,True),(503,True),(504,True)])
async def test_http_status_retry_classification(status,retryable):
    client = make_client()
    client._send = AsyncMock(return_value=httpx.Response(status, request=httpx.Request('GET', 'https://cdn.test/video')))
    with pytest.raises(MediaDownloadError) as exc:
        await client.get_aweme_media('https://cdn.test/video', raise_on_error=True)
    assert exc.value.retryable is retryable
