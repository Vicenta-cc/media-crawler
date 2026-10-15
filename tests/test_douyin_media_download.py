import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

import config
from media_platform.douyin import client as client_module
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import DataFetchError, MediaDownloadError, PlatformRateLimitedError
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
    ([MediaDownloadError('network')] * 2, False, 2, False),
])
async def test_bounded_candidates_refresh_and_rate_limit_stop(
        monkeypatch, outcomes, refreshed, downloads, saved):
    crawler = DouYinCrawler()
    crawler.dy_client = make_client()
    crawler.dy_client.get_aweme_media = AsyncMock(side_effect=outcomes)
    crawler.dy_client.get_video_by_id = AsyncMock(return_value=item('https://cdn/fresh2', 'https://cdn/fresh1'))
    sink = AsyncMock()
    monkeypatch.setattr(store, 'update_dy_aweme_video', sink)
    if saved:
        await crawler.get_aweme_video(item('https://cdn/backup', 'https://cdn/primary'))
    else:
        with pytest.raises(MediaDownloadError, match='media_download_failed'):
            await crawler.get_aweme_video(item('https://cdn/backup', 'https://cdn/primary'))
    assert crawler.dy_client.get_aweme_media.await_count == downloads
    assert crawler.dy_client.get_video_by_id.await_count == int(refreshed)
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
    with pytest.raises(MediaDownloadError, match='media_download_failed'):
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
        async with client._persistent_gate.slot('media'):
            return loop.time()
    starts = sorted(await asyncio.gather(*(reserve() for _ in range(3))))
    assert all(b - a >= 0.018 for a, b in zip(starts, starts[1:]))


@pytest.mark.asyncio
async def test_media_hop_total_timeout_closes_body_releases_slot_and_tries_backup(monkeypatch):
    from contextlib import asynccontextmanager
    closed=[];released=[];calls=[]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            await asyncio.sleep(1)
        async def aclose(self):closed.append(True)
    @asynccontextmanager
    async def slot(operation,**kwargs):
        try:
            async with asyncio.timeout(.02 if kwargs['timeout'] is None else kwargs['timeout']):yield 0
        finally:released.append(operation)
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200,headers={'Content-Type':'video/mp4'},
            stream=Body()) if request.url.host=='slow.example' else httpx.Response(200,content=b'backup-video',headers={'Content-Type':'video/mp4'})
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client=make_client();client.timeout=.02
    client._persistent_gate=type('Gate',(),{'slot':staticmethod(slot)})()
    crawler=DouYinCrawler();crawler.dy_client=client
    saved=AsyncMock();monkeypatch.setattr(store,'update_dy_aweme_video',saved)
    await crawler.get_aweme_video(item('https://backup.example/video','https://slow.example/video'))
    assert calls==['slow.example','backup.example']
    assert closed==[True] and released==['media','media']
    assert saved.await_args.args[1]==b'backup-video'


@pytest.mark.asyncio
@pytest.mark.parametrize('outer_expires',[False,True])
async def test_trial_timeout_only_spends_budget_when_its_own_deadline_expires(outer_expires):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from tools.collection_route import CollectionRouteError
    client=make_client();client.timeout=.01
    trial=SimpleNamespace(begin_media=Mock(return_value='token'),media_timeout=Mock(return_value=.01 if outer_expires else 1),finish_media=Mock())
    bound=SimpleNamespace(trial_limits=trial,authority=None,fail=Mock(side_effect=lambda code:CollectionRouteError(code)))
    client._bound_proxy_session=bound
    async def send(*args,**kwargs):
        if outer_expires:await asyncio.sleep(1)
        raise MediaDownloadError('media_total_timeout')
    client._send=send
    expected=CollectionRouteError if outer_expires else MediaDownloadError
    with pytest.raises(expected,match='PROBE_BUDGET_EXHAUSTED' if outer_expires else 'media_total_timeout'):
        await client.get_aweme_media('https://media.example/video',raise_on_error=True)
    assert bound.fail.call_count==int(outer_expires)
    trial.finish_media.assert_called_once_with('token')


@pytest.mark.asyncio
@pytest.mark.parametrize('error',[asyncio.CancelledError(),PlatformRateLimitedError('PLATFORM_RATE_LIMITED'),RuntimeError('PROBE_BUDGET_EXHAUSTED'),DataFetchError('ACCOUNT_VERIFY')])
async def test_media_safety_and_external_cancel_never_fall_back(monkeypatch,error):
    crawler=DouYinCrawler();crawler.dy_client=make_client()
    crawler.dy_client.get_aweme_media=AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await crawler.get_aweme_video(item('https://backup.example/video','https://first.example/video'))
    assert crawler.dy_client.get_aweme_media.await_count==1


