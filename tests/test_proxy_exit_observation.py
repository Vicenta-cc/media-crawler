import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tools.bound_proxy import BoundProxySession, SharedProbeBudget, parse_exit_sample, fixed_echo_url
from tools.collection_route import CollectionRouteError, derive_profile_key, derive_scope_ids, validate_route_envelope


def snapshot(tmp_path, proxy_id='proxy1', exit_ip=None):
    return validate_route_envelope(dict(schema_version=1, route_mode='proxy_required', platform='dy',
        account_id='account1', profile_key=derive_profile_key('account1'), proxy_id=proxy_id,
        binding_revision=1, config_revision=1, endpoint=dict(scheme='http', host='proxy.invalid', port=8080),
        credentials=dict(username='u#/?@:', password='密#/?@:'), expires_at=time.time()+3600,
        observed_exit=None if exit_ip is None else dict(ip=exit_ip, observed_at=time.time()),
        scheduler=dict(db_path=str(tmp_path/'gate.sqlite'), scope_ids=list(derive_scope_ids('dy','account1',proxy_id,exit_ip)))), account_id='account1')


class FakeResponse:
    status = 200
    def __init__(self, ip):
        self.ip = ip
    async def body(self):
        return json.dumps({'ip':self.ip}).encode()


class FakePage:
    def __init__(self, context):
        self.context = context
        self.closed = False
    async def route(self, pattern, callback):
        self.context.route_handlers.append(callback)
    async def goto(self, url, **kwargs):
        self.context.urls.append(url)
        return FakeResponse(self.context.ip)
    async def evaluate(self, script, url):
        assert "credentials: 'omit'" in script and "redirect: 'error'" in script
        self.context.urls.append(url)
        return {'status': 200, 'body': json.dumps({'ip': self.context.ip})}
    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, ip='203.0.113.1'):
        self.ip, self.pages, self.urls, self.route_handlers = ip, [], [], []
        self.closed = False
    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page
    async def close(self):
        self.closed = True


def session(tmp_path, *, browser_ip='203.0.113.1', http_ip='203.0.113.1', budget_id='trial1', proxy_id='proxy1', **kwargs):
    options=[]
    def client_factory(**options_value):
        options.append(options_value)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={'ip':http_ip})))
    value = BoundProxySession(snapshot(tmp_path,proxy_id), echo_url='https://echo.invalid/ip',
        budget_id=budget_id, http_client_factory=client_factory,
        policy=dict(min_interval=0,per_minute=100,max_concurrency=1,media_interval=0,cooldown_seconds=1), **kwargs)
    return value, FakeContext(browser_ip), options


@pytest.mark.parametrize('value,expected', [('203.0.113.1','203.0.113.1'), (' {"ip":"2001:0db8::1"} ', '2001:db8::1')])
def test_exit_parser(value,expected):
    assert parse_exit_sample(value)==expected


@pytest.mark.parametrize('value', ['{"ip":"203.0.113.1","extra":1}', '{"ip":"203.0.113.1","ip":"203.0.113.2"}', '<html>203.0.113.1</html>', 'bad', '127.0.0.1 garbage', 'fe80::1%eth0'])
def test_exit_parser_rejects_ambiguous(value):
    with pytest.raises(CollectionRouteError, match='PROXY_UNAVAILABLE'):
        parse_exit_sample(value)


@pytest.mark.parametrize('url', ['http://echo.invalid', 'https://user:pass@echo.invalid', 'https://echo.invalid/?q=x','https://echo.invalid/#x',''])
def test_echo_is_explicit_https_without_credentials(url):
    with pytest.raises(CollectionRouteError):
        fixed_echo_url(url)


