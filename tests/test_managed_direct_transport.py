"""Managed routes use explicit transports; fixtures never contact platforms."""
from dataclasses import replace
import http.server
import json
import threading
import time

import pytest

from tools import collection_route as route
from tools.httpx_util import make_async_client
from tests.test_proxy_exit_observation import snapshot


def direct_snapshot(tmp_path):
    base=snapshot(tmp_path)
    ip='198.51.100.9'
    value=replace(base,route_mode='direct_degraded',host='direct.invalid',port=1,username='',password='',
        observed_exit_ip=ip, observed_exit_at=time.time(),expires_at=time.time()+120,
        operation_id='operation',lease_id='lease',lease_version=1,owner_id='app',exit_generation=1,context_id='context',
        scope_ids=tuple(sorted(['dy','dy:account:'+base.account_id,'dy:service_exit:'+ip,'dy:exit:'+ip])))
    return replace(value,snapshot_digest=route.snapshot_digest(value))


@pytest.mark.asyncio
async def test_direct_request_ignores_environment_proxy_and_has_no_auth(tmp_path,monkeypatch):
    requests=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(dict(self.headers)); self.send_response(200); self.end_headers(); self.wfile.write(b'fixture')
        def log_message(self,*args): pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'):
        monkeypatch.setenv(name,'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY','')
    snap=direct_snapshot(tmp_path)
    try:
        assert snap.httpx_proxy() is None and snap.playwright_proxy() is None
        decoded=route.validate_route_envelope(json.loads(route.serialize_route(snap)),account_id=snap.account_id)
        assert decoded==snap
        async with make_async_client(route_snapshot=snap) as client:
            response=await client.get(f'http://127.0.0.1:{server.server_port}/media')
        assert response.content==b'fixture'
        assert len(requests)==1 and not any(k.lower()=='proxy-authorization' for k in requests[0])
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


@pytest.mark.parametrize('mode',['proxy_preferred','direct_degraded'])
def test_managed_modes_cannot_use_legacy_envelope(tmp_path,mode):
    value=json.loads(route.serialize_route(snapshot(tmp_path)))
    value['route_mode']=mode
    with pytest.raises(route.CollectionRouteError): route.validate_route_envelope(value,account_id=value['account_id'])


@pytest.mark.parametrize('field,value',[('ttl',float('nan')),('ttl',float('inf')),('expires_at',float('inf')),('lease_version',True)])
def test_ack_rejects_nonfinite_and_bool_fields(tmp_path,field,value):
    snap=direct_snapshot(tmp_path)
    ack=route.route_ack(snap,owner_id=snap.owner_id,lease_version=1,ttl=60)
    ack[field]=value
    with pytest.raises(route.CollectionRouteError): snap.require_managed_ack(ack)


def test_direct_service_budget_preserves_existing_platform_policy_and_429(tmp_path):
    import sqlite3
    from tools.bound_proxy import BoundProxySession
    from tools.persistent_request_gate import RequestScheduler
    snap=direct_snapshot(tmp_path)
    platform=RequestScheduler(snap.scheduler_db_path,platform='dy',min_interval=2,per_minute=30,max_concurrency=1,media_interval=5)
    platform.enter_cooldown('http_429',30)
    ack=route.route_ack(snap,owner_id=snap.owner_id,lease_version=1,ttl=60)
    value=BoundProxySession(snap,ack=ack,echo_url='https://echo.invalid/ip',budget_id='direct-policy',
        policy=dict(min_interval=2,per_minute=30,max_concurrency=1,media_interval=5,cooldown_seconds=30))
    value.receipt=route.ProbeReceipt(snap.proxy_id,snap.config_revision,snap.context_id,snap.observed_exit_ip,time.time(),snap.exit_generation)
    value.context_id=snap.context_id
    value._make_gate()
    with sqlite3.connect(snap.scheduler_db_path) as db:
        policies={key:json.loads(body) for key,body in db.execute('SELECT platform,limits_json FROM request_scheduler_policy')}
    assert policies['dy'][:2]==[2.0,30]
    assert policies['dy:service_exit:'+snap.observed_exit_ip][:2]==[9.0,6]
    assert platform.snapshot()['cooldown_until']>time.time()+20