@pytest.mark.asyncio
async def test_media_route_loss_is_not_converted_to_candidate_failure():
    from tools.collection_route import CollectionRouteError
    client=make_client()
    client._send=AsyncMock(side_effect=CollectionRouteError('PROXY_REVISION_MISMATCH'))
    with pytest.raises(CollectionRouteError,match='PROXY_REVISION_MISMATCH'):
        await client.get_aweme_media('https://cdn.example/video',raise_on_error=True)
    client._send=AsyncMock(side_effect=DataFetchError('ACCOUNT_VERIFY'))
    with pytest.raises(DataFetchError,match='ACCOUNT_VERIFY'):
        await client.get_aweme_media('https://cdn.example/video',raise_on_error=True)


@pytest.mark.asyncio
async def test_continuous_media_chunks_outlive_old_timeout_without_extending_httpx_idle(monkeypatch):
    from contextlib import asynccontextmanager
    closed=[];released=[];http_timeouts=[];gate_timeouts=[]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(5):
                await asyncio.sleep(.015)
                yield b'chunk'
        async def aclose(self):closed.append(True)
    @asynccontextmanager
    async def slot(operation,**kwargs):
        gate_timeouts.append(kwargs['timeout'])
        try:
            async with asyncio.timeout(kwargs['timeout']):yield 0
        finally:released.append(True)
    def handler(request):
        http_timeouts.append(request.extensions['timeout'])
        return httpx.Response(200,headers={'Content-Type':'video/mp4'},stream=Body())
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client=make_client();client.timeout=.02
    client._persistent_gate=type('Gate',(),{'slot':staticmethod(slot)})()
    assert await client.get_aweme_media('https://media.example/video',raise_on_error=True)==b'chunk'*5
    assert gate_timeouts==[None] and closed==[True] and released==[True]
    assert http_timeouts==[dict(connect=.02,read=.02,write=.02,pool=.02)]


@pytest.mark.asyncio
async def test_httpx_idle_timeout_still_closes_media_and_releases_slot(monkeypatch):
    from contextlib import asynccontextmanager
    closed=[];released=[]
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            raise httpx.ReadTimeout('fixture read inactivity')
        async def aclose(self):closed.append(True)
    @asynccontextmanager
    async def slot(*args,**kwargs):
        try:yield 0
        finally:released.append(True)
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,headers={'Content-Type':'video/mp4'},stream=Body()))))
    client=make_client();client._persistent_gate=type('Gate',(),{'slot':staticmethod(slot)})()
    with pytest.raises(MediaDownloadError,match='ReadTimeout'):
        await client.get_aweme_media('https://media.example/video',raise_on_error=True)
    assert closed==[True] and released==[True]


@pytest.mark.asyncio
async def test_trial_remaining_limits_total_media_budget_without_raising_idle_timeout():
    from types import SimpleNamespace
    from unittest.mock import Mock
    client=make_client();client.timeout=60
    trial=SimpleNamespace(begin_media=Mock(return_value='token'),media_timeout=Mock(return_value=4),finish_media=Mock())
    client._bound_proxy_session=SimpleNamespace(trial_limits=trial,authority=None)
    client._send=AsyncMock(return_value=httpx.Response(200,content=b'video',headers={'Content-Type':'video/mp4'},request=httpx.Request('GET','https://media.example/video')))
    assert await client.get_aweme_media('https://media.example/video',raise_on_error=True)==b'video'
    assert 'media_total_timeout' not in client._send.await_args.kwargs
    assert client._send.await_args.kwargs['timeout']==4
    trial.finish_media.assert_called_once_with('token')


@pytest.mark.asyncio
async def test_external_cancellation_closes_body_and_releases_media_slot(monkeypatch):
    from contextlib import asynccontextmanager
    closed=[];released=[];reading=asyncio.Event()
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.sleep(10)
            yield b'never'
        async def aclose(self):closed.append(True)
    @asynccontextmanager
    async def slot(*args,**kwargs):
        try:
            async with asyncio.timeout(kwargs['timeout']):yield 0
        finally:released.append(True)
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,headers={'Content-Type':'video/mp4'},stream=Body()))))
    client=make_client();client._persistent_gate=type('Gate',(),{'slot':staticmethod(slot)})()
    task=asyncio.create_task(client.get_aweme_media('https://media.example/video',raise_on_error=True))
    await reading.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert closed==[True] and released==[True]


@pytest.mark.asyncio
async def test_api_budget_is_unchanged_by_media_setting(monkeypatch):
    from contextlib import asynccontextmanager
    gate_timeouts=[]
    @asynccontextmanager
    async def slot(operation,**kwargs):
        gate_timeouts.append((operation,kwargs['timeout']))
        yield 0
    monkeypatch.setenv('MEDIACRAWLER_DY_MEDIA_TOTAL_TIMEOUT_SECONDS','1')
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request:httpx.Response(200,json={'ok':True}))))
    client=make_client();client._persistent_gate=type('Gate',(),{'slot':staticmethod(slot)})()
    await client._send('GET','https://api.example/item',operation='api',timeout=60)
    assert gate_timeouts==[('api',60)]
