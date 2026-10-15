import asyncio
import multiprocessing
import sqlite3
import time

import pytest

from tools.persistent_request_gate import (
    MultiScopeGate, RequestScheduler, enter_cooldown_scopes,
    release_scope_tokens, slot_scopes, try_reserve_scopes,
)

SCOPES = ['dy', 'dy:account:a', 'dy:proxy:p', 'dy:exit:192.0.2.1']


def register(db, scopes=SCOPES, **kwargs):
    return [RequestScheduler(db, platform=s, min_interval=kwargs.get('interval', 0),
        per_minute=kwargs.get('quota', 1000), media_interval=kwargs.get('media', 0),
        max_concurrency=1, clock=lambda: kwargs.get('now', 100)) for s in scopes]


def counts(db):
    with sqlite3.connect(db) as c:
        return (c.execute('SELECT platform,request_times_json FROM request_scheduler_state ORDER BY platform').fetchall(),
                c.execute('SELECT token,platform FROM request_scheduler_leases ORDER BY token').fetchall())


@pytest.mark.parametrize('blocked', SCOPES)
def test_no_partial_debit_or_capacity_and_max_delay(tmp_path, blocked):
    db = tmp_path / 'gate.sqlite3'
    states = register(db)
    enter_cooldown_scopes(db, [blocked], 'HTTP 429', 25, now=100)
    before = counts(db)
    result = try_reserve_scopes(db, SCOPES + [SCOPES[0]], 'api', 10, now=100)
    assert result == dict(delay=25, reason='cooldown', scope_tokens={})
    assert counts(db) == before
    success = try_reserve_scopes(db, SCOPES, 'api', 10, now=126)
    assert set(success['scope_tokens']) == set(SCOPES)
    assert len(set(success['scope_tokens'].values())) == 4
    release_scope_tokens(db, success['scope_tokens'])
    assert all(s.snapshot()['in_flight'] == 0 for s in states)
    assert all(s.snapshot()['request_count_last_minute'] == 1 for s in states)


