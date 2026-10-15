"""Owner-only persistent decisions in the original scheduler transaction."""

import json
from contextvars import ContextVar

owner_access = ContextVar("crawler_authority_owner", default=False)


def deny_worker_database():
    import os

    if (
        os.getenv("MEDIACRAWLER_DY_BROWSER_ENGINE") == "camoufox"
        and not owner_access.get()
    ):
        raise RuntimeError("PROXY_UNAVAILABLE")


def process_start(pid):
    import subprocess
    from pathlib import Path

    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        return "" if fields[0] == "Z" else fields[19]
    except FileNotFoundError:
        try:
            return subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "lstart="],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            return ""


def initialize(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS crawler_authority_process_leases (token TEXT PRIMARY KEY, pid INTEGER NOT NULL, start_identity TEXT NOT NULL)"""
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS crawler_authority_ledger (
        channel TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
        result TEXT NOT NULL, grant TEXT UNIQUE, expires_at REAL,
        consumed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(channel,request_id))""")


def lookup(connection, request):
    row = connection.execute(
        "SELECT digest,result FROM crawler_authority_ledger WHERE channel=? AND request_id=?",
        (request["channel"], request["request_id"]),
    ).fetchone()
    if row:
        if row[0] != request["digest"]:
            raise RuntimeError("PROXY_REVISION_MISMATCH")
        return json.loads(row[1])


def record(connection, request, result):
    connection.execute(
        "INSERT INTO crawler_authority_ledger(channel,request_id,digest,result,grant,expires_at) VALUES(?,?,?,?,?,?)",
        (
            request["channel"],
            request["request_id"],
            request["digest"],
            json.dumps(result, allow_nan=False),
            result.get("grant"),
            result.get("grant_expires_at"),
        ),
    )
