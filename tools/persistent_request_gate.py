"""One SQLite scheduler for API, media and application-side cooldown state.

Transactions reserve starts and in-flight leases atomically across local processes.
No network I/O or sleeping is performed while a database transaction is open.
"""
from __future__ import annotations

import asyncio
import json
import math
import logging
import os
import random
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import uuid4


def jitter_factor(jitter, *, lengthen_only=False):
    """Random pacing multiplier in [1-jitter, 1+jitter], never below 0.5; 0 disables.

    lengthen_only keeps the multiplier in [1, 1+jitter] for limits that must never shrink.
    """
    jitter = float(jitter or 0)
    if not math.isfinite(jitter) or jitter <= 0:
        return 1.0
    return max(0.5, random.uniform(1.0 if lengthen_only else 1 - jitter, 1 + jitter))


class RequestScheduler:
    def __init__(self, db_path, *, platform='dy', min_interval=2.0, per_minute=30,
                 max_concurrency=1, media_interval=5.0, cooldown_seconds=300.0,
                 jitter=0.0, lengthen_only_jitter=False, replace_policy=False,
                 clock=time.time, sleeper=time.sleep):
        if not str(db_path).strip():
            raise ValueError('request scheduler database is required')
        numbers = (min_interval, per_minute, max_concurrency, media_interval, cooldown_seconds)
        if not all(math.isfinite(float(x)) for x in numbers) or min(min_interval, media_interval, cooldown_seconds) < 0 or per_minute < 1 or max_concurrency < 1:
            raise ValueError('invalid request scheduler limits')
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.platform = platform
        self.min_interval, self.per_minute = float(min_interval), int(per_minute)
        self.max_concurrency, self.media_interval = int(max_concurrency), float(media_interval)
        self.cooldown_seconds = float(cooldown_seconds)
        self.jitter = float(jitter)
        self.lengthen_only_jitter = bool(lengthen_only_jitter)
        self._clock, self._sleeper = clock, sleeper
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("""CREATE TABLE IF NOT EXISTS request_scheduler_state (
                platform TEXT PRIMARY KEY, next_allowed_at REAL NOT NULL DEFAULT 0,
                cooldown_until REAL NOT NULL DEFAULT 0, request_times_json TEXT NOT NULL DEFAULT '[]',
                last_reason TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL DEFAULT 0)""")
            c.execute('CREATE TABLE IF NOT EXISTS request_scheduler_channels (platform TEXT, channel TEXT, next_at REAL, last_start REAL NOT NULL DEFAULT 0, PRIMARY KEY(platform,channel))')
            if 'last_start' not in {row[1] for row in c.execute('PRAGMA table_info(request_scheduler_channels)')}:
                c.execute('ALTER TABLE request_scheduler_channels ADD COLUMN last_start REAL NOT NULL DEFAULT 0')
            c.execute('CREATE TABLE IF NOT EXISTS request_scheduler_leases (token TEXT PRIMARY KEY, platform TEXT, pid INTEGER, expires_at REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS request_scheduler_migrations (name TEXT PRIMARY KEY)')
            c.execute('CREATE TABLE IF NOT EXISTS request_scheduler_policy (platform TEXT PRIMARY KEY, limits_json TEXT NOT NULL)')
            limits = json.dumps([self.min_interval, self.per_minute, self.max_concurrency, self.media_interval])
            policy = c.execute('SELECT limits_json FROM request_scheduler_policy WHERE platform=?', (platform,)).fetchone()
            if replace_policy:
                # Per-account keys: the limits passed on this run always win.
                c.execute('INSERT OR REPLACE INTO request_scheduler_policy VALUES (?,?)', (platform, limits))
            elif policy and json.loads(policy[0]) != json.loads(limits):
                raise ValueError('shared request scheduler policy differs; update the policy explicitly without deleting its database')
            c.execute('INSERT OR IGNORE INTO request_scheduler_policy VALUES (?,?)', (platform, limits))
            # Import legacy pacing conservatively, once. Never erase an existing cooldown.
            legacy = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='request_gate'").fetchone()
            if legacy and not c.execute("SELECT 1 FROM request_scheduler_migrations WHERE name='request_gate_v1'").fetchone():
                for old_platform, next_at, times in c.execute('SELECT platform,next_at,times FROM request_gate').fetchall():
                    old = c.execute('SELECT next_allowed_at,request_times_json FROM request_scheduler_state WHERE platform=?', (old_platform,)).fetchone()
                    merged = self._times(times) + (self._times(old[1]) if old else [])
                    c.execute('''INSERT INTO request_scheduler_state(platform,next_allowed_at,request_times_json) VALUES(?,?,?)
                        ON CONFLICT(platform) DO UPDATE SET next_allowed_at=excluded.next_allowed_at, request_times_json=excluded.request_times_json''',
                        (old_platform, max(float(next_at), float(old[0]) if old else 0), json.dumps(sorted(merged))))
                c.execute("INSERT INTO request_scheduler_migrations VALUES ('request_gate_v1')")

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=0.2)

    @staticmethod
    def _times(value):
        # Corrupt state must fail closed, never silently replenish the budget.
        values = json.loads(value)
        if not isinstance(values, list) or not all(isinstance(x, (float, int)) and math.isfinite(x) for x in values):
            raise ValueError('invalid persisted request timestamps')
        return values

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def try_acquire(self, operation='api', *, lease_seconds=None):
        result = _try_reserve_scopes(self.db_path, [self.platform], operation,
            lease_seconds, self._clock(),
            {self.platform: (self.jitter, self.lengthen_only_jitter)})
        return result['delay'], result['scope_tokens'].get(self.platform), result['reason']

    def release(self, token):
        with self._connect() as c:
            c.execute('DELETE FROM request_scheduler_leases WHERE token=?', (token,))

    def acquire(self, operation='api'):
        waited = 0.0
        while True:
            delay, _, _ = self.try_acquire(operation)
            if not delay:
                return waited
            pause = min(delay, .25)
            self._sleeper(pause)
            waited += pause

    def enter_cooldown(self, reason, seconds=None):
        duration = self.cooldown_seconds if seconds is None else float(seconds)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError('invalid cooldown')
        now = float(self._clock())
        with self._connect() as c:
            c.execute('''INSERT INTO request_scheduler_state(platform,cooldown_until,last_reason,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(platform) DO UPDATE SET cooldown_until=MAX(cooldown_until,excluded.cooldown_until),last_reason=excluded.last_reason,updated_at=excluded.updated_at''',
                (self.platform, now + duration, str(reason), now))
        return now + duration

    def update_policy(self, *, min_interval, per_minute, max_concurrency, media_interval):
        values = [float(min_interval), int(per_minute), int(max_concurrency), float(media_interval)]
        if not all(math.isfinite(x) for x in values) or min(values[0], values[3]) < 0 or min(values[1:3]) < 1:
            raise ValueError('invalid request scheduler policy')
        with self._connect() as c:
            c.execute('UPDATE request_scheduler_policy SET limits_json=? WHERE platform=?', (json.dumps(values), self.platform))

    def policy(self):
        with self._connect() as c:
            return json.loads(c.execute('SELECT limits_json FROM request_scheduler_policy WHERE platform=?', (self.platform,)).fetchone()[0])

    def snapshot(self):
        return _scope_snapshot(self.db_path, self.platform, float(self._clock()))


