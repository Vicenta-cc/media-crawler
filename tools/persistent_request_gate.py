from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path


class PersistentRequestGate:
    def __init__(self, db_path: str | Path, *, platform: str = "dy", min_interval: float = 2.0, per_minute: int = 30):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.platform = platform
        self.min_interval = max(0.0, float(min_interval))
        self.per_minute = max(1, int(per_minute))
        self._lock = asyncio.Lock()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS request_gate (platform TEXT PRIMARY KEY, next_at REAL NOT NULL DEFAULT 0, times TEXT NOT NULL DEFAULT '[]')")

    async def acquire(self) -> float:
        waited = 0.0
        while True:
            async with self._lock:
                now = time.time()
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute("SELECT next_at,times FROM request_gate WHERE platform=?", (self.platform,)).fetchone()
                    next_at = float(row[0]) if row else 0.0
                    try:
                        times = [float(x) for x in json.loads(row[1])] if row else []
                    except (TypeError, ValueError, json.JSONDecodeError):
                        times = []
                    times = [x for x in times if x > now - 60]
                    target = max(now, next_at)
                    if len(times) >= self.per_minute:
                        target = max(target, min(times) + 60)
                    delay = max(0.0, target - now)
                    if delay == 0:
                        times.append(now)
                        conn.execute("INSERT INTO request_gate(platform,next_at,times) VALUES(?,?,?) ON CONFLICT(platform) DO UPDATE SET next_at=excluded.next_at,times=excluded.times", (self.platform, now + self.min_interval, json.dumps(times)))
                        conn.commit()
                        return waited
            await asyncio.sleep(delay)
            waited += delay
