import asyncio
import multiprocessing
import os
import sqlite3

import pytest

from tools.persistent_request_gate import RequestScheduler
from tools.trial_limits import SharedTrialLimits, forbid_rotation, from_env


@pytest.fixture
def limits(tmp_path):
    db = tmp_path / 'scheduler.sqlite3'
    RequestScheduler(db)
    now = [100.]
    return SharedTrialLimits(db, 'trial-fixture', clock=lambda: now[0]), now


def test_optional_env_and_invalid_ids(tmp_path):
    assert from_env(tmp_path / 'no-file', env={}) is None
    for trial_id in (' ', '../outside', 'x:secret', 'x' * 65):
        with pytest.raises(ValueError, match='TRIAL_LIMITS_INVALID'):
            from_env(tmp_path / 'no-file', env={'MEDIACRAWLER_PROXY_TRIAL_ID': trial_id})
    with pytest.raises(ValueError, match='TRIAL_SCHEDULER_REQUIRED'):
        SharedTrialLimits(tmp_path / 'no-file', 'trial')
    assert not (tmp_path / 'no-file').exists()


def test_operations_persist_and_are_not_refunded(limits):
    limiter, now = limits
    assert [limiter.reserve_operation() for _ in range(40)] == list(range(1, 41))
    restored = SharedTrialLimits(limiter.db_path, limiter.trial_id, clock=lambda: now[0])
    with pytest.raises(RuntimeError, match='PROBE_BUDGET_EXHAUSTED'):
        restored.reserve_operation()
    assert restored.snapshot()['operations'] == 40
    separate = SharedTrialLimits(limiter.db_path, 'other-trial')
    assert separate.snapshot()['operations'] == 0


def test_media_counts_failed_download_and_byte_reservation(limits):
    limiter, now = limits
    token = limiter.begin_media()
    assert limiter.reserve_media_bytes(token, 10) == 10
    assert limiter.media_timeout(token) == 300
    with pytest.raises(RuntimeError):
        limiter.begin_media()
    now[0] += 3
    assert limiter.finish_media(token) == 3
    assert limiter.finish_media(token) == 3
    second = limiter.begin_media()
    assert limiter.media_timeout(second) == 297
    limiter.reserve_media_bytes(second, limiter.MAX_MEDIA_BYTES - 10)
    with pytest.raises(RuntimeError, match='PROBE_BUDGET_EXHAUSTED'):
        limiter.reserve_media_bytes(second, 1)
    assert limiter.snapshot()['media_bytes'] == limiter.MAX_MEDIA_BYTES
    limiter.finish_media(second)
    with pytest.raises(RuntimeError):
        limiter.begin_media()
    assert limiter.snapshot()['media_files'] == 2
    assert limiter.snapshot()['media_seconds'] == 3


def test_media_aggregate_elapsed_deadline(limits):
    limiter, now = limits
    first = limiter.begin_media()
    now[0] += 120
    limiter.finish_media(first)
    second = limiter.begin_media()
    assert limiter.media_timeout(second) == 180
    now[0] += 179
    assert limiter.media_timeout(second) == 1
    now[0] += 1
    with pytest.raises(RuntimeError):
        limiter.media_timeout(second)
    with pytest.raises(RuntimeError):
        limiter.reserve_media_bytes(second, 0)
    limiter.finish_media(second)
    assert limiter.snapshot()['media_seconds'] == 300
    with pytest.raises(RuntimeError):
        limiter.begin_media()


def test_clock_backward_unknown_tokens_and_negative_fail_closed(limits):
    limiter, now = limits
    token = limiter.begin_media()
    for value in (-1, True, 1.2):
        with pytest.raises(ValueError):
            limiter.reserve_media_bytes(token, value)
    other = SharedTrialLimits(limiter.db_path, 'another-trial', clock=lambda: now[0])
    for bad in ('', 'unknown', 'a' * 32, None):
        with pytest.raises(RuntimeError):
            limiter.finish_media(bad)
    with pytest.raises(RuntimeError):
        other.reserve_media_bytes(token, 1)
    now[0] = 99
    with pytest.raises(RuntimeError):
        limiter.media_timeout(token)
    with pytest.raises(RuntimeError):
        limiter.finish_media(token)
    now[0] = 100
    assert limiter.snapshot()['media_files'] == 1 and limiter.snapshot()['media_bytes'] == 0
    limiter.finish_media(token)


