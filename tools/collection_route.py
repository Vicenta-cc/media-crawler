"""Version-one collection routing contract; no network or application imports.

Load this file with importlib without registering it in sys.modules. Secrets
exist only in the validated in-memory snapshot and transport arguments.
"""
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import selectors
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

SCHEMA_VERSION = 1
PROXY_CONFIG_FD_ENV = "MEDIACRAWLER_PROXY_CONFIG_FD"
MAX_ENVELOPE_BYTES = 64 * 1024
FD_READ_TIMEOUT = 5.0
ROUTE_ERROR_CODES = frozenset({
    "PROXY_BUSY", "SUPPLEMENT_DEFERRED", "PROXY_REVISION_MISMATCH",
    "PROXY_EXIT_CHANGED", "PROXY_UNAVAILABLE", "PROXY_AUTH_FAILED",
    "PROXY_CONFIG_INVALID", "PROBE_BUDGET_EXHAUSTED",
    "SCHEDULER_POLICY_CONFLICT",
})
ENVELOPE_FIELDS = frozenset({
    "schema_version", "route_mode", "platform", "account_id", "profile_key",
    "proxy_id", "binding_revision", "config_revision", "endpoint", "credentials",
    "expires_at", "observed_exit", "scheduler",
})
PROVENANCE_FIELDS = frozenset({
    "schema_version", "observation_id", "source_task_id", "source_batch_id",
    "collected_at", "platform", "source_account_id", "route_mode", "proxy_id",
    "binding_revision", "config_revision", "observed_exit_ip", "observed_exit_at",
})


class CollectionRouteError(RuntimeError):
    """Public error with a fixed code only: never attach a URL or credentials."""
    def __init__(self, code):
        self.code = code if code in ROUTE_ERROR_CODES else "PROXY_CONFIG_INVALID"
        super().__init__(self.code)


RouteError = CollectionRouteError


def _invalid():
    raise CollectionRouteError("PROXY_CONFIG_INVALID")


def _exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        _invalid()


def _identity(value):
    # IDs become public scheduler keys. Forbid delimiters/control characters.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        _invalid()
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        _invalid()
    return value


def _timestamp(value, nullable=True):
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _invalid()
    return float(value)


def canonical_exit_ip(value):
    try:
        if not isinstance(value, str) or '%' in value:
            _invalid()
        return str(ipaddress.ip_address(value))
    except (ValueError, TypeError):
        raise CollectionRouteError("PROXY_CONFIG_INVALID") from None


def derive_profile_key(account_id, platform="dy"):
    _identity(account_id)
    if platform != "dy":
        _invalid()
    return hashlib.sha256((platform + ":" + account_id).encode("utf-8")).hexdigest()


def derive_scope_ids(platform, account_id, proxy_id, exit_ip=None):
    if platform != "dy":
        _invalid()
    _identity(account_id)
    _identity(proxy_id)
    scopes = [platform, platform + ":account:" + account_id, platform + ":proxy:" + proxy_id]
    if exit_ip is not None:
        scopes.append(platform + ":exit:" + canonical_exit_ip(exit_ip))
    return tuple(sorted(scopes))


def _host(value):
    if not isinstance(value, str) or not value or any(c in value for c in "/?#@[]%\\"):
        _invalid()
    try:
        parsed = ipaddress.ip_address(value)
        return str(parsed)
    except ValueError:
        try:
            ascii_host = value.encode("idna").decode("ascii").lower()
        except UnicodeError:
            _invalid()
        if len(ascii_host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in ascii_host.rstrip('.').split('.')
        ):
            _invalid()
        return ascii_host.rstrip('.')


