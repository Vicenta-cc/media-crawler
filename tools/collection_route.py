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
MANAGED_ROUTE_MODES = frozenset({'proxy_required', 'proxy_preferred', 'direct_degraded'})
ROUTE_MODES = MANAGED_ROUTE_MODES | {'legacy_direct'}
PROXY_CONFIG_FD_ENV = "MEDIACRAWLER_PROXY_CONFIG_FD"
MAX_ENVELOPE_BYTES = 64 * 1024
FD_READ_TIMEOUT = 5.0
ROUTE_ERROR_CODES = frozenset({
    "PROXY_BUSY", "SUPPLEMENT_DEFERRED", "PROXY_REVISION_MISMATCH",
    "PROXY_EXIT_CHANGED", "PROXY_UNAVAILABLE", "PROXY_AUTH_FAILED",
    "PROXY_CONFIG_INVALID", "PROBE_BUDGET_EXHAUSTED",
    "SCHEDULER_POLICY_CONFLICT", "PROXY_EXPIRED", "PROXY_CONNECTION_REFUSED", "PROXY_TRANSPORT_FAILED",
})
ENVELOPE_FIELDS = frozenset({
    "schema_version", "route_mode", "platform", "account_id", "profile_key",
    "proxy_id", "binding_revision", "config_revision", "endpoint", "credentials",
    "expires_at", "observed_exit", "scheduler",
})
MANAGED_FIELDS = frozenset({
    'operation_id', 'lease_id', 'lease_version', 'owner_id', 'exit_generation',
    'snapshot_digest', 'context_id',
})
PROVENANCE_MANAGED_FIELDS = frozenset({'exit_generation', 'snapshot_digest', 'operation_id', 'lease_id', 'lease_version', 'context_id'})
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
    operation_id: str = field(default='', repr=False)
    lease_id: str = field(default='', repr=False)
    lease_version: int = field(default=0, repr=False)
    owner_id: str = field(default='', repr=False)
    exit_generation: int = field(default=0, repr=False)
    snapshot_digest: str = field(default='', repr=False)
    context_id: str = field(default='', repr=False)
    safety_db_path: str = field(default='', repr=False)
    recovery_claim: str = field(default='', repr=False)
    login_bootstrap: bool = field(default=False, repr=False)

    def __repr__(self):
        return "BoundRoute(schema_version=1, route_mode='proxy_required')"

    @property
    def proxy_server(self):
        host = '[' + self.host + ']' if ':' in self.host else self.host
        return "http://" + host + ":" + str(self.port)

    def playwright_proxy(self):
        if self.route_mode in {'direct_degraded', 'legacy_direct'}:
            return None
        result = {"server": self.proxy_server}
        if self.username or self.password:
            result.update(username=self.username, password=self.password)
        return result

    def httpx_proxy(self):
        if self.route_mode in {'direct_degraded', 'legacy_direct'}:
            return None
        if not (self.username or self.password):
            return self.proxy_server
        credentials = quote(self.username, safe='') + ':' + quote(self.password, safe='')
        return "http://" + credentials + '@' + self.proxy_server[len('http://'):]

    def require_not_expired(self, now=None, *, allow_unknown=False):
        now = _timestamp(time.time() if now is None else now, nullable=False)
        if self.expires_at is None:
            if not allow_unknown:
                raise CollectionRouteError("PROXY_UNAVAILABLE")
        elif now >= self.expires_at:
            raise CollectionRouteError("PROXY_UNAVAILABLE")

    @property
    def managed(self):
        return bool(self.operation_id)

    def require_managed_ack(self, ack=None, *, now=None, lease_version=None):
        """Validate the redacted App ACK before managed business traffic."""
        if not self.managed:
            if self.route_mode in {'proxy_preferred', 'direct_degraded'}:
                raise CollectionRouteError('PROXY_CONFIG_INVALID')
            return None
        if not isinstance(ack, dict) or ack.get('operation_id') != self.operation_id:
            raise CollectionRouteError('PROXY_REVISION_MISMATCH')
        if (ack.get('lease_id') != self.lease_id or ack.get('owner_id') != self.owner_id
                or type(ack.get('lease_version')) is not int or ack.get('lease_version') != (self.lease_version if lease_version is None else lease_version)
                or ack.get('snapshot_digest') != self.snapshot_digest
                or ack.get('context_id') != self.context_id):
            raise CollectionRouteError('PROXY_REVISION_MISMATCH')
        expires = ack.get('expires_at')
        if (type(expires) not in (int, float) or self.expires_at is None
                or not math.isfinite(expires) or not 0 < float(expires) <= float(self.expires_at)):
            raise CollectionRouteError('PROXY_REVISION_MISMATCH')
        if (type(ack.get('ttl')) not in (int, float) or not math.isfinite(ack['ttl'])
                or not 0 < ack['ttl'] <= 300):
            raise CollectionRouteError('PROXY_UNAVAILABLE')
        now = time.time() if now is None else now
        if expires <= now:
            raise CollectionRouteError('PROXY_UNAVAILABLE')
        self.require_not_expired(now)
        return ack

    def business_scope_ids(self, receipt, context_id, now=None, ttl=300):
        self.require_not_expired(now)
        require_probe_receipt(self, receipt, context_id, now=now, ttl=ttl)
        return self.scope_ids if self.route_mode == 'direct_degraded' else derive_scope_ids(self.platform, self.account_id, self.proxy_id, receipt.ip)


