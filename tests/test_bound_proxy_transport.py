import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from tools.collection_route import CollectionRouteError
from tools.httpx_util import make_async_client
from tools.bound_proxy import BoundProxySession, initialize_bound_route
from tools import bound_proxy as bound_module
from media_platform.douyin import client as client_module
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.exception import PlatformRateLimitedError
from tests.test_proxy_exit_observation import snapshot, session, FakePage


@pytest.mark.asyncio
async def test_http_transport_explicit_proxy_trust_env_and_tls(tmp_path,monkeypatch):
    snap=snapshot(tmp_path)
    monkeypatch.setenv('HTTP_PROXY','http://trap.invalid:8080')
    monkeypatch.setenv('NO_PROXY','*')
    options=[]
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: options.append(kwargs))
    make_async_client(route_snapshot=snap)
    assert options[0]['proxy']==snap.httpx_proxy()
    assert options[0]['trust_env'] is False and options[0]['verify'] is True
    for kwargs in ({'verify':False},{'mounts':{'https://':object()}},{'transport':object()}):
        with pytest.raises(CollectionRouteError):
            make_async_client(route_snapshot=snap,**kwargs)


@pytest.mark.asyncio
async def test_every_redirect_hop_uses_all_scopes_no_proxy_auth_origin(tmp_path,monkeypatch):
    value,context,_=session(tmp_path)
    requests=[]
    options=[]
    def handler(request):
        requests.append(request)
        if request.url.path=='/start':
            return httpx.Response(302,headers={'Location':'https://cdn.invalid/final'})
        return httpx.Response(200,content=b'media')
    def factory(**kwargs):
        options.append(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client_module,'make_async_client',factory)
    try:
        await value.open_context(context)
        client=client_module.DouYinClient(headers={},playwright_page=None,cookie_dict={},bound_proxy_session=value)
        response=await client._send('GET','https://platform.invalid/start',operation='media',follow_redirects=True)
        assert response.content==b'media' and len(requests)==2
        assert options[0]['route_snapshot']==value.snapshot
        for request in requests:
            assert 'Proxy-Authorization' not in request.headers
        from tools.persistent_request_gate import RequestScheduler
        for scope in value._gate.scope_ids:
            scheduler=object.__new__(RequestScheduler)
            scheduler.db_path=value.snapshot.scheduler_db_path
            scheduler.platform=scope
            scheduler._clock=__import__('time').time
            assert scheduler.snapshot()['request_count_last_minute']==2
    finally:
        await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('error,status,code', [(httpx.ProxyError('407 secret-canary'),None,'PROXY_AUTH_FAILED'),(httpx.ConnectError('secret-canary'),None,'PROXY_UNAVAILABLE'),(None,407,'PROXY_AUTH_FAILED')])
async def test_proxy_fault_is_safe_and_no_second_request(tmp_path,monkeypatch,error,status,code):
    value,context,_=session(tmp_path)
    calls=[]
    def handler(request):
        calls.append(request)
        if error:
            raise error
        return httpx.Response(status)
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await value.open_context(context)
        client=client_module.DouYinClient(headers={},playwright_page=None,cookie_dict={},bound_proxy_session=value)
        with pytest.raises(CollectionRouteError) as caught:
            await client._send('GET','https://platform.invalid/start',follow_redirects=True)
        assert caught.value.code==code and 'secret-canary' not in str(caught.value)
        assert len(calls)==1
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_429_cools_all_scopes_without_rotation(tmp_path,monkeypatch):
    value,context,_=session(tmp_path)
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(429,headers={'Retry-After':'9'}))))
    try:
        await value.open_context(context)
        client=client_module.DouYinClient(headers={},playwright_page=None,cookie_dict={},bound_proxy_session=value)
        with pytest.raises(PlatformRateLimitedError):
            await client._send('GET','https://platform.invalid/start')
        import sqlite3
        with sqlite3.connect(value.snapshot.scheduler_db_path) as db:
            assert all(row[0]>__import__('time').time()+7 for row in db.execute('SELECT cooldown_until FROM request_scheduler_state').fetchall())
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_core_author_enrichment_does_not_swallow_route_errors(monkeypatch):
    import config
    monkeypatch.setattr(config,'DY_FETCH_AUTHOR_PROFILE',True)
    crawler=DouYinCrawler()
    crawler.dy_client=type('Client',(),{'get_user_info':AsyncMock(side_effect=CollectionRouteError('PROXY_EXIT_CHANGED'))})()
    with pytest.raises(CollectionRouteError):
        await crawler._enrich_author_profile({'author':{'sec_uid':'user'}})


def test_missing_required_fd_fails_before_network(monkeypatch):
    monkeypatch.setattr(bound_module,'_process_route',bound_module._PROCESS_ROUTE_UNSET)
    with pytest.raises(CollectionRouteError):
        initialize_bound_route({'MEDIACRAWLER_PROXY_REQUIRED':'true','MEDIACRAWLER_ACCOUNT_ID':'acct'})