@dataclass(frozen=True, repr=False)
class BoundRoute:
    platform: str
    account_id: str
    profile_key: str
    proxy_id: str
    binding_revision: int
    config_revision: int
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    expires_at: object
    observed_exit_ip: object
    observed_exit_at: object
    scheduler_db_path: str
    scope_ids: tuple
    route_mode: str = "proxy_required"
    schema_version: int = SCHEMA_VERSION

    def __repr__(self):
        return "BoundRoute(schema_version=1, route_mode='proxy_required')"

    @property
    def proxy_server(self):
        host = '[' + self.host + ']' if ':' in self.host else self.host
        return "http://" + host + ":" + str(self.port)

    def playwright_proxy(self):
        result = {"server": self.proxy_server}
        if self.username or self.password:
            result.update(username=self.username, password=self.password)
        return result

    def httpx_proxy(self):
        if not (self.username or self.password):
            return self.proxy_server
        credentials = quote(self.username, safe='') + ':' + quote(self.password, safe='')
        return "http://" + credentials + '@' + self.proxy_server[len('http://'):]

    def require_not_expired(self, now=None, *, allow_unknown=False):
        now = time.time() if now is None else now
        if self.expires_at is None:
            if not allow_unknown:
                raise CollectionRouteError("PROXY_UNAVAILABLE")
        elif now >= self.expires_at:
            raise CollectionRouteError("PROXY_UNAVAILABLE")

    def business_scope_ids(self, receipt, context_id, now=None, ttl=300):
        self.require_not_expired(now)
        require_probe_receipt(self, receipt, context_id, now=now, ttl=ttl)
        return derive_scope_ids(self.platform, self.account_id, self.proxy_id, receipt.ip)


RouteSnapshot = BoundRoute


def validate_route_envelope(envelope, *, account_id, platform="dy", profile_key=None):
    """Validate exact v1 shape and independently supplied execution identity."""
    _exact(envelope, ENVELOPE_FIELDS)
    if type(envelope['schema_version']) is not int or envelope['schema_version'] != 1:
        _invalid()
    if envelope['route_mode'] != 'proxy_required' or envelope['platform'] != platform or platform != 'dy':
        _invalid()
    if envelope['account_id'] != account_id:
        _invalid()
    derived_profile = derive_profile_key(account_id, platform)
    if envelope['profile_key'] != derived_profile or (profile_key is not None and profile_key != derived_profile):
        _invalid()
    proxy_id = _identity(envelope['proxy_id'])
    binding_revision = _revision(envelope['binding_revision'])
    config_revision = _revision(envelope['config_revision'])
    endpoint = envelope['endpoint']
    _exact(endpoint, {'scheme', 'host', 'port'})
    if endpoint['scheme'] != 'http' or type(endpoint['port']) is not int or not 1 <= endpoint['port'] <= 65535:
        _invalid()
    host = _host(endpoint['host'])
    credentials = envelope['credentials']
    _exact(credentials, {'username', 'password'})
    if any(not isinstance(credentials[k], str) or len(credentials[k]) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in credentials[k]) for k in credentials):
        _invalid()
    expires_at = _timestamp(envelope['expires_at'])
    observed = envelope['observed_exit']
    if observed is None:
        exit_ip = exit_at = None
    else:
        _exact(observed, {'ip', 'observed_at'})
        exit_ip = canonical_exit_ip(observed['ip'])
        exit_at = _timestamp(observed['observed_at'], nullable=False)
    scheduler = envelope['scheduler']
    _exact(scheduler, {'db_path', 'scope_ids'})
    if not isinstance(scheduler['db_path'], str) or not os.path.isabs(scheduler['db_path']) or '\x00' in scheduler['db_path']:
        _invalid()
    scopes = derive_scope_ids(platform, account_id, proxy_id, exit_ip)
    if not isinstance(scheduler['scope_ids'], list) or scheduler['scope_ids'] != list(scopes):
        _invalid()
    return BoundRoute(platform, account_id, derived_profile, proxy_id, binding_revision,
                      config_revision, host, endpoint['port'], credentials['username'],
                      credentials['password'], expires_at, exit_ip, exit_at,
                      scheduler['db_path'], scopes)