def test_one_capacity_block_never_consumes_other_scopes(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    register(db)
    held = try_reserve_scopes(db, [SCOPES[2]], 'api', 10, now=100)
    before = counts(db)
    result = try_reserve_scopes(db, SCOPES, 'api', 10, now=100)
    assert result['reason'] == 'concurrency'
    assert result['scope_tokens'] == {}
    assert counts(db) == before
    release_scope_tokens(db, held['scope_tokens'])
    assert try_reserve_scopes(db, SCOPES, 'api', 10, now=100)['delay'] == 0


def test_policy_unknown_and_conflict_preserve_history(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    register(db)
    before = counts(db)
    with pytest.raises(RuntimeError, match='SCHEDULER_POLICY_CONFLICT'):
        try_reserve_scopes(db, SCOPES + ['dy:proxy:unknown'], now=100)
    with pytest.raises(ValueError, match='policy differs'):
        RequestScheduler(db, platform='dy', min_interval=1)
    assert counts(db) == before


def test_rolling_window_media_and_shared_exit(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    states = register(db, quota=2, media=5)
    other = ['dy', 'dy:account:b', 'dy:proxy:q', SCOPES[3]]
    register(db, other, quota=2, media=5)
    assert try_reserve_scopes(db, SCOPES, 'media', now=100)['delay'] == 0
    before = counts(db)
    assert try_reserve_scopes(db, other, 'media', now=101)['delay'] == 4
    assert counts(db) == before
    assert try_reserve_scopes(db, other, 'api', now=101)['delay'] == 0
    assert try_reserve_scopes(db, SCOPES, 'api', now=102)['delay'] == 58
    enter_cooldown_scopes(db, ['dy', SCOPES[2], SCOPES[3]], 'HTTP 429', 90, now=102)
    assert try_reserve_scopes(db, other, now=102)['delay'] == 90
    assert states[2].snapshot()['last_reason'] == 'HTTP 429'


@pytest.mark.asyncio
async def test_async_cancel_timeout_stop_and_release_all(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    register(db, now=time.time())
    gate = MultiScopeGate(db, SCOPES)
    entered = asyncio.Event()
    async def hold():
        async with gate.slot(timeout=3):
            entered.set()
            await asyncio.sleep(10)
    task = asyncio.create_task(hold())
    await entered.wait()
    before = counts(db)
    waiter = asyncio.create_task(gate.reserve_scopes(lease_seconds=3))
    await asyncio.sleep(.03)
    started = time.monotonic()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert time.monotonic() - started < 1
    assert counts(db) == before
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not counts(db)[1]
    with pytest.raises(TimeoutError):
        async with gate.slot(timeout=.02):
            await asyncio.sleep(1)
    assert not counts(db)[1]
    assert all('[' in row[1] for row in counts(db)[0])
    stopped = MultiScopeGate(db, SCOPES, stop_checker=lambda: True)
    before = counts(db)
    with pytest.raises(RuntimeError, match='STOPPED'):
        await stopped.acquire()
    assert counts(db) == before


def test_sync_slot_exception_releases_without_refund(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    states = register(db, now=time.time())
    with pytest.raises(RuntimeError, match='network'):
        with slot_scopes(db, SCOPES):
            raise RuntimeError('network')
    assert all(s.snapshot()['in_flight'] == 0 for s in states)
    assert all(s.snapshot()['request_count_last_minute'] == 1 for s in states)


def process_worker(db, barrier, queue):
    barrier.wait(5)
    for _ in range(2):
        with slot_scopes(db, SCOPES, max_wait=10):
            queue.put(time.time())
            time.sleep(.02)


def test_three_processes_two_requests_bounded_progress(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    states = register(db, interval=.1, now=time.time())
    ctx = multiprocessing.get_context('spawn')
    barrier, queue = ctx.Barrier(3), ctx.Queue()
    processes = [ctx.Process(target=process_worker, args=(db, barrier, queue)) for _ in range(3)]
    deadline = time.monotonic() + 15
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(max(0, deadline - time.monotonic()))
        assert [p.exitcode for p in processes] == [0, 0, 0]
        starts = sorted(queue.get(timeout=1) for _ in range(6))
        assert all(b - a >= .095 for a, b in zip(starts, starts[1:]))
        assert all(s.snapshot()['request_count_last_minute'] == 6 for s in states)
        assert not counts(db)[1]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()


def virtual_worker(db, barrier, clock, queue):
    barrier.wait(5)
    for _ in range(2):
        while True:
            with clock.get_lock():
                now = clock.value
                result = try_reserve_scopes(db, SCOPES, 'api', 10, now=now)
                if result['delay']:
                    clock.value += result['delay']
                    continue
                queue.put(now)
                clock.value += .02
                release_scope_tokens(db, result['scope_tokens'])
                break


def test_virtual_three_processes_two_requests_bounded_progress(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    states = register(db, interval=.1)
    ctx = multiprocessing.get_context('spawn')
    barrier, queue, clock = ctx.Barrier(3), ctx.Queue(), ctx.Value('d', 100.)
    processes = [ctx.Process(target=virtual_worker, args=(db, barrier, clock, queue)) for _ in range(3)]
    deadline = time.monotonic() + 10
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(max(0, deadline - time.monotonic()))
        assert [p.exitcode for p in processes] == [0, 0, 0]
        starts = sorted(queue.get(timeout=1) for _ in range(6))
        assert all(b - a >= .099 for a, b in zip(starts, starts[1:]))
        assert all(s.snapshot()['request_count_last_minute'] == 6 for s in states)
        assert not counts(db)[1]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()


def test_sync_stop_wait_is_bounded(tmp_path):
    import threading
    from tools.persistent_request_gate import reserve_scopes
    db = tmp_path / 'gate.sqlite3'
    register(db, now=time.time())
    enter_cooldown_scopes(db, [SCOPES[2]], 'maintenance', 10)
    before = counts(db)
    stop = threading.Event()
    timer = threading.Timer(.05, stop.set)
    timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='STOPPED'):
            reserve_scopes(db, SCOPES, max_wait=20, stop_checker=stop.is_set)
        assert time.monotonic() - start < 1
        assert counts(db) == before
    finally:
        timer.cancel()
        timer.join()


def test_multi_scope_snapshot_and_unregistered_database(tmp_path):
    db = tmp_path / 'gate.sqlite3'
    with pytest.raises(RuntimeError, match='SCHEDULER_POLICY_CONFLICT'):
        try_reserve_scopes(db, SCOPES, now=100)
    register(db, now=time.time())
    gate = MultiScopeGate(db, SCOPES)
    result = try_reserve_scopes(db, SCOPES, lease_seconds=3)
    assert gate.snapshot()['in_flight'] == 1
    assert set(gate.snapshots()) == set(SCOPES)
    assert all(s['request_count_last_minute'] == 1 for s in gate.snapshots().values())
    release_scope_tokens(db, result['scope_tokens'])
