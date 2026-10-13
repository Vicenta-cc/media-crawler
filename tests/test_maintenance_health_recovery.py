"""Only completed exact maintenance echo proof retires historical uncertainty."""
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from tools.bound_proxy import BoundProxySession
from tools.collection_route import CollectionRouteError
from tests.test_proxy_exit_observation import snapshot,FakeContext
from tests.test_bound_proxy_safety_regressions import managed_session


IP='203.0.113.10'


def maintenance(tmp_path, *, revision=1,exit_ip=IP):
    value=replace(snapshot(tmp_path,proxy_id='p',exit_ip=exit_ip),config_revision=revision)
    return BoundProxySession(value,maintenance_only=True,echo_url='https://echo.invalid/ip',budget_id='maintenance-proof',
        http_client_factory=lambda **kwargs:httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'ip':IP})))),FakeContext(IP)


def mark(session, code='PROXY_UNAVAILABLE',revision=1):
    with session.budget._connect() as db:db.execute('INSERT OR REPLACE INTO collection_route_health VALUES(?,?,?)',('p',revision,code))


def markers(session):
    with session.budget._connect() as db:return db.execute('SELECT proxy_id,config_revision,stop_code FROM collection_route_health').fetchall()


@pytest.mark.asyncio
async def test_ambiguous_maintenance_failure_does_not_persist_transport_health(tmp_path):
    session,context=maintenance(tmp_path)
    session._browser_sample=AsyncMock(side_effect=CollectionRouteError('PROXY_UNAVAILABLE'))
    try:
        with pytest.raises(CollectionRouteError,match='PROXY_UNAVAILABLE'):await session.open_context(context)
        assert markers(session)==[]
    finally:await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['partial','changed_exit','wrong_revision'])
async def test_partial_or_nonmatching_maintenance_proof_keeps_uncertainty(tmp_path,reason):
    session,context=maintenance(tmp_path,revision=2 if reason=='wrong_revision' else 1,exit_ip='203.0.113.11' if reason=='changed_exit' else IP)
    mark(session)
    if reason=='partial':session._browser_sample=AsyncMock(side_effect=[IP,CollectionRouteError('PROXY_UNAVAILABLE')])
    try:
        if reason=='partial':
            with pytest.raises(CollectionRouteError):await session.open_context(context)
        else:await session.open_context(context)
        assert ('p',1,'PROXY_UNAVAILABLE') in markers(session)
    finally:await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('code',['PROXY_AUTH_FAILED','PROXY_CONNECTION_REFUSED','PROXY_TRANSPORT_FAILED','PROXY_EXPIRED','PROXY_REVISION_MISMATCH'])
async def test_classified_transport_health_is_not_cleared_by_echo_success(tmp_path,code):
    session,context=maintenance(tmp_path);mark(session,code)
    try:
        await session.open_context(context)
        fresh,_=managed_session(tmp_path)
        try:
            with pytest.raises(CollectionRouteError,match=code):fresh.check_stop()
        finally:await fresh.close()
        assert markers(session)==[('p',1,code)]
    finally:await session.close()


