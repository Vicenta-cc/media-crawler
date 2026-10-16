"""Actual HTTPX/requests proxy I/O using the paired app's shared fixture."""
import os
from pathlib import Path
import sys
import time

import httpx
import pytest
import requests

_app = Path(os.getenv('ZHANYUN_PROXY_FIXTURE_APP', Path(__file__).resolve().parents[2]/'wukong-proxy-app'))
sys.path.insert(0,str(_app/'tests'))
from proxy_fixture import local_proxy
from tools.collection_route import BoundRoute, derive_profile_key, derive_scope_ids, CollectionRouteError
from tools.httpx_util import make_async_client


def route(fixture,tmp_path):
    return BoundRoute('dy','account-a',derive_profile_key('account-a'),'fixture-proxy',1,1,'127.0.0.1',fixture.proxy.server_port,fixture.username,fixture.password,time.time()+120,None,None,str(tmp_path/'gate.sqlite3'),derive_scope_ids('dy','account-a','fixture-proxy'))


@pytest.mark.asyncio
async def test_httpx_bound_route_forward_connect_and_all_redirect_hops(tmp_path,monkeypatch):
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):
        monkeypatch.setenv(key,'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY','*')
    with local_proxy(tmp_path/'network') as fixture:
        async with make_async_client(route_snapshot=route(fixture,tmp_path),verify=fixture.ca,follow_redirects=True,timeout=5) as client:
            for tls in (False,True):
                response=await client.get(fixture.url('/redirect',https=tls))
                assert response.status_code==200 and len(response.history)==1
        fixture.assert_routed()
        assert {'forward','CONNECT'} <= {e['kind'] for e in fixture.events}
        assert [o['path'] for o in fixture.origins].count('/redirect')==2
        assert [o['path'] for o in fixture.origins].count('/final')==2


def test_requests_explicit_ca_forward_connect_and_environment_isolation(tmp_path,monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY','*')
    with local_proxy(tmp_path/'network') as fixture:
        with requests.Session() as client:
            client.trust_env=False
            client.proxies={'http':fixture.authenticated_url,'https':fixture.authenticated_url}
            for tls in (False,True):
                assert client.get(fixture.url('/redirect',https=tls),verify=fixture.ca,timeout=5).status_code==200
        fixture.assert_routed()
        assert {'forward','CONNECT'} <= {e['kind'] for e in fixture.events}


@pytest.mark.asyncio
@pytest.mark.parametrize('tls',[False,True])
async def test_httpx_407_reaches_no_origin(tmp_path,tls):
    with local_proxy(tmp_path/'network') as fixture:
        fixture.reject=True
        async with make_async_client(route_snapshot=route(fixture,tmp_path),verify=fixture.ca,timeout=5) as client:
            if tls:
                with pytest.raises(httpx.ProxyError):await client.get(fixture.url(https=tls))
            else:
                assert (await client.get(fixture.url(https=tls))).status_code==407
        assert not fixture.origins and not fixture.traps


@pytest.mark.asyncio
async def test_bound_transport_rejects_mounts_and_tls_bypass_before_network(tmp_path):
    with local_proxy(tmp_path/'network') as fixture:
        for kwargs in ({'mounts':{'https://':httpx.AsyncHTTPTransport()}},{'verify':False}):
            with pytest.raises(CollectionRouteError):make_async_client(route_snapshot=route(fixture,tmp_path),**kwargs)
        assert not fixture.events and not fixture.origins


@pytest.mark.asyncio
async def test_httpx_unicode_special_proxy_password_survives_encoding(tmp_path):
    with local_proxy(tmp_path/'network') as fixture:
        fixture.password += '中文'
        async with make_async_client(route_snapshot=route(fixture,tmp_path),verify=fixture.ca,timeout=5) as client:
            assert (await client.get(fixture.url())).status_code==200
        fixture.assert_routed()


@pytest.mark.asyncio
async def test_httpx_disconnect_does_not_retry_direct(tmp_path):
    with local_proxy(tmp_path/'network') as fixture:
        fixture.disconnect=True
        async with make_async_client(route_snapshot=route(fixture,tmp_path),verify=fixture.ca,timeout=5) as client:
            with pytest.raises(httpx.HTTPError):await client.get(fixture.url())
        assert not fixture.origins and not fixture.traps
