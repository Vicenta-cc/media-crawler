import asyncio
import time

import httpx
import pytest
import config
from media_platform.douyin import client as module
from media_platform.douyin.exception import PlatformRateLimitedError


def client():
    return module.DouYinClient(headers={},playwright_page=None,cookie_dict={})


@pytest.mark.asyncio
async def test_media_wait_does_not_spend_global_slot_early(monkeypatch):
    monkeypatch.setattr(config,'DY_REQUEST_MIN_INTERVAL',.08)
    monkeypatch.setattr(config,'DY_MEDIA_REQUEST_INTERVAL',.3)
    monkeypatch.setattr(config,'DY_REQUEST_CONCURRENCY',2)
    starts=[]
    def handler(req):
        starts.append((req.url.path,time.monotonic()))
        return httpx.Response(200,content=b'fixture',headers={'content-type':'image/jpeg'})
    monkeypatch.setattr(module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c=client()
    await c.get_aweme_media('https://fixture.invalid/media1')
    media=asyncio.create_task(c.get_aweme_media('https://fixture.invalid/media2'))
    await asyncio.sleep(.26)
    await c._send('GET','https://fixture.invalid/api')
    await media
    assert all(b[1]-a[1]>=.065 for a,b in zip(starts,starts[1:]))
    assert c._persistent_gate.state.snapshot()['request_count_last_minute']==3


@pytest.mark.asyncio
async def test_each_redirect_and_short_url_share_the_same_budget(monkeypatch):
    def handler(req):
        if req.url.path in ('/media','/short'):
            return httpx.Response(302,headers={'Location':'https://cdn.invalid/final'})
        return httpx.Response(200,content=b'fixture',headers={'content-type':'image/jpeg'})
    monkeypatch.setattr(module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c=client()
    assert await c.get_aweme_media('https://fixture.invalid/media')==b'fixture'
    assert await c.resolve_short_url('https://fixture.invalid/short')=='https://cdn.invalid/final'
    assert c._persistent_gate.state.snapshot()['request_count_last_minute']==3


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['api','media','short_url'])
async def test_429_records_shared_cooldown_without_changing_account(monkeypatch,operation):
    requests=[]
    def handler(req):
        requests.append(req)
        return httpx.Response(429,headers={'Retry-After':'360'})
    monkeypatch.setattr(module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c=client();start=time.time()
    call={'api':c.request('GET','https://fixture.invalid/api')} if operation=='api' else {}
    with pytest.raises(PlatformRateLimitedError):
        if operation=='api':await call['api']
        elif operation=='media':await c.get_aweme_media('https://fixture.invalid/media')
        else:await c.resolve_short_url('https://fixture.invalid/short')
    other=client();snapshot=other._persistent_gate.state.snapshot()
    assert snapshot['cooldown_until']>=start+359
    assert snapshot['in_flight']==0
    with pytest.raises(RuntimeError,match='cooldown'):
        async with other._persistent_gate.slot(max_wait=.01):pytest.fail('must not start a new request')
    assert len(requests)==1


@pytest.mark.asyncio
async def test_network_failure_and_cancellation_release_capacity(monkeypatch):
    started=asyncio.Event()
    async def handler(req):
        started.set()
        await asyncio.sleep(20)
    monkeypatch.setattr(module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c=client();task=asyncio.create_task(c._send('GET','https://fixture.invalid/api'))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert c._persistent_gate.state.snapshot()['in_flight']==0
    def failing(req):raise httpx.ConnectError('fixture failure')
    monkeypatch.setattr(module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(failing)))
    with pytest.raises(httpx.ConnectError):await c._send('GET','https://fixture.invalid/api')
    assert c._persistent_gate.state.snapshot()['in_flight']==0
