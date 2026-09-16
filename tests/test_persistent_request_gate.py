import asyncio
import json
import multiprocessing
import os
import sqlite3
import time
from pathlib import Path

import pytest
from tools.persistent_request_gate import PersistentRequestGate, RequestScheduler, configured_gate


def worker(db, ready, queue):
    gate = PersistentRequestGate(db, min_interval=.08, per_minute=1000, media_interval=0, max_concurrency=2)
    ready.wait(5)
    async def run():
        async with gate.slot('api'):
            queue.put(time.time())
    asyncio.run(run())


def test_cross_process_spacing_and_accounting(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    state = RequestScheduler(db, min_interval=.08, per_minute=1000, media_interval=0, max_concurrency=2)
    ctx = multiprocessing.get_context('spawn')
    barrier, queue = ctx.Barrier(3), ctx.Queue()
    processes = [ctx.Process(target=worker, args=(db, barrier, queue)) for _ in range(3)]
    try:
        for p in processes: p.start()
        for p in processes: p.join(12)
        assert [p.exitcode for p in processes] == [0, 0, 0]
        starts = sorted(queue.get(timeout=1) for _ in processes)
        assert all(b-a >= .065 for a,b in zip(starts,starts[1:]))
        assert state.snapshot()['request_count_last_minute'] == 3
        assert state.snapshot()['in_flight'] == 0
    finally:
        for p in processes:
            if p.is_alive():p.terminate();p.join()


def test_rolling_window_restart_cooldown_and_migration(tmp_path):
    now = [100.0]
    db = tmp_path / 'gate.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE request_gate (platform TEXT PRIMARY KEY,next_at REAL,times TEXT)')
        c.execute('INSERT INTO request_gate VALUES (?,?,?)', ('dy',102,'[99,100]'))
    args = dict(min_interval=2,per_minute=2,clock=lambda:now[0])
    state = RequestScheduler(db,**args)
    delay,_,_ = state.try_acquire()
    assert delay == 59
    state.enter_cooldown('HTTP 429',90)
    restored = RequestScheduler(db,**args)
    assert restored.try_acquire()[0] == 90
    assert restored.snapshot()['request_count_last_minute'] == 2
    now[0]=191
    assert restored.try_acquire()[0] == 0


@pytest.mark.asyncio
async def test_inflight_cancel_timeout_and_restart(tmp_path):
    args=dict(min_interval=0,per_minute=1000,media_interval=0,max_concurrency=1)
    gate = PersistentRequestGate(tmp_path/'gate.sqlite3',**args)
    entered=asyncio.Event()
    async def hold():
        async with gate.slot(timeout=3):
            entered.set();await asyncio.sleep(10)
    first=asyncio.create_task(hold());await entered.wait()
    waiting=asyncio.create_task(gate._reserve('api',lease_seconds=3))
    await asyncio.sleep(.03);assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):await waiting
    assert gate.state.snapshot()['request_count_last_minute']==1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):await first
    assert gate.state.snapshot()['in_flight']==0
    with pytest.raises(TimeoutError):
        async with gate.slot(timeout=.02):await asyncio.sleep(1)
    assert gate.state.snapshot()['in_flight']==0
    assert PersistentRequestGate(tmp_path/'gate.sqlite3',**args).state.snapshot()['request_count_last_minute']==2


def dying_worker(db):
    state=RequestScheduler(db,min_interval=0,per_minute=1000)
    assert state.try_acquire(lease_seconds=300)[1]
    os._exit(0)


def test_crashed_process_releases_capacity_not_history(tmp_path):
    db=tmp_path/'gate.sqlite3';ctx=multiprocessing.get_context('spawn')
    p=ctx.Process(target=dying_worker,args=(db,));p.start();p.join(10)
    assert p.exitcode==0
    state=RequestScheduler(db,min_interval=0,per_minute=1000)
    assert state.try_acquire(lease_seconds=10)[0]==0
    assert state.snapshot()['request_count_last_minute']==2


def test_expired_lease_and_changed_policy_preserve_budget(tmp_path):
    now=[100.0];state=RequestScheduler(tmp_path/'gate.sqlite3',min_interval=0,per_minute=1000,clock=lambda:now[0])
    state.try_acquire(lease_seconds=2)
    now[0]=103
    assert state.try_acquire(lease_seconds=2)[0]==0
    state.update_policy(min_interval=0,per_minute=1,max_concurrency=1,media_interval=5)
    assert state.try_acquire()[0]==60
    assert state.snapshot()['request_count_last_minute']==2


def test_configuration_is_required_and_conflicts_fail_closed(tmp_path,monkeypatch):
    monkeypatch.delenv('MEDIACRAWLER_REQUEST_SCHEDULER_DB',raising=False)
    monkeypatch.delenv('MEDIACRAWLER_CLOAK_PROFILE_ROOT',raising=False)
    with pytest.raises(ValueError,match='requires'):configured_gate()
    RequestScheduler(tmp_path/'gate.sqlite3')
    with pytest.raises(ValueError,match='policy differs'):RequestScheduler(tmp_path/'gate.sqlite3',min_interval=0)


@pytest.mark.asyncio
async def test_cooldown_wait_is_bounded_and_shared(tmp_path):
    gate=PersistentRequestGate(tmp_path/'gate.sqlite3')
    other=RequestScheduler(tmp_path/'gate.sqlite3');other.enter_cooldown('HTTP 429',300)
    with pytest.raises(RuntimeError,match='cooldown'):
        async with gate.slot(max_wait=.1):pytest.fail('must not dispatch')
    assert other.snapshot()['request_count_last_minute']==0
