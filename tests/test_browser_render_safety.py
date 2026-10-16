"""Rendering traffic is fenced without consuming business or legacy resource budgets."""
import json
import asyncio
import os
import time
import sqlite3
from dataclasses import replace

import pytest

from tests.test_bound_proxy_safety_regressions import managed_session, automatic_request,safety_contract


@pytest.mark.asyncio
async def test_login_bootstrap_preserves_login_v2_cache_and_cdp_path(tmp_path):
    session,context=managed_session(tmp_path)
    session._login_bootstrap=True
    context_route_calls=0
    original_route=context.route
    async def tracked_route(pattern,callback):
        nonlocal context_route_calls
        context_route_calls+=1
        await original_route(pattern,callback)
    context.route=tracked_route
    try:
        await session.open_context(context)
        assert context_route_calls==0
        assert session.receipt is not None
    finally:await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['proxy_required','direct_degraded'])
async def test_rendering_over_300_resources_ignores_old_capacity_and_preserves_business_quota(tmp_path,mode):
    session,context=managed_session(tmp_path,mode=mode)
    try:
        await session.open_context(context)
        with session.budget._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS collection_browser_requests (token TEXT PRIMARY KEY,exit_key TEXT,pid INTEGER,started_at REAL,active INTEGER)')
            for index in range(400):
                db.execute('INSERT INTO collection_browser_requests VALUES(?,?,?,?,1)',(str(index),session.snapshot.observed_exit_ip,os.getpid(),time.time()))
            db.execute('UPDATE request_scheduler_policy SET limits_json=? WHERE platform=?',(json.dumps([0,1,1,0]),'dy'))
        async with session.slot('api',max_wait=.01):pass
        for _ in range(400):
            request=await automatic_request(context)
            request.fallback.assert_awaited_once();request.abort.assert_not_awaited()
        assert not context.closed and not getattr(session,'_browser_guard_first_reason',None)
        with session.budget._connect() as db:
            assert db.execute('SELECT COUNT(*) FROM collection_browser_requests').fetchone()[0]==400
            assert len(json.loads(db.execute("SELECT request_times_json FROM request_scheduler_state WHERE platform='dy'").fetchone()[0]))==1
        with pytest.raises(RuntimeError):
            async with session.slot('api',max_wait=.01):pass
    finally:await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid',['generation','ttl','ended','unknown','manual_owner','expired_ack'])
async def test_registered_render_guard_still_rejects_invalid_execution(tmp_path,invalid):
    from tools import collection_route
    path=tmp_path/'safety.db';session,context=managed_session(tmp_path,safety_db=path)
    safety_contract(path,session)
    try:
        await session.open_context(context)
        with sqlite3.connect(path) as db:
            if invalid=='generation':db.execute('UPDATE crawler_shared_exits SET generation=2')
            elif invalid=='ttl':db.execute('UPDATE crawler_shared_exits SET observed_at=?',(time.time()-301,))
            elif invalid=='ended':db.execute("UPDATE crawler_route_admissions SET state='ended'")
            elif invalid=='unknown':db.execute('DELETE FROM crawler_shared_exits')
            elif invalid=='manual_owner':
                snapshot=replace(session.snapshot,recovery_claim='manual-claim')
                snapshot=replace(snapshot,snapshot_digest=collection_route.snapshot_digest(snapshot))
                session.snapshot=snapshot;session._lease_ack=collection_route.route_ack(snapshot,owner_id='owner',lease_version=1,ttl=60)
                db.execute("UPDATE crawler_accounts SET recovery_claim='manual-claim',recovery_state='manual_claimed'")
                db.execute("UPDATE crawler_shared_exits SET pause_reason='ACCOUNT_CHALLENGE',paused_until=?",(time.time()+60,))
                db.execute('CREATE TABLE crawler_manual_recovery_operations(account_id TEXT,claim TEXT,owner_id TEXT,state TEXT)')
                db.execute("INSERT INTO crawler_manual_recovery_operations VALUES('a','manual-claim','wrong-owner','claimed')")
            else:session._lease_ack['expires_at']=time.time()-1
        request=await automatic_request(context)
        request.abort.assert_awaited_once();request.fallback.assert_not_awaited()
        with session.budget._connect() as db:
            assert not db.execute('SELECT * FROM collection_route_health').fetchall()
            assert all(json.loads(row[0])==[] for row in db.execute('SELECT request_times_json FROM request_scheduler_state'))
    finally:await session.close()


@pytest.mark.asyncio
async def test_real_douyin_client_business_quota_still_blocks_next_transport_after_rendering(tmp_path,monkeypatch):
    import httpx
    from media_platform.douyin import client as client_module
    session,context=managed_session(tmp_path);sent=[]
    def respond(request):
        sent.append(request);return httpx.Response(200,json={'status_code':0,'data':[]})
    monkeypatch.setattr(client_module,'make_async_client',lambda **kwargs:httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    try:
        await session.open_context(context)
        with session.budget._connect() as db:
            db.execute('UPDATE request_scheduler_policy SET limits_json=? WHERE platform=?',(json.dumps([0,1,1,0]),'dy'))
        client=client_module.DouYinClient(headers={},playwright_page=None,cookie_dict={},bound_proxy_session=session)
        await client._send('GET','https://business.invalid/api')
        for _ in range(350):(await automatic_request(context)).fallback.assert_awaited_once()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client._send('GET','https://business.invalid/api2'),.02)
        assert len(sent)==1 and not context.closed
    finally:await session.close()
