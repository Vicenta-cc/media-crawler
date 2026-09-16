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
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4


class RequestScheduler:
    def __init__(self, db_path, *, platform='dy', min_interval=2.0, per_minute=30,
                 max_concurrency=1, media_interval=5.0, cooldown_seconds=300.0,
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
            if policy and json.loads(policy[0]) != json.loads(limits):
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
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            # Check the policy on every reservation: existing processes must not
            # continue with old limits after an explicit operator policy change.
            policy = json.loads(c.execute('SELECT limits_json FROM request_scheduler_policy WHERE platform=?', (self.platform,)).fetchone()[0])
            interval, per_minute, concurrency, media_interval = policy
            now = float(self._clock())
            row = c.execute('SELECT next_allowed_at,cooldown_until,request_times_json FROM request_scheduler_state WHERE platform=?', (self.platform,)).fetchone()
            next_at, cooldown, times = (float(row[0]), float(row[1]), self._times(row[2])) if row else (0.0, 0.0, [])
            times = sorted(x for x in times if x > now - 60)
            target = max(now, next_at, cooldown, times[-1] + interval if times else now)
            reason = 'cooldown' if cooldown > now else 'interval'
            if len(times) >= per_minute:
                target = max(target, times[-int(per_minute)] + 60)
                reason = 'rolling_window' if target > max(next_at, cooldown) else reason
            if operation in ('media', 'media_refresh'):
                channel = c.execute("SELECT next_at,last_start FROM request_scheduler_channels WHERE platform=? AND channel='media'", (self.platform,)).fetchone()
                if channel and max(channel[0], channel[1] + media_interval) > target:
                    target, reason = max(channel[0], channel[1] + media_interval), 'media_interval'
            leases = c.execute('SELECT token,pid,expires_at FROM request_scheduler_leases WHERE platform=?', (self.platform,)).fetchall()
            active = []
            for token, pid, expires in leases:
                if expires <= now or not self._alive(pid):
                    c.execute('DELETE FROM request_scheduler_leases WHERE token=?', (token,))
                else:
                    active.append(expires)
            if lease_seconds is not None and len(active) >= concurrency:
                target, reason = max(target, now + min(.1, max(0, min(active) - now))), 'concurrency'
            delay = target - now
            if delay > 0:
                return delay, None, reason
            token = uuid4().hex if lease_seconds is not None else None
            times.append(now)
            c.execute('''INSERT INTO request_scheduler_state(platform,next_allowed_at,request_times_json,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(platform) DO UPDATE SET next_allowed_at=excluded.next_allowed_at,request_times_json=excluded.request_times_json,updated_at=excluded.updated_at''',
                (self.platform, now + interval, json.dumps(times), now))
            if operation in ('media', 'media_refresh'):
                c.execute("INSERT INTO request_scheduler_channels(platform,channel,next_at,last_start) VALUES (?,'media',?,?) ON CONFLICT(platform,channel) DO UPDATE SET next_at=excluded.next_at,last_start=excluded.last_start", (self.platform, now + media_interval, now))
            if token:
                c.execute('INSERT INTO request_scheduler_leases VALUES (?,?,?,?)', (token, self.platform, os.getpid(), now + lease_seconds))
            return 0.0, token, 'ready'

    def release(self, token):
        with self._connect() as c:
            c.execute('DELETE FROM request_scheduler_leases WHERE token=?', (token,))

    def acquire(self, operation='api'):
        waited = 0.0
        while True:
            delay, _, _ = self.try_acquire(operation)
            if not delay:
                return waited
            self._sleeper(delay)
            waited += delay

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

    def snapshot(self):
        with self._connect() as c:
            row = c.execute('SELECT next_allowed_at,cooldown_until,request_times_json,last_reason FROM request_scheduler_state WHERE platform=?', (self.platform,)).fetchone()
            active = c.execute('SELECT pid,expires_at FROM request_scheduler_leases WHERE platform=?', (self.platform,)).fetchall()
        now = float(self._clock())
        return dict(platform=self.platform, next_allowed_at=row[0] if row else 0,
                    cooldown_until=row[1] if row else 0, last_reason=row[3] if row else '',
                    request_count_last_minute=sum(x > now - 60 for x in self._times(row[2])) if row else 0,
                    in_flight=sum(expires > now and self._alive(pid) for pid, expires in active))


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

    async def acquire(self):
        _, waited = await self._reserve('api')
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


def configured_gate(*, db_path='', min_interval=2.0, per_minute=30,
                    max_concurrency=1, media_interval=5.0, cooldown_seconds=300):
    path = str(db_path).strip() or os.getenv('MEDIACRAWLER_REQUEST_SCHEDULER_DB', '').strip()
    if not path:
        root = os.getenv('MEDIACRAWLER_CLOAK_PROFILE_ROOT', '').strip()
        if not root or not Path(root).is_absolute():
            raise ValueError('Douyin requires --request_scheduler_db or an absolute MEDIACRAWLER_CLOAK_PROFILE_ROOT')
        path = str(Path(root) / 'request_scheduler.sqlite3')
    def value(name, default, cast=float):
        return cast(os.getenv('MEDIACRAWLER_' + name, str(default)))
    return PersistentRequestGate(path, min_interval=value('REQUEST_MIN_INTERVAL', min_interval),
        per_minute=value('REQUESTS_PER_MINUTE', per_minute, int),
        max_concurrency=value('REQUEST_CONCURRENCY', max_concurrency, int),
        media_interval=value('MEDIA_REQUEST_INTERVAL', media_interval),
        cooldown_seconds=value('REQUEST_COOLDOWN_SECONDS', cooldown_seconds))