@pytest.mark.asyncio
async def test_unknown_exit_bootstrap_initial_two_pairs_context_scope_and_cleanup(tmp_path):
    value, context, options = session(tmp_path)
    with pytest.raises(CollectionRouteError):
        await value.ensure_business()
    try:
        receipt = await value.open_context(context)
        assert receipt.ip=='203.0.113.1'
        assert value.budget.snapshot()['used']==4
        assert len(context.pages)==2 and all(page.closed for page in context.pages)
        assert all(getattr(page,'_collection_proxy_probe',False) for page in context.pages)
        assert 'dy:exit:203.0.113.1' in (await value.ensure_business()).scope_ids
        assert all(option['trust_env'] is False and option['follow_redirects'] is False and option['verify'] is True for option in options)
        assert all('%E5%AF%86%23%2F%3F%40%3A' in option['proxy'] for option in options)
        async with value.slot():
            pass
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_mismatched_and_changed_samples_stop_new_business(tmp_path):
    value, context, _ = session(tmp_path,http_ip='203.0.113.2')
    with pytest.raises(CollectionRouteError,match='PROXY_EXIT_CHANGED'):
        await value.open_context(context)
    with pytest.raises(CollectionRouteError,match='PROXY_EXIT_CHANGED'):
        await value.ensure_business()
    assert value.budget.snapshot()['used']==4
    await value.close()


@pytest.mark.asyncio
async def test_receipt_reused_until_ttl_then_pair_and_cross_context_baseline(tmp_path):
    value, context, _ = session(tmp_path)
    try:
        await value.open_context(context)
        await value.ensure_business()
        assert value.budget.snapshot()['used']==4
        value.receipt = type(value.receipt)(value.receipt.proxy_id,1,value.context_id,value.receipt.ip,time.time()-301)
        await value.ensure_business()
        assert value.budget.snapshot()['used']==6
    finally:
        await value.close()
    second, other_context, _ = session(tmp_path,browser_ip='203.0.113.2',http_ip='203.0.113.2')
    with pytest.raises(CollectionRouteError,match='PROXY_EXIT_CHANGED'):
        await second.open_context(other_context)
    await second.close()


def test_atomic_shared_budget_limits_and_mismatch(tmp_path):
    path=tmp_path/'budget.sqlite'
    budget=SharedProbeBudget(path,'trial',max_calls=8)
    assert budget.reserve('open')==2
    def reserve():
        try:
            return SharedProbeBudget(path,'trial',max_calls=8).reserve('boundary')
        except CollectionRouteError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _: reserve(),range(5)))
    assert results.count(1)==2
    assert results.count('PROBE_BUDGET_EXHAUSTED')==3
    assert budget.snapshot()['used']==8
    with pytest.raises(CollectionRouteError):
        SharedProbeBudget(path,'trial',max_calls=10)


@pytest.mark.asyncio
async def test_two_resources_same_exit_share_scope(tmp_path):
    first,context1,_=session(tmp_path,proxy_id='p1')
    second,context2,_=session(tmp_path,proxy_id='p2')
    try:
        await first.open_context(context1)
        await second.open_context(context2)
        shared=set(first._gate.scope_ids)&set(second._gate.scope_ids)
        assert 'dy:exit:203.0.113.1' in shared
    finally:
        await first.close(); await second.close()


@pytest.mark.asyncio
async def test_boundary_failure_sticky_across_different_budget(tmp_path):
    value,context,_=session(tmp_path)
    await value.open_context(context)
    context.ip='203.0.113.2'
    value.receipt=type(value.receipt)(value.receipt.proxy_id,1,value.context_id,value.receipt.ip,time.time()-301)
    with pytest.raises(CollectionRouteError,match='PROXY_EXIT_CHANGED'):
        await value.ensure_business()
    another,_,_=session(tmp_path,budget_id='different')
    with pytest.raises(CollectionRouteError,match='PROXY_EXIT_CHANGED'):
        another.check_stop()
    await value.close(); await another.close()


def test_receipt_pipe_public_exact_and_nonblocking(tmp_path):
    import os
    from tools.bound_proxy import emit_probe_receipt
    from tools.collection_route import ProbeReceipt
    value=snapshot(tmp_path)
    r,w=os.pipe()
    try:
        receipt=ProbeReceipt('proxy1',1,'context1','203.0.113.1',100)
        assert emit_probe_receipt(value,receipt,env={'MEDIACRAWLER_PROXY_RECEIPT_FD':str(w)})
        record=json.loads(os.read(r,2048))
        assert set(record)=={'schema_version','type','platform','account_id','proxy_id','binding_revision','config_revision','context_id','ip','observed_at'}
        assert record['type']=='probe_receipt'
        assert '密' not in repr(record) and 'password' not in record
        assert emit_probe_receipt(value,receipt,closed=True,env={'MEDIACRAWLER_PROXY_RECEIPT_FD':str(w)})
        assert json.loads(os.read(r,2048))['type']=='receipt_closed'
        assert not emit_probe_receipt(value,receipt,env={'MEDIACRAWLER_PROXY_RECEIPT_FD':str(r)})
    finally:
        os.close(r);os.close(w)