RouteSnapshot = BoundRoute


def validate_route_envelope(envelope, *, account_id, platform="dy", profile_key=None):
    """Validate legacy v1 or versioned managed route envelope.

    The legacy shape remains readable for explicit compatibility, while any
    managed route must carry operation/lease ownership and a snapshot digest.
    """
    if not isinstance(envelope, dict):
        _invalid()
    extra = set(envelope) - ENVELOPE_FIELDS
    if extra and (not MANAGED_FIELDS <= extra or bool(extra - MANAGED_FIELDS - {'safety_db_path', 'recovery_claim', 'login_bootstrap'})):
        _invalid()
    if not extra:
        fields = ENVELOPE_FIELDS
    else:
        fields = ENVELOPE_FIELDS | extra
    _exact(envelope, fields)
    if type(envelope['schema_version']) is not int or envelope['schema_version'] != 1:
        _invalid()
    if envelope['route_mode'] not in ROUTE_MODES or envelope['platform'] != platform or platform != 'dy':
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
    if envelope['route_mode'] == 'direct_degraded':
        if endpoint is not None:
            _invalid()
        endpoint = {'scheme': 'http', 'host': 'direct.invalid', 'port': 1}
    _exact(endpoint, {'scheme', 'host', 'port'})
    if endpoint['scheme'] != 'http' or type(endpoint['port']) is not int or not 1 <= endpoint['port'] <= 65535:
        _invalid()
    host = _host(endpoint['host'])
    credentials = envelope['credentials']
    _exact(credentials, {'username', 'password'})
    if any(not isinstance(credentials[k], str) or len(credentials[k]) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in credentials[k]) for k in credentials):
        _invalid()
    expires_at = _timestamp(envelope['expires_at'])
    managed = bool(extra)
    if envelope['route_mode'] in {'proxy_preferred', 'direct_degraded'} and not managed:
        _invalid()
    if managed and envelope['route_mode'] == 'legacy_direct':
        _invalid()
    if managed:
        for key in ('operation_id', 'lease_id', 'owner_id', 'context_id'):
            _identity(envelope[key])
        lease_version = _revision(envelope['lease_version'])
        exit_generation = _revision(envelope['exit_generation'])
        digest = envelope['snapshot_digest']
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            _invalid()
        if expires_at is None or expires_at <= time.time():
            raise CollectionRouteError('PROXY_UNAVAILABLE')
    else:
        lease_version = exit_generation = 0
        digest = ''
    observed = envelope['observed_exit']
    if observed is None:
        exit_ip = exit_at = None
    else:
        _exact(observed, {'ip', 'observed_at'})
        exit_ip = canonical_exit_ip(observed['ip'])
        exit_at = _timestamp(observed['observed_at'], nullable=False)
    if envelope['route_mode'] == 'direct_degraded' and exit_ip is None:
        _invalid()
    scheduler = envelope['scheduler']
    _exact(scheduler, {'db_path', 'scope_ids'})
    if not isinstance(scheduler['db_path'], str) or not os.path.isabs(scheduler['db_path']) or '\x00' in scheduler['db_path']:
        _invalid()
    scopes = derive_scope_ids(platform, account_id, proxy_id, exit_ip)
    if envelope['route_mode'] == 'direct_degraded':
        scopes = tuple(sorted([platform, platform + ':account:' + account_id, platform + ':service_exit:' + exit_ip, platform + ':exit:' + exit_ip]))
    if not isinstance(scheduler['scope_ids'], list) or scheduler['scope_ids'] != list(scopes):
        _invalid()
    if type(envelope.get('login_bootstrap', False)) is not bool:
        _invalid()
    if envelope.get('recovery_claim'):
        _identity(envelope['recovery_claim'])
    safety_db = envelope.get('safety_db_path', '')
    if safety_db and (not isinstance(safety_db, str) or not os.path.isabs(safety_db) or '\x00' in safety_db):
        _invalid()
    snapshot = BoundRoute(platform, account_id, derived_profile, proxy_id, binding_revision,
                      config_revision, host, endpoint['port'], credentials['username'],
                      credentials['password'], expires_at, exit_ip, exit_at,
                      scheduler['db_path'], scopes, envelope['route_mode'], 1,
                      envelope.get('operation_id', ''), envelope.get('lease_id', ''),
                      lease_version, envelope.get('owner_id', ''), exit_generation,
                      digest, envelope.get('context_id', ''), safety_db, envelope.get('recovery_claim', ''), envelope.get('login_bootstrap', False))
    if managed and snapshot_digest(snapshot) != digest:
        raise CollectionRouteError('PROXY_REVISION_MISMATCH')
    return snapshot


