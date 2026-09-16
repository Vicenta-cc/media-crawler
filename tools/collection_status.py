"""Durable per-item stage outcomes, separate from successful content exports.

Only stable IDs and reason codes are stored. Never persist signed URLs, cookies,
or raw exception messages here. Atomic replacement preserves the previous state
if a process is stopped during a write.
"""
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

import config


class CollectionIncompleteError(RuntimeError):
    """A required collection stage failed; callers must not report completion."""


def write_collection_status(content_id, stage, status, *, reason='', **details):
    root = Path(config.SAVE_DATA_PATH or 'data') / 'douyin' / 'collection_status'
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(content_id).encode()).hexdigest()
    target = root / f'{stage}-{key}.json'
    payload = dict(platform='dy', content_id=str(content_id), stage=stage,
                   status=status, reason=reason, updated_at=time.time(), **details)
    fd, temporary = tempfile.mkstemp(dir=root, prefix='.status-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def failure_reason(exc):
    """Extract only a bounded code/status, never arbitrary platform response text."""
    message = str(exc)
    if message.startswith('COLLECTION_INCOMPLETE: '):
        message = message.split(': ', 1)[1]
    known = {'missing_or_mismatched_detail', 'incomplete_images', 'missing_image_url',
             'empty_image', 'missing_video_url', 'media_download_failed',
             'media_http_error', 'invalid_media_response', 'creator_pagination_stalled',
             'creator_page_budget_exceeded', 'invalid_creator_page',
             'ACCOUNT_VERIFY', 'ACCOUNT_AUTH_INVALID'}
    if message in known or re.fullmatch(r'HTTP [1-5][0-9]{2}', message):
        return message
    return type(exc).__name__