@pytest.mark.asyncio
async def test_production_policy_retains_application_global_env_limits(tmp_path,monkeypatch):
    value,context,_=session(tmp_path)
    value._policy=None
    monkeypatch.setenv('MEDIACRAWLER_REQUEST_MIN_INTERVAL','3')
    monkeypatch.setenv('MEDIACRAWLER_REQUESTS_PER_MINUTE','20')
    monkeypatch.setenv('MEDIACRAWLER_REQUEST_CONCURRENCY','1')
    monkeypatch.setenv('MEDIACRAWLER_MEDIA_REQUEST_INTERVAL','5')
    try:
        await value.open_context(context)
        with sqlite3.connect(value.snapshot.scheduler_db_path) as db:
            limits=json.loads(db.execute("SELECT limits_json FROM request_scheduler_policy WHERE platform='dy'").fetchone()[0])
        assert limits==[3.0,20,1,5.0]
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_maintenance_probes_stopped_resource_without_authorizing_business(tmp_path):
    stopped,_,_=session(tmp_path)
    stopped.fail('PROXY_UNAVAILABLE')
    maintenance,context,_=session(tmp_path,maintenance_only=True)
    try:
        receipt=await maintenance.open_context(context)
        assert receipt.ip=='203.0.113.1'
        assert maintenance._periodic_task is None and maintenance._gate is None
        assert not maintenance._receipt_delivered
        with pytest.raises(CollectionRouteError,match='PROXY_UNAVAILABLE'):
            await maintenance.ensure_business()
        with pytest.raises(CollectionRouteError,match='PROXY_UNAVAILABLE'):
            async with maintenance.slot():
                pytest.fail('maintenance business')
        with pytest.raises(CollectionRouteError,match='PROXY_UNAVAILABLE'):
            maintenance.provenance(observation_id='obs',source_task_id='task',source_batch_id='batch')
        with sqlite3.connect(maintenance.snapshot.scheduler_db_path) as db:
            assert db.execute('SELECT stop_code FROM collection_route_health').fetchone()[0]=='PROXY_UNAVAILABLE'
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='request_scheduler_policy'").fetchone()
        with pytest.raises(CollectionRouteError,match='PROXY_UNAVAILABLE'):
            stopped.check_stop()
    finally:
        await maintenance.close();await stopped.close()


@pytest.mark.asyncio
async def test_probe_pending_marker_precedes_context_page_event_and_cdp_blocker(tmp_path):
    value,context,_=session(tmp_path)
    events=[]
    class CDP:
        async def send(self,name,params=None):
            events.append((name,params))
        async def detach(self):
            assert context.pages[-1].closed
            events.append(('detached',None))
    async def new_page():
        assert context._collection_proxy_probe_pending is True
        return await FakeContext.new_page(context)
    context.new_page=new_page
    async def new_cdp(page):
        assert page._collection_proxy_probe is True
        return CDP()
    context.new_cdp_session=new_cdp
    try:
        await value.open_context(context)
        assert context._collection_proxy_probe_pending is False
        assert events[0]==('Network.enable',None)
        assert events[1]==('Network.setBlockedURLs',{'urls':['https://echo.invalid/favicon.ico']})
        assert events.count(('detached',None))==2
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_proxy_auth_chromium_error_is_separate_from_connectivity(tmp_path):
    value,context,_=session(tmp_path)
    async def new_page():
        page=await FakeContext.new_page(context)
        async def evaluate(*args,**kwargs):
            raise RuntimeError('net::ERR_INVALID_AUTH_CREDENTIALS secret-canary')
        page.evaluate=evaluate
        return page
    context.new_page=new_page
    with pytest.raises(CollectionRouteError,match='PROXY_AUTH_FAILED') as error:
        await value.open_context(context)
    assert 'secret-canary' not in str(error.value)
    await value.close()


