"""Automatic browser admission must distinguish safety pauses from transport faults."""
from dataclasses import replace
import errno
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from media_platform.douyin import client as client_module
from media_platform.douyin.exception import PlatformRateLimitedError
from tests.test_proxy_exit_observation import FakeContext, snapshot
from tools import bound_proxy as bound_module
from tools import collection_route as route
from tools.bound_proxy import BoundProxySession


class BusinessContext(FakeContext):
    async def route(self, pattern, callback):
        self.route_handlers.append(callback)


def managed_session(tmp_path, *, account='a', proxy='p', exit_ip='203.0.113.10',
                    mode='proxy_required', generation=1, safety_db=''):
    value = replace(snapshot(tmp_path, proxy_id=proxy, exit_ip=exit_ip),
        account_id=account, profile_key=route.derive_profile_key(account), route_mode=mode,
        operation_id='operation-' + account, lease_id='lease-' + account, lease_version=1,
        owner_id='owner', exit_generation=generation, context_id='context-' + account,
        safety_db_path=str(safety_db) if safety_db else '',
        scope_ids=route.derive_scope_ids('dy', account, proxy, exit_ip))
    if mode == 'direct_degraded':
        value = replace(value, username='', password='', proxy_id='service-direct',
            scope_ids=tuple(sorted(['dy', 'dy:account:' + account,
                'dy:exit:' + exit_ip, 'dy:service_exit:' + exit_ip])))
    value = replace(value, snapshot_digest=route.snapshot_digest(value))
    session = BoundProxySession(value, echo_url='https://echo.invalid/ip',
        budget_id='budget-' + account, ack=route.route_ack(value, owner_id='owner', lease_version=1, ttl=60),
        http_client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'ip': exit_ip}))),
        policy=dict(min_interval=0, per_minute=1000, max_concurrency=1,
            media_interval=0, cooldown_seconds=1))
    return session, BusinessContext(exit_ip)


async def automatic_request(context):
    request = SimpleNamespace(url='https://business.invalid/asset', method='GET',
        redirected_from=None, headers={})
    intercepted = SimpleNamespace(request=request, abort=AsyncMock(), fallback=AsyncMock())
    await context.route_handlers[0](intercepted)
    return intercepted


@pytest.mark.asyncio
async def test_http_429_blocks_automatic_requests_for_neighbors_and_independent_exits(tmp_path, monkeypatch):
    pairs = [managed_session(tmp_path, account=account, proxy=proxy, exit_ip=ip)
        for account, proxy, ip in [('a', 'p', '203.0.113.10'),
            ('a', 'p', '203.0.113.10'), ('b', 'q', '203.0.113.10'),
            ('c', 'r', '203.0.113.11')]]
    monkeypatch.setattr(client_module, 'make_async_client', lambda **kwargs:
        httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={'Retry-After': '9'}))))
    try:
        for session, context in pairs:
            await session.open_context(context)
        first = pairs[0][0]
        client = client_module.DouYinClient(headers={}, playwright_page=None,
            cookie_dict={}, bound_proxy_session=first)
        with pytest.raises(PlatformRateLimitedError):
            await client._send('GET', 'https://business.invalid/api')
        assert all(state['cooldown_until'] > time.time() + 7
            for state in first._gate.snapshots().values())
        for _, context in pairs:
            intercepted = await automatic_request(context)
            intercepted.abort.assert_awaited_once()
            intercepted.fallback.assert_not_awaited()
    finally:
        for session, _ in pairs:
            await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_scope', ['platform', 'account', 'exit', 'service_exit'])
async def test_each_scope_cooldown_denies_browser_reservation_without_spending_capacity(
        tmp_path, monkeypatch, blocked_scope):
    from tools.persistent_request_gate import enter_cooldown_scopes
    session, context = managed_session(tmp_path, mode='direct_degraded')
    try:
        await session.open_context(context)
        names = {'platform': 'dy', 'account': 'dy:account:a', 'exit': 'dy:exit:203.0.113.10',
            'service_exit': 'dy:service_exit:203.0.113.10'}
        now = time.time()
        enter_cooldown_scopes(session.snapshot.scheduler_db_path,
            [names[blocked_scope]], 'HTTP 429', 9, now=now)
        with pytest.raises(route.CollectionRouteError, match='PROXY_UNAVAILABLE'):
            session._reserve_browser_request()
        monkeypatch.setattr(bound_module.time, 'time', lambda: now + 10)
        # The refused reservation must leave all forty direct-browser slots available.
        for _ in range(40):
            intercepted = await automatic_request(context)
            intercepted.fallback.assert_awaited_once()
            intercepted.abort.assert_not_awaited()
            session._release_browser_request(intercepted.request)
        exhausted = await automatic_request(context)
        exhausted.abort.assert_awaited_once()
        exhausted.fallback.assert_not_awaited()
    finally:
        await session.close()


