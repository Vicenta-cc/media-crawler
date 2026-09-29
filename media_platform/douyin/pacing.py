"""Account-safety pacing helpers: jittered sleeps and silent risk detection.

Douyin sometimes answers a blocked identity with HTTP 200 / status_code 0 and
an empty body where content must exist. Consecutive such answers are treated
as a verification challenge so the account stops instead of spreading the
block to other endpoints.
"""
import asyncio

import config
from tools import utils
from tools.persistent_request_gate import jitter_factor

from .exception import DataFetchError

SILENT_RISK_LIMIT = 2
_silent_risk_streak = 0
# A single-post detail run has no second request to confirm a signal with.
_single_detail_target = False


def jittered_delay(base: float) -> float:
    return max(0.0, float(base)) * jitter_factor(getattr(config, "DY_PACING_JITTER", 0))


async def jittered_sleep(base: float) -> float:
    delay = jittered_delay(base)
    await asyncio.sleep(delay)
    return delay


def is_ok_status(payload) -> bool:
    return isinstance(payload, dict) and payload.get("status_code") in (None, 0, "0")


# Explicit "post unavailable" markers (deleted / private / filtered). A detail
# response carrying any of them is a normal result, not a silent block.
_UNAVAILABLE_KEYS = ("filter_detail", "filter_reason", "filter_list", "status_msg")


def is_unavailable_content(payload) -> bool:
    return isinstance(payload, dict) and any(payload.get(key) for key in _UNAVAILABLE_KEYS)


def set_single_detail_target(single: bool) -> None:
    global _single_detail_target
    _single_detail_target = bool(single)


def record_silent_risk(endpoint: str) -> None:
    global _silent_risk_streak
    _silent_risk_streak += 1
    limit = 1 if endpoint == "aweme_detail" and _single_detail_target else SILENT_RISK_LIMIT
    utils.logger.warning(
        f"SILENT_RISK_SIGNAL endpoint={endpoint} consecutive={_silent_risk_streak}/{limit}"
    )
    if _silent_risk_streak >= limit:
        raise DataFetchError("ACCOUNT_VERIFY: silent empty responses")


def record_content_ok() -> None:
    global _silent_risk_streak
    _silent_risk_streak = 0


def silent_risk_streak() -> int:
    return _silent_risk_streak


def reset_silent_risk() -> None:
    record_content_ok()
    set_single_detail_target(False)