@pytest.mark.asyncio
async def test_explicit_parent_trial_id_enables_same_budget_without_global_env(tmp_path,monkeypatch):
    monkeypatch.delenv('MEDIACRAWLER_PROXY_TRIAL_ID',raising=False)
    value,context,_=session(tmp_path,trial_id='parent_trial')
    try:
        await value.open_context(context)
        assert value.trial_limits is not None
        async with value.slot():
            pass
        assert value.trial_limits.snapshot()['operations']==1
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_unknown_order_expiry_bootstrap_receipt_never_allows_business(tmp_path):
    from dataclasses import replace
    value,context,_=session(tmp_path)
    value.snapshot=replace(value.snapshot,expires_at=None)
    from tools.collection_route import ProbeGuard
    value.guard=ProbeGuard(value.snapshot)
    try:
        receipt=await value.open_context(context)
        assert receipt.ip=='203.0.113.1' and value._gate is None
        with pytest.raises(CollectionRouteError,match='PROXY_UNAVAILABLE'):
            await value.ensure_business()
        with pytest.raises(CollectionRouteError,match='PROXY_UNAVAILABLE'):
            async with value.slot():
                pytest.fail('unknown expiry business')
        with sqlite3.connect(value.snapshot.scheduler_db_path) as db:
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='request_scheduler_policy'").fetchone()
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_supplement_reuses_existing_slow_account_policy_and_history(tmp_path):
    from tools.persistent_request_gate import RequestScheduler
    path=tmp_path/'gate.sqlite'
    policy=dict(min_interval=0,per_minute=100,max_concurrency=1,media_interval=0,cooldown_seconds=1)
    RequestScheduler(path,platform='dy',**policy)
    account=RequestScheduler(path,platform='dy:account:account1',**{**policy,'min_interval':15,'per_minute':6})
    account.enter_cooldown('original account cooldown',60)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE request_scheduler_state SET request_times_json=? WHERE platform=?",(json.dumps([time.time()-10]),'dy:account:account1'))
        before=db.execute("SELECT * FROM request_scheduler_state WHERE platform='dy:account:account1'").fetchone()
    value,context,_=session(tmp_path,reuse_existing_policy=True)
    try:
        await value.open_context(context)
        with sqlite3.connect(path) as db:
            policies=dict(db.execute('SELECT platform,limits_json FROM request_scheduler_policy'))
            after=db.execute("SELECT * FROM request_scheduler_state WHERE platform='dy:account:account1'").fetchone()
        assert json.loads(policies['dy:account:account1'])==[15.0,6,1,0.0]
        assert after==before
        assert json.loads(policies['dy:proxy:proxy1'])==json.loads(policies['dy'])
        assert json.loads(policies['dy:exit:203.0.113.1'])==json.loads(policies['dy'])
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_missing_account_supplement_requires_explicit_conservative_fallback(tmp_path):
    from tools.persistent_request_gate import RequestScheduler
    policy=dict(min_interval=3,per_minute=20,max_concurrency=1,media_interval=5,cooldown_seconds=300)
    RequestScheduler(tmp_path/'gate.sqlite',platform='dy',**policy)
    value,context,_=session(tmp_path,reuse_existing_policy=True,account_policy={'min_interval':15,'per_minute':6})
    try:
        await value.open_context(context)
        with sqlite3.connect(tmp_path/'gate.sqlite') as db:
            row=db.execute("SELECT limits_json FROM request_scheduler_policy WHERE platform='dy:account:account1'").fetchone()
        assert json.loads(row[0])==[15.0,6,1,5.0]
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_missing_account_supplement_without_fallback_fails_closed(tmp_path):
    from tools.persistent_request_gate import RequestScheduler
    RequestScheduler(tmp_path/'gate.sqlite',platform='dy',min_interval=3,per_minute=20)
    value,context,_=session(tmp_path,reuse_existing_policy=True)
    with pytest.raises(CollectionRouteError,match='SCHEDULER_POLICY_CONFLICT'):
        await value.open_context(context)
    with sqlite3.connect(tmp_path/'gate.sqlite') as db:
        assert db.execute("SELECT 1 FROM request_scheduler_policy WHERE platform='dy:account:account1'").fetchone() is None
    await value.close()


