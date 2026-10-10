"""Optional trial-only process-shared limits; not a normal crawler policy.

Accepted media bytes are bounded before save; transport buffering can already
have received a final chunk. This is not a claim about a hard wire-byte limit.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

ERROR_CODE = 'PROBE_BUDGET_EXHAUSTED'
TRIAL_ID_ENV = 'MEDIACRAWLER_PROXY_TRIAL_ID'


def _exhausted():
    # Existing route code also represents the general finite trial stop gate.
    raise RuntimeError(ERROR_CODE)


def _trial_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
        raise ValueError('TRIAL_LIMITS_INVALID')
    return value


def forbid_rotation(trial_id, *, enabled):
    """Consumer must call before starting a trial with automatic rotation/tasks."""
    if trial_id:
        _trial_id(trial_id)
        if enabled is not False:
            _exhausted()


class SharedTrialLimits:
    MAX_OPERATIONS = 40
    MAX_MEDIA_FILES = 2
    MAX_MEDIA_BYTES = 20 * 1024 * 1024
    MAX_MEDIA_SECONDS = 300.

    def __init__(self, db_path, trial_id, *, clock=time.time):
        self.trial_id = _trial_id(trial_id)
        if not str(db_path).strip():
            raise ValueError('TRIAL_LIMITS_INVALID')
        self.db_path = Path(db_path).expanduser().resolve()
        # Trial ledger must augment the existing scheduler, never create a new budget island.
        if not self.db_path.is_file():
            raise ValueError('TRIAL_SCHEDULER_REQUIRED')
        self.clock = clock
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute("SELECT 1 FROM request_scheduler_policy WHERE platform='dy'").fetchone():
                raise ValueError('TRIAL_SCHEDULER_REQUIRED')
            c.execute('''CREATE TABLE IF NOT EXISTS proxy_trial_limits (
                trial_id TEXT PRIMARY KEY, operations INTEGER NOT NULL DEFAULT 0,
                media_files INTEGER NOT NULL DEFAULT 0, media_bytes INTEGER NOT NULL DEFAULT 0,
                media_seconds REAL NOT NULL DEFAULT 0, stopped INTEGER NOT NULL DEFAULT 0)''')
            c.execute('''CREATE TABLE IF NOT EXISTS proxy_trial_media (
                token TEXT PRIMARY KEY, trial_id TEXT NOT NULL, started_at REAL NOT NULL,
                pid INTEGER NOT NULL, finished_at REAL, duration REAL, bytes INTEGER NOT NULL DEFAULT 0)''')
            if 'stopped' not in {row[1] for row in c.execute('PRAGMA table_info(proxy_trial_limits)')}:
                c.execute('ALTER TABLE proxy_trial_limits ADD COLUMN stopped INTEGER NOT NULL DEFAULT 0')
            c.execute('INSERT OR IGNORE INTO proxy_trial_limits(trial_id) VALUES(?)', (self.trial_id,))

    @classmethod
    def from_env(cls, snapshot_scheduler_db, *, env=None, clock=time.time):
        env = os.environ if env is None else env
        trial_id = env.get(TRIAL_ID_ENV, '')
        if trial_id == '':
            return None
        return cls(snapshot_scheduler_db, trial_id, clock=clock)

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=.2)

    def _now(self):
        now = float(self.clock())
        if not math.isfinite(now) or now < 0:
            raise ValueError('TRIAL_LIMITS_INVALID')
        return now

    def _stop(self, c):
        c.execute('UPDATE proxy_trial_limits SET stopped=1 WHERE trial_id=?', (self.trial_id,))
        # Persist the stop before raising: the context manager would otherwise
        # roll it back alongside the rejected debit.
        c.commit()
        _exhausted()

    def _row(self, c, *, allow_stopped=False):
        stopped = c.execute('SELECT stopped FROM proxy_trial_limits WHERE trial_id=?', (self.trial_id,)).fetchone()
        if not stopped or stopped[0] not in (0, 1) or stopped[0] and not allow_stopped:
            _exhausted()
        row = c.execute('SELECT operations,media_files,media_bytes,media_seconds FROM proxy_trial_limits WHERE trial_id=?', (self.trial_id,)).fetchone()
        if not row or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in row):
            _exhausted()
        return row

    def reserve_operation(self):
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            operations, _, _, _ = self._row(c)
            if operations >= self.MAX_OPERATIONS:
                self._stop(c)
            c.execute('UPDATE proxy_trial_limits SET operations=operations+1 WHERE trial_id=?', (self.trial_id,))
            return operations + 1

    def begin_media(self):
        """Consumes file count even when download subsequently fails; no refund."""
        now = self._now()
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            _, files, size, seconds = self._row(c)
            active = c.execute('SELECT 1 FROM proxy_trial_media WHERE trial_id=? AND finished_at IS NULL', (self.trial_id,)).fetchone()
            # One active media timer guarantees aggregate duration accounting.
            # An abandoned token remains stopped until its owner finishes it;
            # never silently reset a crashed download's budget.
            if active:
                _exhausted()
            if files >= self.MAX_MEDIA_FILES or size >= self.MAX_MEDIA_BYTES or seconds >= self.MAX_MEDIA_SECONDS:
                self._stop(c)
            token = uuid4().hex
            c.execute('INSERT INTO proxy_trial_media(token,trial_id,started_at,pid) VALUES(?,?,?,?)', (token, self.trial_id, now, os.getpid()))
            c.execute('UPDATE proxy_trial_limits SET media_files=media_files+1 WHERE trial_id=?', (self.trial_id,))
            return token

    def _media(self, c, token):
        if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
            _exhausted()
        row = c.execute('SELECT started_at,finished_at,duration FROM proxy_trial_media WHERE token=? AND trial_id=?', (token, self.trial_id)).fetchone()
        if not row or not math.isfinite(row[0]) or row[0] < 0:
            _exhausted()
        return row

    def _remaining(self, c, token, now):
        started, finished, _ = self._media(c, token)
        if finished is not None or now < started:
            _exhausted()
        _, _, _, spent = self._row(c)
        return max(0., self.MAX_MEDIA_SECONDS - spent - (now - started))

    def media_timeout(self, token):
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            remaining = self._remaining(c, token, self._now())
            if remaining <= 0:
                self._stop(c)
            return remaining

    def reserve_media_bytes(self, token, size):
        if type(size) is not int or size < 0:
            raise ValueError('TRIAL_LIMITS_INVALID')
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if self._remaining(c, token, self._now()) <= 0:
                self._stop(c)
            _, _, accepted, _ = self._row(c)
            if accepted + size > self.MAX_MEDIA_BYTES:
                self._stop(c)
            c.execute('UPDATE proxy_trial_limits SET media_bytes=media_bytes+? WHERE trial_id=?', (size, self.trial_id))
            c.execute('UPDATE proxy_trial_media SET bytes=bytes+? WHERE token=? AND trial_id=?', (size, token, self.trial_id))
            return accepted + size

    def finish_media(self, token):
        """Call in finally on success/failure/cancel. Idempotent, no budget refund."""
        now = self._now()
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            started, finished, duration = self._media(c, token)
            if finished is not None:
                return duration
            if now < started:
                _exhausted()
            duration = now - started
            c.execute('UPDATE proxy_trial_media SET finished_at=?,duration=? WHERE token=? AND trial_id=?', (now, duration, token, self.trial_id))
            c.execute('UPDATE proxy_trial_limits SET media_seconds=media_seconds+? WHERE trial_id=?', (duration, self.trial_id))
            return duration

    def snapshot(self):
        with self._connect() as c:
            operations, files, size, seconds = self._row(c, allow_stopped=True)
            stopped = c.execute('SELECT stopped FROM proxy_trial_limits WHERE trial_id=?', (self.trial_id,)).fetchone()[0]
            active = c.execute('SELECT started_at FROM proxy_trial_media WHERE trial_id=? AND finished_at IS NULL', (self.trial_id,)).fetchall()
        now = self._now()
        if any(now < row[0] for row in active):
            _exhausted()
        return dict(trial_id=self.trial_id, operations=operations, media_files=files,
                    media_bytes=size, media_seconds=seconds + sum(now - row[0] for row in active),
                    active_media=len(active), stopped=bool(stopped))


def from_env(snapshot_scheduler_db, *, env=None, clock=time.time):
    return SharedTrialLimits.from_env(snapshot_scheduler_db, env=env, clock=clock)
