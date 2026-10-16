import asyncio
import json
import os
import sqlite3
import time

import pytest

from tools import persistent_request_gate as gates
from tools.authority_ledger import process_start
from tools.request_authority import RemoteGate

SCOPES=['dy','dy:account:media']


def registered(tmp_path):
    db=tmp_path/'gate.sqlite3'
    for scope in SCOPES:
        gates.RequestScheduler(db,platform=scope,min_interval=0,per_minute=100,max_concurrency=1,media_interval=0)
    return db


def test_renewal_is_atomic_process_bound_and_never_debits_quota(tmp_path):
    db=registered(tmp_path)
    held=gates.try_reserve_scopes(db,SCOPES,'media',90)
    tokens=held['scope_tokens'];pid=os.getpid();identity=process_start(pid)
    with sqlite3.connect(db) as connection:
        before=connection.execute('SELECT platform,request_times_json FROM request_scheduler_state ORDER BY platform').fetchall()
        connection.execute('UPDATE request_scheduler_leases SET expires_at=0')
    # A live expired lease still consumes capacity while heartbeat is delayed.
    assert gates.try_reserve_scopes(db,SCOPES,'media',90)['reason']=='concurrency'
    now=time.time()
    assert gates.renew_scope_tokens(db,tokens,pid=pid,start_identity=identity,now=now)=={'expires_at':now+90}
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT platform,request_times_json FROM request_scheduler_state ORDER BY platform').fetchall()==before
        connection.execute('DELETE FROM request_scheduler_leases WHERE token=?',(tokens[SCOPES[1]],))
        snapshot=connection.execute('SELECT expires_at FROM request_scheduler_leases').fetchall()
    with pytest.raises(RuntimeError):gates.renew_scope_tokens(db,tokens,pid=pid,start_identity=identity,now=now+10)
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT expires_at FROM request_scheduler_leases').fetchall()==snapshot
    with pytest.raises(RuntimeError):gates.renew_scope_tokens(db,{SCOPES[0]:tokens[SCOPES[0]]},pid=pid,start_identity='recycled-pid')


@pytest.mark.asyncio
async def test_local_media_slot_renews_without_a_total_deadline_or_extra_quota(tmp_path,monkeypatch):
    db=registered(tmp_path)
    monkeypatch.setattr(gates,'MEDIA_HEARTBEAT_SECONDS',.01)
    gate=gates.MultiScopeGate(db,SCOPES)
    async with gate.slot('media',timeout=None):
        await asyncio.sleep(.055)
        with sqlite3.connect(db) as connection:
            rows=connection.execute('SELECT expires_at FROM request_scheduler_leases').fetchall()
            assert len(rows)==2 and all(row[0]>time.time()+80 for row in rows)
            assert all(len(json.loads(row[0]))==1 for row in connection.execute('SELECT request_times_json FROM request_scheduler_state'))
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT COUNT(*) FROM request_scheduler_leases').fetchone()[0]==0
    with pytest.raises(ValueError):
        async with gate.slot('api',timeout=None):pass


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_kind',['renew','stop','cancel','cancel_release_failure'])
async def test_remote_heartbeat_failure_closes_body_before_releasing(failure_kind,monkeypatch):
    monkeypatch.setattr(gates,'MEDIA_HEARTBEAT_SECONDS',.01)
    events=[]
    class Authority:
        async def acall(self,operation,payload=None):
            events.append(operation)
            if operation=='renew_request':
                assert payload=={'scope_tokens':{'dy':'token'}}
                if failure_kind=='renew':raise RuntimeError('AUTHORITY_LOST')
                return {'expires_at':time.time()+90}
            if operation=='release' and failure_kind=='cancel_release_failure':raise RuntimeError('OWNER_LOST')
            return None
    async def stop():
        events.append('check_stop')
        if failure_kind=='stop':raise RuntimeError('ACCOUNT_CHALLENGE')
    gate=RemoteGate(Authority(),stop_checker=stop)
    async def reserve(*args,**kwargs):
        assert kwargs['lease_seconds']==90
        return {'dy':'token'},0
    monkeypatch.setattr(gate,'reserve_scopes',reserve)
    async def transfer():
        async with gate.slot('media',timeout=None):
            try:await asyncio.sleep(10)
            finally:events.append('body_closed')
    task=asyncio.create_task(transfer())
    if failure_kind.startswith('cancel'):
        await asyncio.sleep(.025);task.cancel()
    with pytest.raises(asyncio.CancelledError if failure_kind.startswith('cancel') else RuntimeError):await task
    assert events.index('body_closed')<events.index('release')
    count=len(events);await asyncio.sleep(.025);assert len(events)==count


def test_renewal_rejects_new_cooldown_without_extending_any_scope(tmp_path):
    db=registered(tmp_path)
    tokens=gates.try_reserve_scopes(db,SCOPES,'media',90)['scope_tokens']
    with sqlite3.connect(db) as connection:
        before=connection.execute('SELECT token,expires_at FROM request_scheduler_leases ORDER BY token').fetchall()
    gates.enter_cooldown_scopes(db,[SCOPES[1]],'HTTP 429',30)
    with pytest.raises(RuntimeError):
        gates.renew_scope_tokens(db,tokens,pid=os.getpid(),start_identity=process_start(os.getpid()))
    with sqlite3.connect(db) as connection:
        assert connection.execute('SELECT token,expires_at FROM request_scheduler_leases ORDER BY token').fetchall()==before


@pytest.mark.asyncio
async def test_external_cancel_during_close_is_not_cancelled_again_by_heartbeat(monkeypatch):
    monkeypatch.setattr(gates, 'MEDIA_HEARTBEAT_SECONDS', .001)
    renewing = asyncio.Event()
    closing = asyncio.Event()
    closed = asyncio.Event()

    async def renew():
        renewing.set()
        await closing.wait()
        raise RuntimeError('AUTHORITY_LOST')

    async def transfer():
        async with gates.renewable_media_lifetime(renew):
            try:
                await asyncio.sleep(10)
            finally:
                closing.set()
                await asyncio.sleep(.03)
                closed.set()

    task = asyncio.create_task(transfer())
    await renewing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert task.cancelling() == 1


@pytest.mark.asyncio
async def test_heartbeat_removes_only_its_own_cancellation(monkeypatch):
    monkeypatch.setattr(gates, 'MEDIA_HEARTBEAT_SECONDS', .001)

    async def renew():
        raise RuntimeError('AUTHORITY_LOST')

    async def transfer():
        with pytest.raises(RuntimeError, match='AUTHORITY_LOST'):
            async with gates.renewable_media_lifetime(renew):
                await asyncio.sleep(10)
        assert asyncio.current_task().cancelling() == 0

    await asyncio.create_task(transfer())


@pytest.mark.asyncio
async def test_external_cancel_after_heartbeat_cancel_takes_priority(monkeypatch):
    monkeypatch.setattr(gates, 'MEDIA_HEARTBEAT_SECONDS', .001)
    closing = asyncio.Event()

    async def renew():
        raise RuntimeError('AUTHORITY_LOST')

    async def transfer():
        async with gates.renewable_media_lifetime(renew):
            try:
                await asyncio.sleep(10)
            finally:
                closing.set()
                await asyncio.sleep(10)

    task = asyncio.create_task(transfer())
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelling() == 1