def snapshot_digest(snapshot):
    """Stable SHA-256 over non-secret route identity and lease metadata."""
    payload = {
        'schema_version': snapshot.schema_version, 'route_mode': snapshot.route_mode,
        'platform': snapshot.platform, 'account_id': snapshot.account_id,
        'profile_key': snapshot.profile_key, 'proxy_id': snapshot.proxy_id,
        'binding_revision': snapshot.binding_revision, 'config_revision': snapshot.config_revision,
        'host': snapshot.host, 'port': snapshot.port,
        'expires_at': None if snapshot.expires_at is None else float(snapshot.expires_at),
        'observed_exit_ip': snapshot.observed_exit_ip,
        'observed_exit_at': None if snapshot.observed_exit_at is None else float(snapshot.observed_exit_at),
        'operation_id': snapshot.operation_id, 'lease_id': snapshot.lease_id,
        'lease_version': snapshot.lease_version, 'owner_id': snapshot.owner_id,
        'exit_generation': snapshot.exit_generation, 'context_id': snapshot.context_id,
        'scope_ids': list(snapshot.scope_ids),
        **({'safety_db_path': snapshot.safety_db_path} if snapshot.safety_db_path else {}),
        **({'recovery_claim': snapshot.recovery_claim} if snapshot.recovery_claim else {}),
        **({'login_bootstrap': True} if snapshot.login_bootstrap else {}),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def route_ack(snapshot, *, owner_id, lease_version, context_id=None, now=None, ttl=300):
    """Create the redacted ACK only after owner/version/TTL checks."""
    if not snapshot.managed or owner_id != snapshot.owner_id or lease_version != snapshot.lease_version:
        raise CollectionRouteError('PROXY_REVISION_MISMATCH')
    now = _timestamp(time.time() if now is None else now, nullable=False)
    snapshot.require_not_expired(now)
    if context_id is not None and context_id != snapshot.context_id:
        raise CollectionRouteError('PROXY_REVISION_MISMATCH')
    remaining = snapshot.expires_at - now
    if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= remaining:
        raise CollectionRouteError('PROXY_UNAVAILABLE')
    return {'operation_id': snapshot.operation_id, 'lease_id': snapshot.lease_id,
            'owner_id': snapshot.owner_id, 'lease_version': snapshot.lease_version,
            'snapshot_digest': snapshot.snapshot_digest, 'context_id': snapshot.context_id,
            'expires_at': snapshot.expires_at, 'ttl': float(ttl)}


def serialize_route(snapshot):
    """Return secret-bearing bytes for the anonymous pipe ONLY."""
    observed = None if snapshot.observed_exit_ip is None else {
        'ip': snapshot.observed_exit_ip, 'observed_at': snapshot.observed_exit_at,
    }
    envelope = dict(schema_version=1, route_mode=snapshot.route_mode, platform=snapshot.platform,
                    account_id=snapshot.account_id, profile_key=snapshot.profile_key,
                    proxy_id=snapshot.proxy_id, binding_revision=snapshot.binding_revision,
                    config_revision=snapshot.config_revision,
                    endpoint=None if snapshot.route_mode == 'direct_degraded' else dict(scheme='http', host=snapshot.host, port=snapshot.port),
                    credentials=dict(username=snapshot.username, password=snapshot.password),
                    expires_at=snapshot.expires_at, observed_exit=observed,
                    scheduler=dict(db_path=snapshot.scheduler_db_path, scope_ids=list(snapshot.scope_ids)))
    if snapshot.managed:
        envelope.update(operation_id=snapshot.operation_id, lease_id=snapshot.lease_id,
                        lease_version=snapshot.lease_version, owner_id=snapshot.owner_id,
                        exit_generation=snapshot.exit_generation, snapshot_digest=snapshot.snapshot_digest,
                        context_id=snapshot.context_id)
        if snapshot.safety_db_path:
            envelope['safety_db_path'] = snapshot.safety_db_path
        if snapshot.recovery_claim:
            envelope['recovery_claim'] = snapshot.recovery_claim
        if snapshot.login_bootstrap:
            envelope['login_bootstrap'] = True
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
        if proxy_required or env.get('MEDIACRAWLER_MANAGED_ROUTE') == '1':
            _invalid()
        return None
    if not isinstance(raw, str) or not re.fullmatch(r'[0-9]+', raw):
        _invalid()
    snapshot = load_route_from_fd(int(raw), account_id=account_id, platform=platform, profile_key=profile_key)
    if env.get('MEDIACRAWLER_MANAGED_ROUTE') == '1' and not snapshot.managed:
        _invalid()
    return snapshot


@dataclass(frozen=True)
class ProbeReceipt:
    proxy_id: str
    config_revision: int
    context_id: str
    ip: str
    observed_at: float
    exit_generation: int = 0


def require_probe_receipt(snapshot, receipt, context_id, *, now=None, ttl=300):
    now = time.time() if now is None else now
    snapshot.require_not_expired(now, allow_unknown=True)
    if (not isinstance(receipt, ProbeReceipt) or receipt.proxy_id != snapshot.proxy_id
            or receipt.config_revision != snapshot.config_revision or receipt.context_id != context_id
            or (snapshot.managed and (receipt.exit_generation != snapshot.exit_generation or context_id != snapshot.context_id))):
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
        return ProbeReceipt(self.snapshot.proxy_id, self.snapshot.config_revision, context_id, browser_ip, at,
                            self.snapshot.exit_generation)

    def require_business(self, receipt, context_id, *, now=None):
        self.snapshot.require_not_expired(now)
        require_probe_receipt(self.snapshot, receipt, context_id, now=now, ttl=self.ttl)
        with self._lock:
            if self._baseline is None or receipt.ip != self._baseline:
                raise CollectionRouteError('PROXY_EXIT_CHANGED')
        return self.snapshot.scope_ids if self.snapshot.route_mode=='direct_degraded' else derive_scope_ids(self.snapshot.platform, self.snapshot.account_id, self.snapshot.proxy_id, receipt.ip)


def validate_collection_route_provenance(value):
    """Return a new exact public v1 dictionary; reject secret/unknown fields."""
    extra = set(value) - PROVENANCE_FIELDS if isinstance(value, dict) else set()
    if extra and extra != PROVENANCE_MANAGED_FIELDS:
        _invalid()
    _exact(value, PROVENANCE_FIELDS | extra)
    if value['route_mode'] in {'proxy_preferred', 'direct_degraded'} and not extra:
        _invalid()
    if extra:
        _revision(value['exit_generation']); _revision(value['lease_version'])
        for key in ('operation_id', 'lease_id', 'context_id'):
            _identity(value[key])
        if not isinstance(value['snapshot_digest'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['snapshot_digest']):
            _invalid()
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['platform'] != 'dy':
        _invalid()
    for name in ('observation_id', 'source_task_id', 'source_batch_id', 'source_account_id'):
        _identity(value[name])
    _timestamp(value['collected_at'], nullable=False)
    if value['route_mode'] in MANAGED_ROUTE_MODES:
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
        **({key:getattr(snapshot,key) for key in PROVENANCE_MANAGED_FIELDS} if snapshot.managed else {}),
    ))
