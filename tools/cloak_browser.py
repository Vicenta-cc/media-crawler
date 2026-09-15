"""Account-bound persistent CloakBrowser contexts; no shared/default account."""
from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
from pathlib import Path


def profile_for(account_id: str, root: Path, version: str) -> dict:
    if not account_id.strip():
        raise ValueError("CloakBrowser requires MEDIACRAWLER_ACCOUNT_ID")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    key = hashlib.sha256(("dy:" + account_id).encode()).hexdigest()
    registry = root / "profiles.sqlite3"
    with sqlite3.connect(registry) as db:
        os.chmod(registry, 0o600)
        db.execute("CREATE TABLE IF NOT EXISTS profiles (account TEXT PRIMARY KEY, seed INTEGER UNIQUE, version TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT seed, version FROM profiles WHERE account=?", (key,)).fetchone()
        if row is None:
            seed = db.execute("SELECT COALESCE(MAX(seed), 9999) + 1 FROM profiles").fetchone()[0]
            if seed > 99999:
                raise RuntimeError("CloakBrowser profile seed capacity reached")
            db.execute("INSERT INTO profiles VALUES (?, ?, ?)", (key, seed, version))
            row = (seed, version)
    directory = root / key
    directory.mkdir(mode=0o700, exist_ok=True)
    return {"directory": directory, "seed": row[0], "version": row[1]}


async def launch_account_context(account_id: str, root: Path, *, headless: bool, proxy=None):
    from cloakbrowser import launch_persistent_context_async
    from cloakbrowser.config import get_chromium_version

    profile = profile_for(account_id, root, get_chromium_version())
    lock = (profile["directory"] / "adapter.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("CloakBrowser account profile is already in use") from None
    try:
        context = await launch_persistent_context_async(
            user_data_dir=profile["directory"], headless=headless, proxy=proxy,
            args=[f"--fingerprint={profile['seed']}"],
            browser_version=profile["version"], humanize=False, geoip=False,
            accept_downloads=True,
        )
    except BaseException:
        lock.close()
        raise
    close = context.close

    async def close_locked(*args, **kwargs):
        try:
            await close(*args, **kwargs)
        finally:
            lock.close()

    context.close = close_locked
    return context, profile


async def prepare_account_login(context, profile):
    """Start an explicit fresh login in the existing profile, retaining its identity.

    Mark the profile first: a cancelled login must not resurrect an old database
    snapshot on the next crawl. Call only while holding the profile's launch lock.
    """
    (profile["directory"] / "auth-imported").touch(mode=0o600)
    state = await context.storage_state()
    # A reopened persistent context may not enumerate localStorage origins
    # until they have been visited. Include known login origins and cookie hosts.
    origins = {item["origin"] for item in state.get("origins", [])}
    origins.update("https://" + host for host in (
        "www.douyin.com", "douyin.com", "creator.douyin.com", "live.douyin.com", "douhot.douyin.com"))
    origins.update("https://" + cookie["domain"].lstrip(".")
                   for cookie in state.get("cookies", []) if cookie.get("domain"))
    for page in list(context.pages):
        await page.close()
    await context.clear_cookies()
    page = await context.new_page()
    try:
        await page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html></html>"))
        for origin in sorted(origins):
            await page.goto(origin, wait_until="domcontentloaded")
            await page.evaluate("localStorage.clear(); sessionStorage.clear()")
    finally:
        await page.close()