class PersistentRequestGate:
    def __init__(self, db_path, **kwargs):
        self.state = RequestScheduler(db_path, **kwargs)

    async def _reserve(self, operation, *, lease_seconds=None, max_wait=300):
        start = time.monotonic()
        last_reason = None
        while True:
            try:
                delay, token, reason = self.state.try_acquire(operation, lease_seconds=lease_seconds)
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower():
                    raise
                delay, token, reason = .05, None, 'database_busy'
            if delay == 0:
                return token, time.monotonic() - start
            if reason != last_reason:
                logging.getLogger(__name__).info('REQUEST_SCHEDULER_WAIT platform=%s operation=%s reason=%s delay_seconds=%.3f', self.state.platform, operation, reason, delay)
                last_reason = reason
            if time.monotonic() - start + delay > max_wait:
                raise RuntimeError(f'REQUEST_SCHEDULER_WAIT_EXCEEDED: {reason}')
            # Bounded polling responds to cancellation, cooldown extension and policy changes.
            await asyncio.sleep(min(delay, .25))

    async def acquire(self, operation='api'):
        _, waited = await self._reserve(operation)
        return waited

    @asynccontextmanager
    async def slot(self, operation='api', *, timeout=60, max_wait=300):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('finite request timeout is required')
        token, waited = await self._reserve(operation, lease_seconds=timeout + 30, max_wait=max_wait)
        try:
            # A live request cannot outlive its lease: total timeout, not only per-read timeout.
            async with asyncio.timeout(timeout):
                yield waited
        finally:
            self.state.release(token)


def scheduler_db_path(db_path=''):
    path = str(db_path).strip() or os.getenv('MEDIACRAWLER_REQUEST_SCHEDULER_DB', '').strip()
    if not path:
        root = os.getenv('MEDIACRAWLER_CLOAK_PROFILE_ROOT', '').strip()
        if not root or not Path(root).is_absolute():
            raise ValueError('Douyin requires --request_scheduler_db or an absolute MEDIACRAWLER_CLOAK_PROFILE_ROOT')
        path = str(Path(root) / 'request_scheduler.sqlite3')
    return path