@pytest.mark.asyncio
async def test_automatic_proxy_refresh_is_disabled_for_bound_client(tmp_path):
    value,_,_=session(tmp_path)
    from proxy.proxy_mixin import ProxyRefreshMixin
    client=type('Client',(ProxyRefreshMixin,),{})()
    client._bound_proxy_session=value
    client._proxy_ip_pool=type('Pool',(),{'is_current_proxy_expired':lambda self:pytest.fail('must not refresh')})()
    await client._refresh_proxy_if_expired()
    await value.close()


@pytest.mark.asyncio
async def test_echo_page_exact_url_and_strips_auth_headers(tmp_path):
    value,context,_=session(tmp_path)
    try:
        await value.open_context(context)
        callback=context.route_handlers[0]
        request=type('Request',(),dict(url=value.echo_url,method='GET',redirected_from=None,headers={'Cookie':'platform-secret','Authorization':'account-secret','Accept':'text/plain'}))()
        intercepted=type('Route',(),{'request':request,'continue_':AsyncMock(),'abort':AsyncMock()})()
        await callback(intercepted)
        assert intercepted.continue_.await_args.kwargs['headers']=={'Accept':'text/plain'}
        request.url='https://other.invalid'
        await callback(intercepted)
        intercepted.abort.assert_awaited_once()
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_store_normalization_preserves_only_validated_public_source(tmp_path,monkeypatch):
    from store import douyin as store
    value,context,_=session(tmp_path)
    sink=AsyncMock()
    monkeypatch.setattr(store.DouyinStoreFactory,'create_store',lambda:type('Store',(),{'store_content':sink})())
    try:
        await value.open_context(context)
        provenance=value.provenance(observation_id='obs1',source_task_id='task1',source_batch_id='batch1')
        item={'aweme_id':'1','collection_route_provenance':provenance}
        await store.update_douyin_aweme(item)
        saved=sink.await_args.kwargs['content_item']
        assert saved['collection_route_provenance']==provenance
        item['collection_route_provenance']={**provenance,'password':'secret-canary'}
        with pytest.raises(CollectionRouteError):
            await store.update_douyin_aweme(item)
        assert sink.await_count==1
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_trial_operation_limit_shared_and_echo_excluded(tmp_path,monkeypatch):
    monkeypatch.setenv('MEDIACRAWLER_PROXY_TRIAL_ID','trial1')
    value,context,_=session(tmp_path)
    try:
        await value.open_context(context)
        assert value.trial_limits.snapshot()['operations']==0
        for _ in range(40):
            async with value.slot():
                pass
        with pytest.raises(CollectionRouteError,match='PROBE_BUDGET_EXHAUSTED'):
            async with value.slot():
                pytest.fail('forty-first business request')
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_trial_media_stream_stops_before_accepting_over_cap(tmp_path,monkeypatch):
    monkeypatch.setenv('MEDIACRAWLER_PROXY_TRIAL_ID','trial1')
    value,context,_=session(tmp_path)
    reads=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for number in range(400):
                reads.append(number)
                yield b'a'*65536
        async def aclose(self):
            pass
    monkeypatch.setattr(client_module,'make_async_client',lambda **kw:httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={'content-type':'video/mp4'},stream=Stream()))))
    try:
        await value.open_context(context)
        client=client_module.DouYinClient(headers={},playwright_page=None,cookie_dict={},bound_proxy_session=value)
        with pytest.raises(CollectionRouteError,match='PROBE_BUDGET_EXHAUSTED'):
            await client.get_aweme_media('https://cdn.invalid/video',raise_on_error=True)
        assert len(reads)==321  # twenty MiB accepted, one buffered reject chunk.
        state=value.trial_limits.snapshot()
        assert state['media_bytes']==20*1024*1024
    finally:
        await value.close()


def test_real_legacy_provider_model_default_protocol_still_formats_http():
    from proxy.types import IpInfoModel
    from tools.crawler_util import format_proxy_info
    legacy=IpInfoModel(ip='proxy.invalid',port=8080,user='u#/?@:',password='密#/?@:')
    assert legacy.protocol=='https://'  # Actual provider default, not a fixture override.
    browser,http_proxy=format_proxy_info(legacy)
    assert browser=={'server':'http://proxy.invalid:8080','username':'u#/?@:','password':'密#/?@:'}
    assert http_proxy=='http://u%23%2F%3F%40%3A:%E5%AF%86%23%2F%3F%40%3A@proxy.invalid:8080'
    from tools.collection_route import validate_route_envelope
    from tests.test_collection_route_contract import envelope
    value=envelope(endpoint={'scheme':'https','host':'proxy.invalid','port':8080})
    with pytest.raises(CollectionRouteError):
        validate_route_envelope(value,account_id='acct-1')