def serialize_route(snapshot):
    """Return secret-bearing bytes for the anonymous pipe ONLY."""
    observed = None if snapshot.observed_exit_ip is None else {
        'ip': snapshot.observed_exit_ip, 'observed_at': snapshot.observed_exit_at,
    }
    envelope = dict(schema_version=1, route_mode=snapshot.route_mode, platform=snapshot.platform,
                    account_id=snapshot.account_id, profile_key=snapshot.profile_key,
                    proxy_id=snapshot.proxy_id, binding_revision=snapshot.binding_revision,
                    config_revision=snapshot.config_revision,
                    endpoint=dict(scheme='http', host=snapshot.host, port=snapshot.port),
                    credentials=dict(username=snapshot.username, password=snapshot.password),
                    expires_at=snapshot.expires_at, observed_exit=observed,
                    scheduler=dict(db_path=snapshot.scheduler_db_path, scope_ids=list(snapshot.scope_ids)))
    validate_route_envelope(envelope, account_id=snapshot.account_id, platform=snapshot.platform)
    data = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(data) > MAX_ENVELOPE_BYTES:
        _invalid()
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def load_route_from_fd(fd, *, account_id, platform="dy", profile_key=None, timeout=FD_READ_TIMEOUT):
    """Own and close an anonymous read-only pipe FD, reading through EOF.

    timeout can be lowered by fixtures/callers, never raised beyond five seconds.
    No browser or HTTP factory is invoked by this parser.
    """
    if type(fd) is not int or fd < 3:
        _invalid()
    try:
        if type(timeout) not in (int, float) or not 0 < timeout <= FD_READ_TIMEOUT:
            _invalid()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if flags & os.O_ACCMODE != os.O_RDONLY:
            _invalid()
        import stat
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            _invalid()
        deadline = time.monotonic() + timeout
        chunks = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _invalid()
                if not selector.select(remaining):
                    _invalid()
                chunk = os.read(fd, min(8192, MAX_ENVELOPE_BYTES + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > MAX_ENVELOPE_BYTES:
                    _invalid()
        envelope = json.loads(chunks.decode('utf-8'), object_pairs_hook=_unique_object)
        return validate_route_envelope(envelope, account_id=account_id, platform=platform, profile_key=profile_key)
    except CollectionRouteError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise CollectionRouteError('PROXY_CONFIG_INVALID') from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def load_route_from_env(env=None, *, account_id, platform="dy", profile_key=None, proxy_required=False):
    """Absent FD is legacy only when proxy_required is explicitly false."""
    env = os.environ if env is None else env
    raw = env.get(PROXY_CONFIG_FD_ENV)
    if raw is None:
        if proxy_required:
            _invalid()
        return None
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]+', raw):
        _invalid()
    return load_route_from_fd(int(raw), account_id=account_id, platform=platform, profile_key=profile_key)


@dataclass(frozen=True)
class ProbeReceipt:
    proxy_id: str
    config_revision: int
    context_id: str
    ip: str
    observed_at: float


def require_probe_receipt(snapshot, receipt, context_id, *, now=None, ttl=300):
    now = time.time() if now is None else now
    snapshot.require_not_expired(now, allow_unknown=True)
    if not isinstance(receipt, ProbeReceipt) or receipt.proxy_id != snapshot.proxy_id or receipt.config_revision != snapshot.config_revision or receipt.context_id != context_id:
        raise CollectionRouteError('PROXY_REVISION_MISMATCH')
    ip = canonical_exit_ip(receipt.ip)
    at = _timestamp(receipt.observed_at, nullable=False)
    if not 0 < ttl <= 300 or at > now or now - at > ttl:
        raise CollectionRouteError('PROXY_UNAVAILABLE')
    if snapshot.observed_exit_ip is not None and ip != snapshot.observed_exit_ip:
        raise CollectionRouteError('PROXY_EXIT_CHANGED')
    return receipt


class ProbeGuard:
    """One execution's atomic echo budget and baseline across context opens.

    reserve_pair debits before either attempt (failed attempts are not refunded).
    Transport owners must perform isolated browser+HTTP probes themselves.
    """
    def __init__(self, snapshot, *, max_calls=96, ttl=300):
        if type(max_calls) is not int or max_calls < 2 or not 0 < ttl <= 300:
            _invalid()
        self.snapshot = snapshot
        self.max_calls = max_calls
        self.ttl = ttl
        self.calls_used = 0
        self._baseline = snapshot.observed_exit_ip
        self._lock = threading.Lock()

    def reserve_pair(self):
        with self._lock:
            if self.calls_used + 2 > self.max_calls:
                raise CollectionRouteError('PROBE_BUDGET_EXHAUSTED')
            self.calls_used += 2
            return self.calls_used

    def confirm_pair(self, context_id, browser_ip, http_ip, *, observed_at=None):
        _identity(context_id)
        if browser_ip is None or http_ip is None:
            raise CollectionRouteError('PROXY_UNAVAILABLE')
        browser_ip, http_ip = canonical_exit_ip(browser_ip), canonical_exit_ip(http_ip)
        if browser_ip != http_ip:
            raise CollectionRouteError('PROXY_EXIT_CHANGED')
        at = time.time() if observed_at is None else _timestamp(observed_at, nullable=False)
        self.snapshot.require_not_expired(at, allow_unknown=True)
        with self._lock:
            if self._baseline is not None and self._baseline != browser_ip:
                raise CollectionRouteError('PROXY_EXIT_CHANGED')
            self._baseline = browser_ip
        return ProbeReceipt(self.snapshot.proxy_id, self.snapshot.config_revision, context_id, browser_ip, at)

    def require_business(self, receipt, context_id, *, now=None):
        self.snapshot.require_not_expired(now)
        require_probe_receipt(self.snapshot, receipt, context_id, now=now, ttl=self.ttl)
        with self._lock:
            if self._baseline is None or receipt.ip != self._baseline:
                raise CollectionRouteError('PROXY_EXIT_CHANGED')
        return derive_scope_ids(self.snapshot.platform, self.snapshot.account_id, self.snapshot.proxy_id, receipt.ip)


def validate_collection_route_provenance(value):
    """Return a new exact public v1 dictionary; reject secret/unknown fields."""
    _exact(value, PROVENANCE_FIELDS)
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['platform'] != 'dy':
        _invalid()
    for name in ('observation_id', 'source_task_id', 'source_batch_id', 'source_account_id'):
        _identity(value[name])
    _timestamp(value['collected_at'], nullable=False)
    if value['route_mode'] == 'proxy_required':
        _identity(value['proxy_id'])
        _revision(value['binding_revision'])
        _revision(value['config_revision'])
        canonical_exit_ip(value['observed_exit_ip'])
        _timestamp(value['observed_exit_at'], nullable=False)
    elif value['route_mode'] == 'legacy_direct':
        if any(value[k] is not None for k in ('proxy_id', 'binding_revision', 'config_revision', 'observed_exit_ip', 'observed_exit_at')):
            _invalid()
    else:
        _invalid()
    return dict(value)


def collection_route_provenance(snapshot, receipt, *, observation_id, source_task_id,
                                source_batch_id, collected_at, context_id):
    snapshot.require_not_expired(collected_at)
    require_probe_receipt(snapshot, receipt, context_id, now=collected_at)
    return validate_collection_route_provenance(dict(
        schema_version=1, observation_id=observation_id, source_task_id=source_task_id,
        source_batch_id=source_batch_id, collected_at=collected_at, platform=snapshot.platform,
        source_account_id=snapshot.account_id, route_mode=snapshot.route_mode,
        proxy_id=snapshot.proxy_id, binding_revision=snapshot.binding_revision,
        config_revision=snapshot.config_revision, observed_exit_ip=receipt.ip,
        observed_exit_at=receipt.observed_at,
    ))