def configured_gate(*, db_path='', min_interval=2.0, per_minute=30,
                    max_concurrency=1, media_interval=5.0, cooldown_seconds=300, jitter=0.0):
    path = scheduler_db_path(db_path)
    def value(name, default, cast=float):
        return cast(os.getenv('MEDIACRAWLER_' + name, str(default)))
    return PersistentRequestGate(path, min_interval=value('REQUEST_MIN_INTERVAL', min_interval),
        per_minute=value('REQUESTS_PER_MINUTE', per_minute, int),
        max_concurrency=value('REQUEST_CONCURRENCY', max_concurrency, int),
        media_interval=value('MEDIA_REQUEST_INTERVAL', media_interval),
        cooldown_seconds=value('REQUEST_COOLDOWN_SECONDS', cooldown_seconds),
        # The platform key is the per-IP limit: jitter may only lengthen its interval.
        jitter=jitter, lengthen_only_jitter=True)


def _scope_ids(scope_ids):
    if isinstance(scope_ids, (str, bytes)):
        raise ValueError('scope_ids must be a sequence')
    values = list(scope_ids)
    if not values or any(not isinstance(scope, str) or not scope.strip() for scope in values):
        raise ValueError('nonempty scheduler scopes are required')
    return sorted(set(values))


def _try_reserve_scopes(db_path, scope_ids, operation, lease_seconds, now, jitter_options=None, safety_fence=None):
    scopes = _scope_ids(scope_ids)
    now = float(now)
    if not math.isfinite(now) or (lease_seconds is not None and
            (not math.isfinite(lease_seconds) or lease_seconds <= 0)):
        raise ValueError('invalid reservation time or lease')
    pending = []
    target, reason = now, 'ready'
    with sqlite3.connect(db_path, timeout=.2) as c:
        if safety_fence:
            c.execute('ATTACH DATABASE ? AS safety', (safety_fence['db_path'],))
        c.execute('BEGIN IMMEDIATE')
        if safety_fence:
            row = c.execute('SELECT generation,observed_at,paused_until,pause_reason FROM safety.crawler_shared_exits WHERE exit_key=?', (safety_fence['exit_ip'],)).fetchone()
            admission = c.execute('SELECT account_id,exit_key,generation,state FROM safety.crawler_route_admissions WHERE operation_id=?', (safety_fence['operation_id'],)).fetchone()
            account = c.execute('SELECT recovery_claim,recovery_state FROM safety.crawler_accounts WHERE id=?', (safety_fence['account_id'],)).fetchone()
            recovery = bool(account and safety_fence.get('recovery_claim') and account[0] == safety_fence['recovery_claim'] and account[1] == 'claimed')
            if (not row or row[0] != safety_fence['generation'] or (row[3] and not recovery)
                    or not 0 <= now - row[1] <= safety_fence['ttl']
                    or (row[2] is not None and row[2] > now)
                    or not admission or tuple(admission) != (safety_fence['account_id'], safety_fence['exit_ip'], safety_fence['generation'], 'active')):
                raise RuntimeError('ACCOUNT_CHALLENGE')
        for scope in scopes:
            policy_row = c.execute('SELECT limits_json FROM request_scheduler_policy WHERE platform=?', (scope,)).fetchone()
            try:
                policy = json.loads(policy_row[0]) if policy_row else None
                if (not isinstance(policy, list) or len(policy) != 4 or
                        not all(isinstance(x, (int, float)) and math.isfinite(x) for x in policy) or
                        min(policy[0], policy[3]) < 0 or min(policy[1:3]) < 1 or
                        any(int(policy[i]) != policy[i] for i in (1, 2))):
                    raise ValueError()
            except (ValueError, TypeError):
                raise RuntimeError('SCHEDULER_POLICY_CONFLICT') from None
            interval, per_minute, concurrency, media_interval = policy
            row = c.execute('SELECT next_allowed_at,cooldown_until,request_times_json FROM request_scheduler_state WHERE platform=?', (scope,)).fetchone()
            next_at, cooldown, times = (float(row[0]), float(row[1]), RequestScheduler._times(row[2])) if row else (0., 0., [])
            times = sorted(x for x in times if x > now - 60)
            jitter, lengthen_only = (jitter_options or {}).get(scope, (0., True))
            floor = max(.5, 1 - jitter) if jitter > 0 and not lengthen_only else 1.
            scope_target = max(now, next_at, cooldown, times[-1] + interval * floor if times else now)
            scope_reason = 'cooldown' if cooldown > now else 'interval'
            if len(times) >= per_minute:
                scope_target = max(scope_target, times[-int(per_minute)] + 60)
                scope_reason = 'rolling_window' if scope_target > max(next_at, cooldown) else scope_reason
            if operation in ('media', 'media_refresh'):
                channel = c.execute("SELECT next_at,last_start FROM request_scheduler_channels WHERE platform=? AND channel='media'", (scope,)).fetchone()
                if channel and max(channel[0], channel[1] + media_interval) > scope_target:
                    scope_target, scope_reason = max(channel[0], channel[1] + media_interval), 'media_interval'
            active = []
            for token, pid, expires in c.execute('SELECT token,pid,expires_at FROM request_scheduler_leases WHERE platform=?', (scope,)).fetchall():
                if not RequestScheduler._alive(pid):
                    c.execute('DELETE FROM request_scheduler_leases WHERE token=?', (token,))
                else:
                    # A live operation may hold a body/browser beyond its deadline;
                    # only explicit completion or process death releases capacity.
                    active.append(max(expires, now + .1))
            if lease_seconds is not None and len(active) >= concurrency:
                capacity_target = now + min(.1, max(0., min(active) - now))
                if capacity_target > scope_target:
                    scope_target, scope_reason = capacity_target, 'concurrency'
            if scope_target > target:
                target, reason = scope_target, scope_reason
            pending.append((scope, times, interval, media_interval, jitter, lengthen_only))
        if target > now:
            return dict(delay=target - now, reason=reason, scope_tokens={})
        tokens = {}
        for scope, times, interval, media_interval, jitter, lengthen_only in pending:
            times.append(now)
            c.execute('''INSERT INTO request_scheduler_state(platform,next_allowed_at,request_times_json,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(platform) DO UPDATE SET next_allowed_at=excluded.next_allowed_at,request_times_json=excluded.request_times_json,updated_at=excluded.updated_at''',
                (scope, now + interval * jitter_factor(jitter, lengthen_only=lengthen_only), json.dumps(times), now))
            if operation in ('media', 'media_refresh'):
                c.execute("INSERT INTO request_scheduler_channels(platform,channel,next_at,last_start) VALUES (?,'media',?,?) ON CONFLICT(platform,channel) DO UPDATE SET next_at=excluded.next_at,last_start=excluded.last_start", (scope, now + media_interval, now))
            if lease_seconds is not None:
                token = uuid4().hex
                c.execute('INSERT INTO request_scheduler_leases VALUES (?,?,?,?)', (token, scope, os.getpid(), now + lease_seconds))
                tokens[scope] = token
        return dict(delay=0., reason='ready', scope_tokens=tokens)


