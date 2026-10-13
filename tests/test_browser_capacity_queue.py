import asyncio
import subprocess
import sys
import time

import pytest

from tests.test_bound_proxy_safety_regressions import managed_session,automatic_request,safety_contract


def active(session):
    with session.budget._connect() as db:
        return db.execute('SELECT COUNT(*) FROM collection_browser_requests WHERE active=1').fetchone()[0]


@pytest.mark.asyncio
async def test_pending_queue_is_hard_bounded_and_cancellation_releases_only_queue_count(tmp_path):
    session,context=managed_session(tmp_path);pending=[]
    try:
        await session.open_context(context)
        for _ in range(32):session._reserve_browser_request()
        pending=[asyncio.create_task(automatic_request(context)) for _ in range(300)]
        await asyncio.sleep(.01)
        assert session._browser_pending_count==300 and active(session)==32
        overflow=await automatic_request(context)
        overflow.abort.assert_awaited_once();overflow.fallback.assert_not_awaited()
        assert session._browser_guard_first_reason=='BROWSER_QUEUE_LIMIT'
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
        assert session._browser_pending_count==0 and active(session)==32
    finally:
        for task in pending:task.cancel()
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,limit',[('proxy_required',32),('direct_degraded',16)])
@pytest.mark.parametrize('cross_process',[False,True])
async def test_registered_route_waits_then_atomically_resumes_without_exceeding_cap(tmp_path,mode,limit,cross_process):
    session,context=managed_session(tmp_path,mode=mode)
    try:
        await session.open_context(context)
        tokens=[session._reserve_browser_request() for _ in range(limit)]
        pending=asyncio.create_task(automatic_request(context))
        await asyncio.sleep(.02)
        assert not pending.done() and not context.closed
        assert not getattr(session,'_browser_guard_first_reason',None)
        assert active(session)==limit
        if cross_process:
            with session.budget._connect() as db:
                path=db.execute('PRAGMA database_list').fetchone()[2]
            subprocess.run([sys.executable,'-c',"import sqlite3,sys;db=sqlite3.connect(sys.argv[1]);db.execute('UPDATE collection_browser_requests SET active=0 WHERE token=?',(sys.argv[2],));db.commit()",path,tokens[0]],check=True,timeout=5)
        else:session._release_browser_token(tokens[0])
        route=await asyncio.wait_for(pending,1)
        route.fallback.assert_awaited_once();route.abort.assert_not_awaited()
        assert active(session)==limit
    finally:await session.close()
    assert active(session)==limit  # Session cleanup does not prove the browser has drained.


@pytest.mark.asyncio
async def test_session_close_keeps_live_browser_capacity_until_actual_request_end(tmp_path):
    session,context=managed_session(tmp_path)
    neighbor,neighbor_context=managed_session(tmp_path,account='b',proxy='q')
    try:
        await session.open_context(context);await neighbor.open_context(neighbor_context)
        routes=[await automatic_request(context) for _ in range(32)]
        await session.close()
        assert not context.closed and active(session)==32
        pending=asyncio.create_task(automatic_request(neighbor_context))
        await asyncio.sleep(.02)
        assert not pending.done() and active(session)==32
        # A slow or failed actual context close is not sufficient; only completion releases.
        session._release_browser_request(routes[0].request)
        (await asyncio.wait_for(pending,1)).fallback.assert_awaited_once()
        assert active(session)==32
        assert session._browser_pending_count==0
    finally:await session.close();await neighbor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop',['pause','cooldown','timeout','cancel','closed'])
async def test_pending_route_remains_fenced_and_does_not_consume_token(tmp_path,stop):
    path=tmp_path/'safety.db'
    session,context=managed_session(tmp_path,safety_db=path)
    safety_contract(path,session)
    try:
        await session.open_context(context)
        for _ in range(32):session._reserve_browser_request()
        if stop=='timeout':session._browser_capacity_wait_seconds=.04
        pending=asyncio.create_task(automatic_request(context))
        await asyncio.sleep(.01)
        assert not pending.done()
        if stop=='pause':
            import sqlite3
            with sqlite3.connect(path) as db:db.execute("UPDATE crawler_shared_exits SET pause_reason='ACCOUNT_CHALLENGE',paused_until=?",(time.time()+60,))
        elif stop=='cooldown':session.enter_cooldown('HTTP 429',60)
        elif stop=='cancel':pending.cancel()
        elif stop=='closed':await session.close()
        if stop=='cancel':
            with pytest.raises(asyncio.CancelledError):await pending
        else:
            route=await asyncio.wait_for(pending,1)
            route.abort.assert_awaited_once();route.fallback.assert_not_awaited()
        assert active(session)==32
        with session.budget._connect() as db:
            assert not db.execute('SELECT * FROM collection_route_health').fetchall()
    finally:await session.close()
