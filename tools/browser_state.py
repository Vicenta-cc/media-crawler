# -*- coding: utf-8 -*-

import os
import sqlite3
from pathlib import Path
from typing import Iterable

import config
from tools import utils


def browser_user_data_dir(platform: str | None = None) -> Path:
    platform_name = platform or config.PLATFORM
    return Path(os.getcwd()) / "browser_data" / (config.USER_DATA_DIR % platform_name)


def has_saved_cookie_state(
    cookie_names: Iterable[str],
    domains: Iterable[str],
    platform: str | None = None,
) -> bool:
    cookies_path = browser_user_data_dir(platform) / "Default" / "Cookies"
    if not cookies_path.exists() or cookies_path.stat().st_size == 0:
        return False

    names = tuple(cookie_names)
    domain_patterns = tuple(f"%{domain}" for domain in domains)
    if not names or not domain_patterns:
        return False

    name_placeholders = ",".join("?" for _ in names)
    domain_clause = " OR ".join("host_key LIKE ?" for _ in domain_patterns)
    params = (*names, *domain_patterns)

    try:
        with sqlite3.connect(f"file:{cookies_path}?mode=ro", uri=True, timeout=1) as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM cookies
                WHERE name IN ({name_placeholders}) AND ({domain_clause})
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        utils.logger.warning(
            "[browser_state] Failed to inspect saved login cookies, "
            f"falling back to cookie file presence: {exc}"
        )
        return True