def safety_contract(path, session):
    """App owns producing these canonical rows; this fixture exercises its reader."""
    value = session.snapshot
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE crawler_shared_exits(exit_key TEXT,generation INTEGER,observed_at REAL,
                paused_until REAL,pause_reason TEXT);
            CREATE TABLE crawler_accounts(id TEXT,recovery_claim TEXT,recovery_state TEXT);
            CREATE TABLE crawler_route_admissions(operation_id TEXT,account_id TEXT,
                exit_key TEXT,generation INTEGER,state TEXT);
        ''')
        db.execute('INSERT INTO crawler_shared_exits VALUES(?,?,?,NULL,?)',
            (value.observed_exit_ip, value.exit_generation, time.time(), ''))
        db.execute('INSERT INTO crawler_accounts VALUES(?,?,?)', (value.account_id, '', ''))
        db.execute('INSERT INTO crawler_route_admissions VALUES(?,?,?,?,?)',
            (value.operation_id, value.account_id, value.observed_exit_ip, value.exit_generation, 'active'))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['proxy_required', 'direct_degraded'])
async def test_canonical_pause_stops_locally_then_trusted_recovery_can_reopen_route(
        tmp_path, capsys, mode):
    path = tmp_path / 'safety.sqlite3'
    session, context = managed_session(tmp_path, mode=mode, safety_db=path)
    safety_contract(path, session)
    sessions = [session]
    try:
        await session.open_context(context)
        capsys.readouterr()
        with sqlite3.connect(path) as db:
            db.execute('UPDATE crawler_shared_exits SET pause_reason=?,paused_until=?',
                ('ACCOUNT_CHALLENGE', time.time() + 60))
        blocked = await automatic_request(context)
        blocked.abort.assert_awaited_once()
        with pytest.raises(route.CollectionRouteError, match='PROXY_UNAVAILABLE'):
            session.check_stop()
        assert 'COLLECTION_ROUTE_EVENT' not in capsys.readouterr().out
        with sqlite3.connect(path) as db:
            db.execute("UPDATE crawler_shared_exits SET pause_reason='',paused_until=NULL")
        recovered, recovered_context = managed_session(tmp_path, mode=mode, safety_db=path)
        sessions.append(recovered)
        await recovered.open_context(recovered_context)
        allowed = await automatic_request(recovered_context)
        allowed.fallback.assert_awaited_once()
        allowed.abort.assert_not_awaited()
        unrelated, unrelated_context = managed_session(tmp_path, account='b', proxy='q',
            mode='direct_degraded', exit_ip='203.0.113.11', generation=2)
        sessions.append(unrelated)
        await unrelated.open_context(unrelated_context)
        (await automatic_request(unrelated_context)).fallback.assert_awaited_once()
    finally:
        for value in sessions:
            await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['proxy_required', 'direct_degraded'])
@pytest.mark.parametrize('fault,expected', [('auth', 'PROXY_AUTH_FAILED'),
    ('refused', 'PROXY_CONNECTION_REFUSED'), ('connection', 'PROXY_TRANSPORT_FAILED')])
async def test_classified_transport_fault_persists_only_for_affected_resource_or_direct_exit_generation(
        tmp_path, monkeypatch, capsys, mode, fault, expected):
    session, context = managed_session(tmp_path, mode=mode)
    sessions = [session]
    def handler(request):
        if fault == 'auth':
            return httpx.Response(407)
        if fault == 'refused':
            raise httpx.ConnectError('fixture') from OSError(errno.ECONNREFUSED, 'fixture')
        raise httpx.ConnectError('fixture')
    monkeypatch.setattr(client_module, 'make_async_client', lambda **kwargs:
        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await session.open_context(context)
        capsys.readouterr()
        client = client_module.DouYinClient(headers={}, playwright_page=None,
            cookie_dict={}, bound_proxy_session=session)
        with pytest.raises(route.CollectionRouteError, match=expected):
            await client._send('GET', 'https://business.invalid/api')
        assert expected in capsys.readouterr().out
        reopened, _ = managed_session(tmp_path, mode=mode)
        sessions.append(reopened)
        with pytest.raises(route.CollectionRouteError, match=expected):
            reopened.check_stop()
        neighbors = [('b', 'q', '203.0.113.11', 1)]
        if mode == 'direct_degraded':
            neighbors.append(('c', 'r', '203.0.113.10', 2))
        for account, proxy, ip, generation in neighbors:
            independent, independent_context = managed_session(tmp_path, account=account,
                proxy=proxy, exit_ip=ip, mode=mode, generation=generation)
            sessions.append(independent)
            await independent.open_context(independent_context)
            (await automatic_request(independent_context)).fallback.assert_awaited_once()
    finally:
        for value in sessions:
            await value.close()