@pytest.mark.asyncio
async def test_http_echo_custom_user_agent_without_platform_credentials(tmp_path, monkeypatch):
    value, _, _ = session(tmp_path)
    requests = []
    options = []
    monkeypatch.setenv('HTTP_PROXY', 'http://trap.invalid:8080')
    monkeypatch.setenv('NO_PROXY', '*')

    def factory(**kwargs):
        options.append(kwargs)
        def respond(request):
            requests.append(request)
            return httpx.Response(200, text='203.0.113.1')
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False)

    value._http_client_factory = factory
    try:
        assert await value._http_sample() == '203.0.113.1'
        assert len(requests) == 1
        request = requests[0]
        assert str(request.url) == value.echo_url
        assert request.headers['User-Agent'] == 'ZhanyunProxyProbe/1.0'
        assert request.headers['Accept'] == 'application/json,text/plain'
        assert all(header not in request.headers for header in (
            'Cookie', 'Authorization', 'Proxy-Authorization'))
        assert options[0]['proxy'] == value.snapshot.httpx_proxy()
        assert options[0]['trust_env'] is False
        assert options[0]['follow_redirects'] is False
        assert options[0]['verify'] is True
    finally:
        await value.close()


def test_login_bootstrap_requires_explicit_bool_and_trial_identity(tmp_path, monkeypatch):
    monkeypatch.delenv('MEDIACRAWLER_PROXY_TRIAL_ID', raising=False)
    with pytest.raises(CollectionRouteError, match='PROXY_CONFIG_INVALID'):
        session(tmp_path, login_bootstrap=True)
    with pytest.raises(CollectionRouteError, match='PROXY_CONFIG_INVALID'):
        session(tmp_path, trial_id='trial', login_bootstrap=1)


def test_trial_login_reallocates_remaining_echo_budget_without_reset_or_total_increase(tmp_path):
    path = tmp_path/'budget.sqlite'
    budget = SharedProbeBudget(path, 'same-trial', max_calls=96)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE collection_probe_budget SET used=30,initial=1,opens=10,boundaries=3 WHERE budget_id=?', ('same-trial',))
    with pytest.raises(CollectionRouteError, match='PROBE_BUDGET_EXHAUSTED'):
        budget.reserve('open')
    assert budget.snapshot()['used'] == 30
    assert budget.reserve('open', use_remaining=True) == 1
    assert budget.snapshot() == {'max_calls':96,'used':32,'initial':1,'opens':11,'ticks':0,'boundaries':3}
    with sqlite3.connect(path) as db:
        db.execute('UPDATE collection_probe_budget SET used=95 WHERE budget_id=?', ('same-trial',))
    with pytest.raises(CollectionRouteError, match='PROBE_BUDGET_EXHAUSTED'):
        budget.reserve('open', use_remaining=True)
    assert budget.snapshot()['used'] == 95
    assert budget.snapshot()['opens'] == 11
    for category, flag in [('tick',True), ('boundary',True), ('open',1)]:
        with pytest.raises(CollectionRouteError, match='PROXY_CONFIG_INVALID'):
            budget.reserve(category, use_remaining=flag)


@pytest.mark.asyncio
async def test_only_explicit_trial_login_session_reallocates_open_budget(tmp_path):
    value, context, _ = session(tmp_path, trial_id='trial-login', login_bootstrap=True)
    with sqlite3.connect(value.snapshot.scheduler_db_path) as db:
        db.execute('UPDATE collection_probe_budget SET used=30,initial=1,opens=10,boundaries=3 WHERE budget_id=?', (value.budget.budget_id,))
    try:
        await value.open_context(context)
        assert value.budget.snapshot()['used'] == 32
        assert value.budget.snapshot()['opens'] == 11
    finally:
        await value.close()