def try_reserve_scopes(db_path, scope_ids, operation='api', lease_seconds=None, now=None, safety_fence=None):
    """Reserve every registered scope in one transaction, or debit none.

    Business route wrappers must enforce their four required scopes; this generic
    scheduler deliberately also supports existing single-scope callers.
    """
    try:
        return _try_reserve_scopes(db_path, scope_ids, operation, lease_seconds,
                                   time.time() if now is None else now, safety_fence=safety_fence)
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc).lower():
            raise RuntimeError('SCHEDULER_POLICY_CONFLICT') from None
        raise


def release_scope_tokens(db_path, scope_tokens):
    with sqlite3.connect(db_path, timeout=.2) as c:
        c.executemany('DELETE FROM request_scheduler_leases WHERE platform=? AND token=?',
                      list(scope_tokens.items()))


def enter_cooldown_scopes(db_path, scope_ids, reason, seconds=300, now=None):
    scopes = _scope_ids(scope_ids)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('invalid cooldown')
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError('invalid cooldown time')
    with sqlite3.connect(db_path, timeout=.2) as c:
        c.execute('BEGIN IMMEDIATE')
        for scope in scopes:
            if not c.execute('SELECT 1 FROM request_scheduler_policy WHERE platform=?', (scope,)).fetchone():
                raise RuntimeError('SCHEDULER_POLICY_CONFLICT')
            c.execute('''INSERT INTO request_scheduler_state(platform,cooldown_until,last_reason,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(platform) DO UPDATE SET cooldown_until=MAX(cooldown_until,excluded.cooldown_until),last_reason=excluded.last_reason,updated_at=excluded.updated_at''',
                (scope, now + seconds, str(reason), now))
    return now + seconds


def _check_stop(stop_checker):
    if stop_checker and stop_checker():
        raise RuntimeError('REQUEST_SCHEDULER_STOPPED')


