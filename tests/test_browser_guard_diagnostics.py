import json
import os
import sqlite3

import pytest

from tests.test_bound_proxy_safety_regressions import managed_session, automatic_request
from tools import bound_proxy


@pytest.mark.asyncio
@pytest.mark.parametrize('error,reason',[(bound_proxy.CollectionRouteError('PROXY_UNAVAILABLE'),'BOUND_REQUEST_GUARD'),(RuntimeError('private'),'BOUND_RUNTIME_GUARD'),(sqlite3.OperationalError('private'),'BOUND_DATABASE_GUARD')])
async def test_request_guard_preserves_route_runtime_database_first_cause(tmp_path,monkeypatch,error,reason):
    session,context=managed_session(tmp_path);events=[]
    monkeypatch.setattr(bound_proxy,'emit_browser_guard_stopped',lambda snapshot,value:events.append(value))
    try:
        await session.open_context(context)
        def stopped():raise error
        monkeypatch.setattr(session,'check_stop',stopped)
        (await automatic_request(context)).abort.assert_awaited_once()
        assert events==[reason]
    finally:await session.close()


def test_anonymous_publisher_emits_only_bound_identity_and_fixed_reason(tmp_path,monkeypatch):
    session,_=managed_session(tmp_path)
    read_fd,write_fd=os.pipe()
    monkeypatch.setenv('MEDIACRAWLER_PROXY_RECEIPT_FD',str(write_fd))
    try:
        assert not bound_proxy.emit_browser_guard_stopped(session.snapshot,'https://secret.invalid/password')
        assert bound_proxy.emit_browser_guard_stopped(session.snapshot,'PAGE_SIZES_FAILED')
        value=json.loads(os.read(read_fd,4096))
        assert set(value)=={'type','operation_id','owner_id','lease_version','snapshot_digest','context_id','reason'}
        assert value['snapshot_digest']==session.snapshot.snapshot_digest and value['reason']=='PAGE_SIZES_FAILED'
        assert 'secret' not in json.dumps(value)
    finally:os.close(read_fd);os.close(write_fd)


@pytest.mark.asyncio
async def test_closed_session_stops_locally_and_keeps_first_reason_without_transport_health(tmp_path,monkeypatch):
    session,context=managed_session(tmp_path)
    events=[]
    monkeypatch.setattr(bound_proxy,'emit_browser_guard_stopped',lambda snapshot,reason:events.append(reason))
    await session.open_context(context)
    await session.close()
    stopped=await automatic_request(context)
    stopped.abort.assert_awaited_once()
    session.record_browser_guard_stop('CDP_GUARD_TIMEOUT')
    assert events==['BOUND_REQUEST_GUARD']
    with session.budget._connect() as db:
        assert not db.execute('SELECT * FROM collection_route_health').fetchall()