@pytest.mark.asyncio
async def test_cancel_finally_keeps_spent_bytes_file_and_elapsed(limits):
    limiter, now = limits
    started = asyncio.Event()
    async def media():
        token = limiter.begin_media()
        try:
            limiter.reserve_media_bytes(token, 100)
            started.set()
            await asyncio.sleep(20)
        finally:
            limiter.finish_media(token)
    task = asyncio.create_task(media())
    await started.wait()
    now[0] += 7
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = limiter.snapshot()
    assert (snapshot['media_files'], snapshot['media_bytes'], snapshot['media_seconds'], snapshot['active_media']) == (1, 100, 7, 0)


def consume_operations(db, barrier, queue):
    limiter = SharedTrialLimits(db, 'shared')
    barrier.wait(5)
    successes = 0
    for _ in range(20):
        try:
            limiter.reserve_operation()
            successes += 1
        except RuntimeError:
            pass
    queue.put(successes)


def test_multi_process_counter_never_exceeds_40(tmp_path):
    db = tmp_path / 'scheduler.sqlite3'
    RequestScheduler(db)
    ctx = multiprocessing.get_context('spawn')
    barrier, queue = ctx.Barrier(3), ctx.Queue()
    processes = [ctx.Process(target=consume_operations, args=(db, barrier, queue)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
        assert [p.exitcode for p in processes] == [0, 0, 0]
        assert sum(queue.get(timeout=1) for _ in processes) == 40
        assert SharedTrialLimits(db, 'shared').snapshot()['operations'] == 40
        assert RequestScheduler(db).snapshot()['request_count_last_minute'] == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()


def test_corrupt_counter_and_abandoned_media_fail_closed(limits):
    limiter, now = limits
    token = limiter.begin_media()
    restarted = SharedTrialLimits(limiter.db_path, limiter.trial_id, clock=lambda: now[0])
    with pytest.raises(RuntimeError):
        restarted.begin_media()
    with sqlite3.connect(limiter.db_path) as c:
        c.execute('UPDATE proxy_trial_limits SET media_bytes=-1')
    with pytest.raises(RuntimeError):
        restarted.reserve_operation()


def test_rotation_and_auto_tasks_must_explicitly_be_off():
    for enabled in (True, None, 0, ''):
        with pytest.raises(RuntimeError):
            forbid_rotation('trial', enabled=enabled)
    forbid_rotation('trial', enabled=False)
    forbid_rotation('', enabled=True)


@pytest.mark.parametrize('cause', ['operations', 'bytes', 'time', 'files'])
def test_exhaustion_stops_every_later_operation_across_instances(limits, cause):
    limiter, now = limits
    if cause == 'operations':
        for _ in range(40):
            limiter.reserve_operation()
        fail = limiter.reserve_operation
    elif cause == 'bytes':
        token = limiter.begin_media()
        limiter.reserve_media_bytes(token, 3)
        fail = lambda: limiter.reserve_media_bytes(token, limiter.MAX_MEDIA_BYTES)
    elif cause == 'time':
        token = limiter.begin_media()
        now[0] += 300
        fail = lambda: limiter.media_timeout(token)
    else:
        for _ in range(2):
            limiter.finish_media(limiter.begin_media())
        fail = limiter.begin_media
    with pytest.raises(RuntimeError):
        fail()
    restarted = SharedTrialLimits(limiter.db_path, limiter.trial_id, clock=lambda: now[0])
    assert restarted.snapshot()['stopped']
    with pytest.raises(RuntimeError):
        restarted.reserve_operation()
    with pytest.raises(RuntimeError):
        restarted.begin_media()
    if cause in ('bytes', 'time'):
        limiter.finish_media(token)
        assert restarted.snapshot()['active_media'] == 0
        with pytest.raises(RuntimeError):
            restarted.reserve_media_bytes(token, 0)
