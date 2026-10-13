import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_bound_proxy_safety_regressions import managed_session, automatic_request
from tools import bound_proxy


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
async def test_capacity_stop_reports_first_cause_without_transport_health(tmp_path,monkeypatch):
    session,context=managed_session(tmp_path)
    events=[]
    monkeypatch.setattr(bound_proxy,'emit_browser_guard_stopped',lambda snapshot,reason:events.append(reason))
    try:
        await session.open_context(context)
        for _ in range(32):session._reserve_browser_request()
        stopped=await automatic_request(context)
        stopped.abort.assert_awaited_once()
        session._stop_background('PAGE_SIZES_FAILED')
        assert events==['SHARED_ACTIVE_LIMIT']
        with session.budget._connect() as db:
            assert not db.execute('SELECT * FROM collection_route_health').fetchall()
    finally:await session.close()


@pytest.mark.asyncio
async def test_actual_page_sizes_failure_reports_before_close_and_latches(tmp_path,monkeypatch):
    session,context=managed_session(tmp_path)
    events=[];callbacks={}
    monkeypatch.setattr(bound_proxy,'emit_browser_guard_stopped',lambda snapshot,reason:events.append(reason))
    page=SimpleNamespace(on=lambda event,callback:callbacks.update({event:callback}),close=AsyncMock(),is_closed=lambda:False)
    context.pages.append(page);session.context=context
    try:
        session._monitor_page(page)
        request=SimpleNamespace(sizes=AsyncMock(side_effect=RuntimeError('https://secret.invalid/password')))
        callbacks['requestfinished'](request)
        await asyncio.sleep(.01)
        session._stop_background('PAGE_BYTE_LIMIT')
        assert events==['PAGE_SIZES_FAILED'] and page.close.await_count>=1
        with session.budget._connect() as db:
            assert not db.execute('SELECT * FROM collection_route_health').fetchall()
    finally:await session.close()
