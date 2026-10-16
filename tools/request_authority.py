"""Private inherited authority channel. This module never opens a database.

Only the Python worker owns this descriptor; browser/Node descendants must not
inherit it. A single reader dispatches replies, including synchronous callers.
"""

import asyncio
import concurrent.futures
import json
import os
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from uuid import uuid4

ENV_FD = "MEDIACRAWLER_REQUEST_AUTHORITY_FD"
ENV_CHANNEL = "MEDIACRAWLER_REQUEST_AUTHORITY_CHANNEL"
MAX_FRAME = 16384
TIMEOUT = 5.0
REQUEST_KINDS = frozenset(
    {
        "api",
        "media",
        "media_refresh",
        "login",
        "login_action",
        "login_navigation",
        "navigate",
        "navigation",
        "browser",
        "search",
        "detail",
        "comments",
    }
)


def _error():
    from tools.collection_route import CollectionRouteError

    return CollectionRouteError("PROXY_UNAVAILABLE")


class AuthorityClient:
    def __init__(self, snapshot, *, env=None):
        env = os.environ if env is None else env
        try:
            fd = int(env[ENV_FD])
            if fd < 3:
                raise ValueError()
            os.set_inheritable(fd, False)
            self.socket = socket.socket(fileno=fd)
            if (
                self.socket.family != socket.AF_UNIX
                or self.socket.type != socket.SOCK_STREAM
            ):
                raise ValueError()
            self.socket.settimeout(TIMEOUT)
            self.channel = env[ENV_CHANNEL]
            if not isinstance(self.channel, str) or len(self.channel) != 32:
                raise ValueError()
        except (OSError, ValueError, KeyError):
            raise _error() from None
        self.identity = {
            "account_id": snapshot.account_id,
            "owner_id": snapshot.owner_id,
            "operation_id": snapshot.operation_id,
            "lease_id": snapshot.lease_id,
            "lease_version": snapshot.lease_version,
            "snapshot_digest": snapshot.snapshot_digest,
            "context_id": snapshot.context_id,
        }
        self.pending = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.closed = False
        self.on_disconnect = None
        self.reader = threading.Thread(
            target=self._read, daemon=True, name="request-authority-reader"
        )
        self.reader.start()

    def _read(self):
        raw = bytearray()
        try:
            while not self.closed:
                try:
                    chunk = self.socket.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_FRAME:
                    break
                while b"\n" in raw:
                    frame, _, rest = raw.partition(b"\n")
                    raw = bytearray(rest)
                    reply = json.loads(frame)
                    with self.lock:
                        future = self.pending.pop(reply["request_id"], None)
                    if future is None or future.done():
                        continue
                    if reply.get("ok") is True:
                        future.set_result(reply.get("result"))
                    else:
                        from tools.collection_route import CollectionRouteError

                        future.set_exception(
                            CollectionRouteError(
                                reply.get("error", "PROXY_UNAVAILABLE")
                            )
                        )
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            self.close()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            pending, self.pending = self.pending, {}
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        callback = self.on_disconnect
        if callback is not None:
            callback()
        for future in pending.values():
            if not future.done():
                future.set_exception(_error())

    def submit(self, operation, payload=None, *, request_id=None):
        request_id = request_id or uuid4().hex
        future = concurrent.futures.Future()
        with self.lock:
            if self.closed or len(self.pending) >= 64 or request_id in self.pending:
                raise _error()
            self.sequence += 1
            frame = {
                "version": 1,
                "channel": self.channel,
                "sequence": self.sequence,
                "request_id": request_id,
                "identity": self.identity,
                "pid": os.getpid(),
                "deadline": time.time() + TIMEOUT,
                "operation": operation,
                "payload": payload or {},
            }
            encoded = (
                json.dumps(frame, allow_nan=False, separators=(",", ":")) + "\n"
            ).encode()
            if len(encoded) > MAX_FRAME:
                raise _error()
            self.pending[request_id] = future
            try:
                self.socket.sendall(encoded)
            except OSError:
                self.pending.pop(request_id, None)
                raise _error() from None
        return future

    def call(self, operation, payload=None, *, request_id=None):
        try:
            return self.submit(operation, payload, request_id=request_id).result(
                TIMEOUT
            )
        except concurrent.futures.TimeoutError:
            self.close()
            raise _error() from None

    async def acall(self, operation, payload=None, *, request_id=None):
        try:
            # Sending is short and bounded, and never holds the event loop during
            # socket backpressure. The single reader resolves the future.
            future = await asyncio.to_thread(
                self.submit, operation, payload, request_id=request_id
            )
            return await asyncio.wait_for(asyncio.wrap_future(future), TIMEOUT)
        except TimeoutError:
            self.close()
            raise _error() from None