def _reservation(db_path, scope_ids, operation, lease_seconds, safety_fence=None):
    try:
        return try_reserve_scopes(db_path, scope_ids, operation, lease_seconds, safety_fence=safety_fence)
    except sqlite3.OperationalError as exc:
        if 'locked' not in str(exc).lower():
            raise
        return dict(delay=.05, reason='database_busy', scope_tokens={})


def reserve_scopes(db_path, scope_ids, operation='api', *, lease_seconds=None,
                   max_wait=300, stop_checker=None):
    """Synchronous wait adapter; returns (scope_tokens, elapsed_seconds)."""
    scopes = _scope_ids(scope_ids)
    start = time.monotonic()
    while True:
        _check_stop(stop_checker)
        result = _reservation(db_path, scopes, operation, lease_seconds)
        if result['delay'] == 0:
            return result['scope_tokens'], time.monotonic() - start
        if time.monotonic() - start + result['delay'] > max_wait:
            raise RuntimeError(f"REQUEST_SCHEDULER_WAIT_EXCEEDED: {result['reason']}")
        time.sleep(min(result['delay'], .25))


@contextmanager
def slot_scopes(db_path, scope_ids, operation='api', *, timeout=60, max_wait=300,
                stop_checker=None):
    """Sync caller must enforce its network's total timeout <= timeout."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('finite request timeout is required')
    tokens, waited = reserve_scopes(db_path, scope_ids, operation,
        lease_seconds=timeout + 30, max_wait=max_wait, stop_checker=stop_checker)
    try:
        yield waited
    finally:
        release_scope_tokens(db_path, tokens)


class MultiScopeGate:
    """Async adapter over pre-registered policies, compatible with gate callers."""
    def __init__(self, db_path, scope_ids, *, stop_checker=None, cooldown_seconds=300, safety_fence=None):
        self.db_path = db_path
        self.scope_ids = _scope_ids(scope_ids)
        self.safety_fence = safety_fence
        self.stop_checker = stop_checker
        self.cooldown_seconds = cooldown_seconds
        self.state = self
        self.platform = 'dy' if 'dy' in self.scope_ids else self.scope_ids[0]

    async def reserve_scopes(self, operation='api', *, lease_seconds=None, max_wait=300):
        start = time.monotonic()
        while True:
            _check_stop(self.stop_checker)
            result = _reservation(self.db_path, self.scope_ids, operation, lease_seconds, self.safety_fence)
            if result['delay'] == 0:
                return result['scope_tokens'], time.monotonic() - start
            if time.monotonic() - start + result['delay'] > max_wait:
                raise RuntimeError(f"REQUEST_SCHEDULER_WAIT_EXCEEDED: {result['reason']}")
            await asyncio.sleep(min(result['delay'], .25))

    async def acquire(self, operation='api'):
        _, waited = await self.reserve_scopes(operation)
        return waited

    @asynccontextmanager
    async def slot_scopes(self, operation='api', *, timeout=60, max_wait=300):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('finite request timeout is required')
        tokens, waited = await self.reserve_scopes(operation,
            lease_seconds=timeout + 30, max_wait=max_wait)
        try:
            async with asyncio.timeout(timeout):
                yield waited
        finally:
            release_scope_tokens(self.db_path, tokens)

    def slot(self, operation='api', *, timeout=60, max_wait=300):
        return self.slot_scopes(operation, timeout=timeout, max_wait=max_wait)

    def snapshot(self):
        return _scope_snapshot(self.db_path, self.platform, time.time())

    def snapshots(self):
        now = time.time()
        return {scope: _scope_snapshot(self.db_path, scope, now) for scope in self.scope_ids}

    def enter_cooldown(self, reason, seconds=None):
        return enter_cooldown_scopes(self.db_path, self.scope_ids, reason,
            self.cooldown_seconds if seconds is None else seconds)


def _scope_snapshot(db_path, scope, now):
    with sqlite3.connect(db_path, timeout=.2) as c:
        row = c.execute('SELECT next_allowed_at,cooldown_until,request_times_json,last_reason FROM request_scheduler_state WHERE platform=?', (scope,)).fetchone()
        active = c.execute('SELECT pid,expires_at FROM request_scheduler_leases WHERE platform=?', (scope,)).fetchall()
    return dict(platform=scope, next_allowed_at=row[0] if row else 0,
                cooldown_until=row[1] if row else 0, last_reason=row[3] if row else '',
                request_count_last_minute=sum(x > now - 60 for x in RequestScheduler._times(row[2])) if row else 0,
                in_flight=sum(RequestScheduler._alive(pid) for pid, expires in active))