class RemoteBudget:
    def __init__(self, authority):
        self.authority = authority

    def reserve(self, category, *, use_remaining=False):
        return self.authority.call(
            "probe_reserve", {"category": category, "use_remaining": use_remaining}
        )

    def confirm_baseline(self, snapshot, ip):
        return self.authority.call("probe_confirm", {"ip": ip})

    def snapshot(self):
        return self.authority.call("probe_snapshot")


class RemoteTrial:
    def __init__(self, authority):
        self.authority = authority

    def reserve_operation(self):
        return self.authority.call("trial_operation")

    def begin_media(self):
        return self.authority.call("trial_begin_media")

    def media_timeout(self, token):
        return self.authority.call("trial_media_timeout", {"token": token})

    def reserve_media_bytes(self, token, size):
        return self.authority.call("trial_media_bytes", {"token": token, "size": size})

    def finish_media(self, token):
        return self.authority.call("trial_finish_media", {"token": token})

    def snapshot(self):
        return self.authority.call("trial_snapshot")


class RemoteGate:
    def __init__(self, authority, *, cooldown_seconds=300, stop_checker=None):
        self.authority = authority
        self.cooldown_seconds = cooldown_seconds
        self.stop_checker = stop_checker
        self.state = self
        self.platform = "dy"

    async def reserve_scopes(
        self, operation="api", *, lease_seconds=None, max_wait=300
    ):
        if operation not in REQUEST_KINDS:
            raise _error()
        started = time.monotonic()
        while True:
            result = await self.authority.acall(
                "reserve", {"operation": operation, "lease_seconds": lease_seconds}
            )
            if result["delay"] == 0:
                # Consume immediately before the caller can issue network I/O.
                try:
                    await self.authority.acall("consume", {"grant": result["grant"]})
                except BaseException:
                    try:
                        await self.authority.acall(
                            "release", {"scope_tokens": result["scope_tokens"]}
                        )
                    except Exception:  # noqa: BLE001, S110 - Preserve the original error; owner reclaims leases.
                        pass  # Parent loss still retains live-child safety locks.
                    raise
                return result["scope_tokens"], time.monotonic() - started
            if time.monotonic() - started + result["delay"] > max_wait:
                raise RuntimeError("REQUEST_SCHEDULER_WAIT_EXCEEDED")
            await asyncio.sleep(min(result["delay"], 0.25))

    async def acquire(self, operation="api"):
        _, waited = await self.reserve_scopes(operation)
        return waited

    @asynccontextmanager
    async def slot(self, operation="api", *, timeout=60, max_wait=300):
        import math

        from tools.persistent_request_gate import (
            MEDIA_LEASE_SECONDS,
            renewable_media_lifetime,
        )
        renewable = operation == 'media' and timeout is None
        if not renewable and (timeout is None or not math.isfinite(timeout) or timeout <= 0 or timeout > 3600):
            raise ValueError("finite request timeout is required")
        tokens, waited = await self.reserve_scopes(
            operation, lease_seconds=MEDIA_LEASE_SECONDS if renewable else timeout + 30, max_wait=max_wait
        )
        try:
            if renewable:
                async def renew():
                    result = await self.authority.acall('renew_request', {'scope_tokens': tokens})
                    if (not isinstance(result, dict) or set(result) != {'expires_at'}
                            or type(result['expires_at']) not in (int, float)
                            or not math.isfinite(result['expires_at']) or result['expires_at'] <= time.time()):
                        raise _error()
                async with renewable_media_lifetime(renew, self.stop_checker):
                    yield waited
            else:
                async with asyncio.timeout(timeout):
                    yield waited
        finally:
            original_error = sys.exc_info()[0]
            try:
                await self.authority.acall("release", {"scope_tokens": tokens})
            except Exception:  # Keep cancellation/body failure; owner retains live leases.
                if original_error is None:
                    raise

    slot_scopes = slot

    def enter_cooldown(self, reason, seconds=None):
        return self.authority.call(
            "cooldown",
            {
                "reason": reason,
                "seconds": self.cooldown_seconds if seconds is None else seconds,
            },
        )

    def snapshot(self):
        return self.authority.call("gate_snapshot")["dy"]

    def snapshots(self):
        return self.authority.call("gate_snapshot")
