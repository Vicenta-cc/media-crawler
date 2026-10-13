"""Fixed collection route, isolated exit probes and shared finite echo budget."""
import asyncio
import fcntl
import hashlib
import stat
import threading
import json
import math
import os
import sqlite3
import selectors
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from tools.collection_route import (
    CollectionRouteError, ProbeGuard, ProbeReceipt, canonical_exit_ip, collection_route_provenance,
    load_route_from_env,
)


def _error(code='PROXY_CONFIG_INVALID'):
    raise CollectionRouteError(code)


def transport_error_code(error, *, managed=False):
    if isinstance(error, httpx.ProxyError) and '407' in str(error):
        return 'PROXY_AUTH_FAILED'
    if managed and isinstance(error, httpx.ConnectError):
        import errno
        cause = error
        for _ in range(8):
            if getattr(cause, 'errno', None) == errno.ECONNREFUSED:
                return 'PROXY_CONNECTION_REFUSED'
            cause = getattr(cause, '__cause__', None) or getattr(cause, '__context__', None)
            if cause is None:
                break
        return 'PROXY_TRANSPORT_FAILED'
    return 'PROXY_UNAVAILABLE'


def fixed_echo_url(value):
    """Only an explicit HTTPS origin/path, no auth, query or fragment."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(ord(c) < 33 for c in value)):
            _error()
        parsed.port
        return value
    except (ValueError, TypeError):
        _error()


def parse_exit_sample(body):
    """Accept a bare IP or exact {ip: string}; never extract from arbitrary text."""
    if not isinstance(body, (str, bytes)) or len(body) > 1024:
        _error('PROXY_UNAVAILABLE')
    try:
        body = body.decode('utf-8') if isinstance(body, bytes) else body
        sample = body.strip()
        if sample.startswith('{'):
            pairs = json.loads(sample, object_pairs_hook=lambda pairs: pairs)
            if not isinstance(pairs, list) or len(pairs) != 1 or pairs[0][0] != 'ip':
                _error('PROXY_UNAVAILABLE')
            sample = pairs[0][1]
        return canonical_exit_ip(sample)
    except (ValueError, UnicodeError, TypeError, CollectionRouteError):
        raise CollectionRouteError('PROXY_UNAVAILABLE') from None


def emit_account_risk(snapshot):
    if not snapshot.managed:
        return False
    try:
        fd = int(os.environ['MEDIACRAWLER_PROXY_RECEIPT_FD'])
        value = dict(type='account_risk',operation_id=snapshot.operation_id,owner_id=snapshot.owner_id,
            lease_version=snapshot.lease_version,snapshot_digest=snapshot.snapshot_digest,context_id=snapshot.context_id,
            reason='ACCOUNT_CHALLENGE')
        data = (json.dumps(value,allow_nan=False)+'\n').encode()
        os.set_blocking(fd,False)
        return os.write(fd,data)==len(data)
    except (OSError,ValueError,KeyError):
        return False


BROWSER_GUARD_REASONS = frozenset({'BOUND_REQUEST_GUARD','BOUND_RUNTIME_GUARD','BOUND_DATABASE_GUARD',
    'SHARED_ADMISSION_GUARD','SHARED_COOLDOWN_GUARD','SHARED_REQUEST_LIMIT','SHARED_ACTIVE_LIMIT',
    'PAGE_REQUEST_LIMIT','PAGE_ROLLING_LIMIT','PAGE_LIFETIME_LIMIT','PAGE_BYTE_LIMIT','PAGE_SIZES_FAILED',
    'CDP_GUARD_TIMEOUT','CDP_GUARD_FAILED','PERIODIC_PROBE_GUARD'})


def emit_browser_guard_stopped(snapshot, reason):
    if not snapshot.managed or reason not in BROWSER_GUARD_REASONS:
        return False
    try:
        fd=int(os.environ['MEDIACRAWLER_PROXY_RECEIPT_FD'])
        value=dict(type='browser_guard_stopped',operation_id=snapshot.operation_id,owner_id=snapshot.owner_id,
            lease_version=snapshot.lease_version,snapshot_digest=snapshot.snapshot_digest,context_id=snapshot.context_id,reason=reason)
        data=(json.dumps(value,allow_nan=False)+'\n').encode()
        os.set_blocking(fd,False)
        return os.write(fd,data)==len(data)
    except (OSError,ValueError,KeyError):
        return False


def emit_probe_receipt(snapshot, receipt, *, closed=False, env=None):
    """Best-effort nonblocking public ACK pipe. False means parent must defer."""
    env = os.environ if env is None else env
    raw_fd = env.get('MEDIACRAWLER_PROXY_RECEIPT_FD')
    if raw_fd is None:
        return False
    try:
        if not isinstance(raw_fd, str) or not raw_fd.isdecimal():
            return False
        fd = int(raw_fd)
        if fd < 3 or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY or not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return False
        record = dict(schema_version=1, type='receipt_closed' if closed else 'probe_receipt',
                      platform=snapshot.platform, account_id=snapshot.account_id,
                      proxy_id=snapshot.proxy_id, binding_revision=snapshot.binding_revision,
                      config_revision=snapshot.config_revision, context_id=receipt.context_id,
                      ip=receipt.ip, observed_at=receipt.observed_at)
        if snapshot.managed:
            record.update(operation_id=snapshot.operation_id, lease_id=snapshot.lease_id,
                          lease_version=snapshot.lease_version, owner_id=snapshot.owner_id,
                          snapshot_digest=snapshot.snapshot_digest,
                          exit_generation=snapshot.exit_generation)
        data = (json.dumps(record, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if len(data) > 2048:
            return False
        os.set_blocking(fd, False)
        return os.write(fd, data) == len(data)
    except (OSError, ValueError, TypeError):
        return False


class SharedProbeBudget:
    """Atomic total/category budget in the same DB across child processes.

    One execution/trial ID must be passed unchanged to every participating child.
    Failed attempts spend reserved budget. No clear/refund operation exists.
    """
    def __init__(self, db_path, budget_id, *, max_calls=96):
        if not isinstance(budget_id, str) or not budget_id or len(budget_id) > 128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in budget_id):
            _error()
        if type(max_calls) is not int or max_calls < 4 or max_calls > 96:
            _error()
        self.db_path, self.budget_id, self.max_calls = str(db_path), budget_id, max_calls
        try:
            with self._connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS collection_probe_budget (budget_id TEXT PRIMARY KEY, max_calls INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, initial INTEGER NOT NULL DEFAULT 0, opens INTEGER NOT NULL DEFAULT 0, ticks INTEGER NOT NULL DEFAULT 0, boundaries INTEGER NOT NULL DEFAULT 0)')
                db.execute('CREATE TABLE IF NOT EXISTS collection_route_health (proxy_id TEXT NOT NULL, config_revision INTEGER NOT NULL, stop_code TEXT NOT NULL, PRIMARY KEY(proxy_id,config_revision))')
                db.execute('CREATE TABLE IF NOT EXISTS collection_probe_baseline (budget_id TEXT NOT NULL, proxy_id TEXT NOT NULL, config_revision INTEGER NOT NULL, ip TEXT NOT NULL, PRIMARY KEY(budget_id,proxy_id,config_revision))')
                db.execute('INSERT OR IGNORE INTO collection_probe_budget(budget_id,max_calls) VALUES(?,?)', (budget_id, max_calls))
                if db.execute('SELECT max_calls FROM collection_probe_budget WHERE budget_id=?', (budget_id,)).fetchone()[0] != max_calls:
                    _error()
        except sqlite3.Error:
            raise CollectionRouteError('PROXY_UNAVAILABLE') from None

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=.2)

    def reserve(self, category, *, use_remaining=False):
        """Return pairs, optionally reallocating unused total budget to login opens."""
        if (category not in ('open', 'tick', 'boundary') or type(use_remaining) is not bool
                or (use_remaining and category != 'open')):
            _error()
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                maximum, used, initial, opens, ticks, boundaries = db.execute('SELECT max_calls,used,initial,opens,ticks,boundaries FROM collection_probe_budget WHERE budget_id=?', (self.budget_id,)).fetchone()
                first = category == 'open' and not initial
                pairs = 2 if first else 1
                counter, limit = {'open': (opens, 10), 'tick': (ticks, 24), 'boundary': (boundaries, 12)}[category]
                if used + 2 * pairs > maximum or (not first and counter >= limit and not use_remaining):
                    _error('PROBE_BUDGET_EXHAUSTED')
                column = {'open': 'opens', 'tick': 'ticks', 'boundary': 'boundaries'}[category]
                if first:
                    db.execute('UPDATE collection_probe_budget SET used=used+4,initial=1 WHERE budget_id=?', (self.budget_id,))
                else:
                    db.execute('UPDATE collection_probe_budget SET used=used+2,' + column + '=' + column + '+1 WHERE budget_id=?', (self.budget_id,))
                return pairs
        except sqlite3.Error:
            raise CollectionRouteError('PROXY_UNAVAILABLE') from None

    def confirm_baseline(self, snapshot, ip):
        try:
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                key = (self.budget_id, snapshot.proxy_id, snapshot.config_revision)
                row = db.execute('SELECT ip FROM collection_probe_baseline WHERE budget_id=? AND proxy_id=? AND config_revision=?', key).fetchone()
                if row and row[0] != ip:
                    _error('PROXY_EXIT_CHANGED')
                db.execute('INSERT OR IGNORE INTO collection_probe_baseline VALUES(?,?,?,?)', (*key, ip))
        except sqlite3.Error:
            raise CollectionRouteError('PROXY_UNAVAILABLE') from None

    def snapshot(self):
        with self._connect() as db:
            row = db.execute('SELECT max_calls,used,initial,opens,ticks,boundaries FROM collection_probe_budget WHERE budget_id=?', (self.budget_id,)).fetchone()
        return dict(zip(('max_calls', 'used', 'initial', 'opens', 'ticks', 'boundaries'), row))


_PROCESS_ROUTE_UNSET = object()
_process_route = _PROCESS_ROUTE_UNSET


def initialize_bound_route(env=None, *, platform='dy'):
    """Consume FD once at startup; subsequent consumers share one module instance."""
    global _process_route
    if _process_route is _PROCESS_ROUTE_UNSET:
        env = os.environ if env is None else env
        required = env.get('MEDIACRAWLER_PROXY_REQUIRED', '').lower() in ('1', 'true', 'yes')
        _process_route = load_route_from_env(env, account_id=env.get('MEDIACRAWLER_ACCOUNT_ID', ''), platform=platform, proxy_required=required)
    return _process_route


def obtain_route_ack(snapshot, *, env=None, timeout=5.0, previous_ack=None):
    """Request App authorization on inherited pipes before any network I/O."""
    if not snapshot.managed:
        return None
    env = os.environ if env is None else env
    try:
        read_fd = int(env['MEDIACRAWLER_ROUTE_ACK_FD'])
        write_fd = int(env['MEDIACRAWLER_PROXY_RECEIPT_FD'])
        if (min(read_fd, write_fd) < 3 or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 < timeout <= 5
                or fcntl.fcntl(read_fd,fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
                or fcntl.fcntl(write_fd,fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY
                or not stat.S_ISFIFO(os.fstat(read_fd).st_mode) or not stat.S_ISFIFO(os.fstat(write_fd).st_mode)):
            _error()
        request = dict(type='route_ack', operation_id=snapshot.operation_id, owner_id=snapshot.owner_id,
            lease_version=snapshot.lease_version, snapshot_digest=snapshot.snapshot_digest,
            context_id=snapshot.context_id)
        if previous_ack is not None:
            request = dict(type='route_renew', operation_id=uuid4().hex, route_operation_id=snapshot.operation_id, owner_id=snapshot.owner_id, lease_version=previous_ack['lease_version'])
        os.write(write_fd, (json.dumps(request, allow_nan=False) + '\n').encode())
        deadline = time.monotonic()+timeout
        raw = bytearray()
        with selectors.DefaultSelector() as poll:
            poll.register(read_fd,selectors.EVENT_READ)
            while len(raw) < 4096:
                remaining=deadline-time.monotonic()
                if remaining <= 0 or not poll.select(remaining):
                    _error('PROXY_UNAVAILABLE')
                chunk = os.read(read_fd, 1)
                if chunk == b'\n':
                    return snapshot.require_managed_ack(json.loads(raw), lease_version=snapshot.lease_version if previous_ack is None else previous_ack['lease_version']+1)
                if not chunk:
                    break
                raw.extend(chunk)
        _error()
    except (KeyError, ValueError, OSError, TypeError):
        _error('PROXY_REVISION_MISMATCH')


class BoundProxySession:
    """A frozen route with one live browser context and a validated receipt."""
    def __init__(self, snapshot, *, echo_url=None, budget_id=None, stop_checker=None,
                 ttl=None, max_calls=None, verify=True, http_client_factory=None,
                 policy=None, maintenance_only=False, trial_id=None, reuse_existing_policy=False,
                 account_policy=None, login_bootstrap=False, ack=None, lease_renewer=None):
        self.snapshot = snapshot
        # Managed routes are unusable until the App's redacted ACK has been
        # validated.  This fail-closed check also prevents callers from
        # falling through to an unmanaged automatic proxy pool.
        if ack is not None and (not isinstance(ack,dict) or type(ack.get('lease_version')) is not int or ack['lease_version'] < snapshot.lease_version):
            _error('PROXY_REVISION_MISMATCH')
        self._lease_ack = self.snapshot.require_managed_ack(ack, lease_version=ack.get('lease_version')) if ack is not None else obtain_route_ack(snapshot)
        self._lease_renewer = lease_renewer
        self._lease_lock = threading.RLock()
        if trial_id is not None and not isinstance(trial_id, str):
            _error()
        self._trial_id = trial_id
        if type(login_bootstrap) is not bool:
            _error()
        effective_trial_id = trial_id if trial_id is not None else os.getenv('MEDIACRAWLER_PROXY_TRIAL_ID', '')
        login_bootstrap = login_bootstrap or snapshot.login_bootstrap
        if login_bootstrap and not effective_trial_id and not (snapshot.managed and snapshot.login_bootstrap):
            _error()
        self._login_bootstrap = login_bootstrap
        if type(maintenance_only) is not bool:
            _error()
        self.maintenance_only = maintenance_only
        self._maintenance_ip = None
        self.echo_url = fixed_echo_url(echo_url or os.getenv('MEDIACRAWLER_PROXY_ECHO_URL', ''))
        try:
            ttl = float(ttl if ttl is not None else os.getenv('MEDIACRAWLER_PROXY_PROBE_TTL', '300'))
            max_calls = int(max_calls if max_calls is not None else os.getenv('MEDIACRAWLER_PROXY_PROBE_MAX_CALLS', '96'))
        except (ValueError, TypeError):
            _error()
        if not math.isfinite(ttl) or not 0 < ttl <= 300 or verify is False:
            _error()
        self.guard = ProbeGuard(snapshot, max_calls=max_calls, ttl=ttl)
        self.budget = SharedProbeBudget(snapshot.scheduler_db_path, budget_id or os.getenv('MEDIACRAWLER_PROXY_PROBE_BUDGET_ID', ''), max_calls=max_calls)
        self.stop_checker = stop_checker
        self.verify = verify
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        if type(reuse_existing_policy) is not bool:
            _error()
        self._reuse_existing_policy = reuse_existing_policy
        if account_policy is not None and (not isinstance(account_policy, dict)
                or set(account_policy) != {'min_interval', 'per_minute'}):
            _error('SCHEDULER_POLICY_CONFLICT')
        self._account_policy = dict(account_policy) if account_policy is not None else None
        self._policy = policy
        self.receipt = None
        self.context = None
        self.context_id = None
        self._probe_lock = asyncio.Lock()
        self._periodic_task = None
        self._stopped = None
        self._on_probe_page = None
        self._gate = None
        self._counter_tasks = set()
        self._page_counts = {}
        self._browser_tokens = {}
        self._browser_opened_at = time.time()
        with self.budget._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS collection_browser_requests (token TEXT PRIMARY KEY,exit_key TEXT NOT NULL,pid INTEGER NOT NULL,started_at REAL NOT NULL,active INTEGER NOT NULL)')
        self._echo_context_handler = None
        self._receipt_delivered = False
        self.trial_limits = None

    def check_stop(self):
        if self.snapshot.managed:
            with self._lease_lock:
                if self._lease_ack['expires_at'] <= time.time() and not self._lease_renewer:
                    _error('PROXY_UNAVAILABLE')
                if self._lease_ack['expires_at'] - time.time() < 30:
                    previous = self._lease_ack
                    renewed = self._lease_renewer(previous) if self._lease_renewer else obtain_route_ack(self.snapshot, previous_ack=previous)
                    if type(renewed.get('lease_version')) is not int or renewed['lease_version'] <= previous['lease_version']:
                        _error('PROXY_REVISION_MISMATCH')
                    self._lease_ack = self.snapshot.require_managed_ack(renewed, lease_version=renewed['lease_version'])
        if self._stopped:
            _error(self._stopped)
        self.snapshot.require_not_expired(allow_unknown=True)
        try:
            with self.budget._connect() as db:
                row = db.execute('SELECT stop_code FROM collection_route_health WHERE proxy_id=? AND config_revision=?', (self._health_key(), self.snapshot.config_revision)).fetchone()
            if row and not self.maintenance_only:
                _error(row[0])
        except sqlite3.Error:
            _error('PROXY_UNAVAILABLE')
        if self.stop_checker:
            result = self.stop_checker()
            if result:
                _error(result if isinstance(result, str) else 'PROXY_UNAVAILABLE')

    def _event(self, code, receipt=None):
        event = dict(schema_version=1, code=code, proxy_id=self.snapshot.proxy_id,
                     config_revision=self.snapshot.config_revision, account_id=self.snapshot.account_id,
                     observed_exit_ip=receipt.ip if receipt else None,
                     observed_at=receipt.observed_at if receipt else time.time())
        print('COLLECTION_ROUTE_EVENT ' + json.dumps(event, separators=(',', ':'), allow_nan=False), flush=True)
        if self.snapshot.managed and code != 'ROUTE_OBSERVED':
            try:
                fd=int(os.environ['MEDIACRAWLER_PROXY_RECEIPT_FD'])
                data=(json.dumps({'type':'route_fault','event':event},allow_nan=False)+'\n').encode()
                os.set_blocking(fd,False)
                os.write(fd,data)
            except (OSError,ValueError,KeyError):
                pass

    def _health_key(self):
        if self.snapshot.route_mode=='direct_degraded':
            return 'service-direct.'+hashlib.sha256(self.snapshot.observed_exit_ip.encode()).hexdigest()[:32]+'.'+str(self.snapshot.exit_generation)
        return self.snapshot.proxy_id

    def fail(self, code):
        error = CollectionRouteError(code)
        self._stopped = error.code
        transport=error.code in {'PROXY_AUTH_FAILED','PROXY_CONNECTION_REFUSED','PROXY_TRANSPORT_FAILED','PROXY_EXPIRED','PROXY_EXIT_CHANGED'}
        if transport or not self.snapshot.managed:
            try:
                with self.budget._connect() as db:
                    db.execute('INSERT OR REPLACE INTO collection_route_health VALUES(?,?,?)',
                               (self._health_key(), self.snapshot.config_revision, error.code))
            except sqlite3.Error:
                pass
            self._event(error.code)
        return error

    async def open_context(self, context, context_id=None, on_probe_page=None):
        self.check_stop()
        if self.context is not None:
            _error()
        self.context, self.context_id = context, self.snapshot.context_id if self.snapshot.managed else context_id or uuid4().hex
        self._on_probe_page = on_probe_page
        if callable(getattr(context, 'route', None)):
            # Chromium's implicit favicon request is context-level; page.route
            # alone does not intercept it. Keep an echo-origin-only deny guard.
            parsed_echo = urlsplit(self.echo_url)
            echo_origin = parsed_echo.scheme + '://' + parsed_echo.netloc
            self._echo_pattern = '**/*'
            async def context_echo_only(route):
                request = route.request
                try:
                    is_probe = getattr(request.frame.page, '_collection_proxy_probe', False)
                except Exception:
                    is_probe = False
                if not is_probe and not request.url.startswith(echo_origin + '/'):
                    if self.snapshot.managed:
                        try:
                            self.check_stop()
                            token = self._reserve_browser_request()
                            self._browser_tokens[id(request)] = token
                        except (CollectionRouteError, RuntimeError, sqlite3.Error) as exc:
                            reason=getattr(self,'_reservation_stop_reason',None) or ('BOUND_REQUEST_GUARD' if isinstance(exc,CollectionRouteError) else 'BOUND_DATABASE_GUARD' if isinstance(exc,sqlite3.Error) else 'BOUND_RUNTIME_GUARD')
                            self.record_browser_guard_stop(reason)
                            await route.abort()
                            self._stop_background()
                            return
                    # Preserve later business CDP/page guards and route handlers.
                    await route.fallback()
                    return
                if not is_probe or request.url != self.echo_url or request.method != 'GET' or request.redirected_from is not None:
                    await route.abort()
                    return
                headers = {key: value for key, value in request.headers.items()
                           if key.lower() not in ('cookie', 'authorization', 'proxy-authorization')}
                await route.continue_(headers=headers)
            self._echo_context_handler = context_echo_only
            await context.route(self._echo_pattern, context_echo_only)
        if callable(getattr(context, 'on', None)):
            context.on('page', self._monitor_page)
            for page in list(context.pages):
                self._monitor_page(page)
        try:
            await self._probe('open')
        except CollectionRouteError as exc:
            self._stopped = exc.code
            raise
        if not self.maintenance_only:
            self._periodic_task = asyncio.create_task(self._periodic_probe())
        return self.receipt

    async def close(self):
        if self.receipt is not None and not self.maintenance_only:
            emit_probe_receipt(self.snapshot, self.receipt, closed=True)
        task, self._periodic_task = self._periodic_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for counter_task in list(self._counter_tasks):
            counter_task.cancel()
        if self._counter_tasks:
            await asyncio.gather(*self._counter_tasks, return_exceptions=True)
        self._counter_tasks.clear()
        self._page_counts.clear()
        # Keep the echo-origin deny guard until the browser actually closes.
        # Removing it first lets a delayed browser favicon escape during teardown.
        self.context = None
        self.receipt = None

    def _spawn_counter(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._counter_tasks.add(task)
        task.add_done_callback(self._counter_tasks.discard)

    def record_browser_guard_stop(self, reason):
        if reason not in BROWSER_GUARD_REASONS or getattr(self,'_browser_guard_first_reason',None):
            return
        self._browser_guard_first_reason=reason
        emit_browser_guard_stopped(self.snapshot,reason)

    def _stop_background(self, reason=None):
        if reason is not None:self.record_browser_guard_stop(reason)
        # Admission, pacing and counter stops are local safety decisions.
        self._stopped='PROXY_UNAVAILABLE'
        if self.context is not None:
            self._spawn_counter(self._close_for_stop())

    async def _close_for_stop(self):
        # Close pages, not context: its patched close owns session task cleanup.
        for page in list(self.context.pages):
            try:
                await page.close()
            except Exception:
                pass

    def _monitor_page(self, page):
        key = id(page)
        if key in self._page_counts:
            return
        state = {'requests': 0, 'bytes': 0, 'times': [], 'opened': time.monotonic()}
        self._page_counts[key] = state

        def request_started(request):
            if getattr(page, '_collection_proxy_probe', False):
                return
            now = time.monotonic()
            state['requests'] += 1
            state['times'] = [stamp for stamp in state['times'] if stamp > now - 60]
            state['times'].append(now)
            rolling_limit = 300 if self._login_bootstrap and now - state['opened'] < 60 else 120
            if state['requests'] > 300 or len(state['times']) > rolling_limit or now - state['opened'] > 1200:
                self._stop_background('PAGE_REQUEST_LIMIT' if state['requests']>300 else 'PAGE_ROLLING_LIMIT' if len(state['times'])>rolling_limit else 'PAGE_LIFETIME_LIMIT')

        async def request_finished(request):
            if getattr(page, '_collection_proxy_probe', False):
                return
            try:
                sizes = await request.sizes()
                state['bytes'] += sizes['responseBodySize'] + sizes['responseHeadersSize']
                if state['bytes'] > 50 * 1024 * 1024:
                    self._stop_background('PAGE_BYTE_LIMIT')
            except Exception:
                # A byte counter that cannot be maintained isn't a safe run.
                self._stop_background('PAGE_SIZES_FAILED')

        async def lifetime():
            await asyncio.sleep(1200)
            if not getattr(page, '_collection_proxy_probe', False) and not page.is_closed():
                self._stop_background('PAGE_LIFETIME_LIMIT')

        if callable(getattr(page, 'on', None)):
            page.on('request', request_started)
            page.on('requestfinished', lambda request: (self._release_browser_request(request), self._spawn_counter(request_finished(request))))
            page.on('requestfailed', self._release_browser_request)
            self._spawn_counter(lifetime())

    def _reserve_browser_request(self):
        """Fence automatic browser traffic and bound shared-exit concurrency."""
        from tools.persistent_request_gate import RequestScheduler
        now = time.time()
        self._reservation_stop_reason=None
        fence = self._safety_fence()
        key = self.snapshot.observed_exit_ip
        token = uuid4().hex
        with self.budget._connect() as db:
            if fence:
                db.execute('ATTACH DATABASE ? AS safety', (fence['db_path'],))
            db.execute('BEGIN IMMEDIATE')
            if fence:
                row = db.execute('SELECT generation,observed_at,paused_until,pause_reason FROM safety.crawler_shared_exits WHERE exit_key=?', (key,)).fetchone()
                admission = db.execute('SELECT account_id,exit_key,generation,state FROM safety.crawler_route_admissions WHERE operation_id=?', (self.snapshot.operation_id,)).fetchone()
                account = db.execute('SELECT recovery_claim,recovery_state FROM safety.crawler_accounts WHERE id=?',(self.snapshot.account_id,)).fetchone()
                recovery = bool(account and self.snapshot.recovery_claim and account[0] == self.snapshot.recovery_claim and account[1] in {'claimed','manual_claimed'})
                if recovery and account[1]=='manual_claimed':
                    recovery=bool(db.execute("SELECT 1 FROM safety.crawler_manual_recovery_operations WHERE account_id=? AND claim=? AND owner_id=? AND state='claimed'",(self.snapshot.account_id,self.snapshot.recovery_claim,self.snapshot.owner_id)).fetchone())
                if (not row or row[0] != self.snapshot.exit_generation or (row[3] and not recovery)
                        or not 0 <= now-row[1] <= 300 or (row[2] is not None and row[2] > now and not (recovery and account[1]=='manual_claimed'))
                        or not admission or tuple(admission) != (self.snapshot.account_id,key,self.snapshot.exit_generation,'active')):
                    self._reservation_stop_reason='SHARED_ADMISSION_GUARD'
                    _error('PROXY_UNAVAILABLE')
            for scope in self.snapshot.scope_ids:
                cooldown=db.execute('SELECT cooldown_until FROM request_scheduler_state WHERE platform=?',(scope,)).fetchone()
                if cooldown and cooldown[0]>now:
                    self._reservation_stop_reason='SHARED_COOLDOWN_GUARD'
                    _error('PROXY_UNAVAILABLE')
            for previous, pid in db.execute('SELECT token,pid FROM collection_browser_requests WHERE active=1').fetchall():
                if not RequestScheduler._alive(pid):
                    db.execute('UPDATE collection_browser_requests SET active=0 WHERE token=?',(previous,))
            db.execute('DELETE FROM collection_browser_requests WHERE active=0 AND started_at<?',(now-60,))
            count, active = db.execute('SELECT COUNT(*),COALESCE(SUM(active),0) FROM collection_browser_requests WHERE exit_key=? AND (started_at>? OR active=1)',(key,now-60)).fetchone()
            rolling, concurrent = (40,16) if self.snapshot.route_mode == 'direct_degraded' else (120,32)
            if self._login_bootstrap and now-self._browser_opened_at < 60:
                rolling, concurrent = 300,32
            if count >= rolling or active >= concurrent:
                self._reservation_stop_reason='SHARED_REQUEST_LIMIT' if count>=rolling else 'SHARED_ACTIVE_LIMIT'
                _error('PROBE_BUDGET_EXHAUSTED')
            db.execute('INSERT INTO collection_browser_requests VALUES(?,?,?,?,1)',(token,key,os.getpid(),now))
        return token

    def _release_browser_request(self, request):
        token = self._browser_tokens.pop(id(request),None)
        if token:
            with self.budget._connect() as db:
                db.execute('UPDATE collection_browser_requests SET active=0 WHERE token=?',(token,))

    async def _periodic_probe(self):
        try:
            while True:
                pause = self.guard.ttl
                if self.snapshot.managed:
                    pause = min(pause,max(1,self._lease_ack['expires_at']-time.time()-25))
                await asyncio.sleep(pause)
                await self._probe('tick')
        except asyncio.CancelledError:
            raise
        except CollectionRouteError as exc:
            self._stopped = exc.code
            if self.context:
                self.record_browser_guard_stop('PERIODIC_PROBE_GUARD')
                await self.context.close()

    async def _browser_sample(self):
        previous_pending = getattr(self.context, '_collection_proxy_probe_pending', False)
        self.context._collection_proxy_probe_pending = True
        try:
            page = await self.context.new_page()
            page._collection_proxy_probe = True
        except Exception:
            raise CollectionRouteError('PROXY_UNAVAILABLE') from None
        finally:
            self.context._collection_proxy_probe_pending = previous_pending
        cdp = None
        try:
            if callable(getattr(self.context, 'new_cdp_session', None)):
                cdp = await self.context.new_cdp_session(page)
                await cdp.send('Network.enable')
                parsed_echo = urlsplit(self.echo_url)
                await cdp.send('Network.setBlockedURLs', {'urls': [
                    parsed_echo.scheme + '://' + parsed_echo.netloc + '/favicon.ico']})
            # Mark the page before any navigation so business document guards can
            # explicitly identify this page without widening business allowlists.
            page._collection_proxy_probe = True
            if self._on_probe_page:
                result = self._on_probe_page(page)
                if hasattr(result, '__await__'):
                    await result

            async def echo_only(route):
                request = route.request
                # Exact URL + no redirects; abort subresources and background JS.
                if request.url != self.echo_url or request.method != 'GET' or request.redirected_from is not None:
                    await route.abort()
                    return
                headers = {key: value for key, value in request.headers.items()
                           if key.lower() not in ('cookie', 'authorization', 'proxy-authorization')}
                await route.continue_(headers=headers)
            await page.route('**/*', echo_only)
            # A real browser DOM fetch from about:blank avoids registering echo
            # as a visited storage origin. Playwright storage_state internally
            # navigates closed origins and otherwise triggers an unguarded favicon.
            # Echo must explicitly allow CORS from null; no security bypass/API
            # request context substitute is used.
            sample = await asyncio.wait_for(page.evaluate("""async url => {
                const response = await fetch(url, {mode: 'cors', credentials: 'omit',
                    redirect: 'error', cache: 'no-store'});
                if (response.status !== 200) return {status: response.status, body: ''};
                const reader = response.body.getReader();
                const chunks = []; let size = 0;
                while (true) {
                    const chunk = await reader.read();
                    if (chunk.done) break;
                    size += chunk.value.byteLength;
                    if (size > 1024) { await reader.cancel(); return {status: 0, body: ''}; }
                    chunks.push(chunk.value);
                }
                const body = new Uint8Array(size); let offset = 0;
                for (const chunk of chunks) {body.set(chunk, offset); offset += chunk.byteLength;}
                return {status: response.status, body: new TextDecoder('utf-8', {fatal: true}).decode(body)};
            }""", self.echo_url), timeout=10)
            if not isinstance(sample, dict) or set(sample) != {'status', 'body'} or sample['status'] != 200:
                _error('PROXY_AUTH_FAILED' if isinstance(sample, dict) and sample.get('status') == 407 else 'PROXY_UNAVAILABLE')
            return parse_exit_sample(sample['body'])
        except CollectionRouteError:
            raise
        except Exception as exc:
            # Chromium reports rejected proxy credentials before a Response.
            # Match only documented network auth codes, not generic tunnel errors.
            text = str(exc)
            auth_failed = any(code in text for code in (
                'ERR_INVALID_AUTH_CREDENTIALS', 'ERR_PROXY_AUTH_REQUESTED',
                'ERR_PROXY_AUTH_UNSUPPORTED', 'ERR_MISSING_AUTH_CREDENTIALS'))
            raise CollectionRouteError('PROXY_AUTH_FAILED' if auth_failed else 'PROXY_UNAVAILABLE') from None
        finally:
            try:
                await page.close()
            except Exception:
                pass  # Outer context owner closes it; preserve safe probe error.
            if cdp is not None:
                try:
                    await cdp.detach()
                except Exception:
                    pass  # Target is already closed; blocker remained until close.

    async def _http_sample(self):
        try:
            async with self._http_client_factory(proxy=self.snapshot.httpx_proxy(), trust_env=False,
                                                 verify=self.verify, follow_redirects=False, timeout=10) as client:
                response = await client.get(self.echo_url, headers={
                    'Accept': 'application/json,text/plain',
                    'User-Agent': 'ZhanyunProxyProbe/1.0',
                })
            if response.status_code != 200:
                _error('PROXY_AUTH_FAILED' if response.status_code == 407 else 'PROXY_UNAVAILABLE')
            return parse_exit_sample(response.content)
        except CollectionRouteError:
            raise
        except httpx.HTTPError as exc:
            raise CollectionRouteError(transport_error_code(exc, managed=self.snapshot.managed)) from None
        except Exception:
            raise CollectionRouteError('PROXY_UNAVAILABLE') from None

    async def _probe(self, category):
        try:
            return await self._probe_locked(category)
        except CollectionRouteError as exc:
            self.fail(exc.code)
            raise

    async def _probe_locked(self, category):
        async with self._probe_lock:
            self.check_stop()
            if self.context is None:
                _error('PROXY_UNAVAILABLE')
            pairs = self.budget.reserve(category, use_remaining=self._login_bootstrap and category == 'open')
            for _ in range(pairs):
                # Shared budget is authority; per-process guard mirrors this usage.
                self.guard.reserve_pair()
                try:
                    browser_ip = await self._browser_sample()
                except CollectionRouteError as browser_error:
                    if browser_error.code=='PROXY_AUTH_FAILED' and not self.snapshot.managed:
                        raise browser_error from None
                    if browser_error.code in {'PROXY_UNAVAILABLE','PROXY_AUTH_FAILED'}:
                        # A generic Chromium HTTP failure doesn't identify407.
                        # Use the already-reserved pair's HTTP attempt for exact
                        # diagnosis; never relabel generic tunnel failures auth.
                        try:
                            await self._http_sample()
                        except CollectionRouteError as http_error:
                            if http_error.code in {'PROXY_AUTH_FAILED', 'PROXY_CONNECTION_REFUSED', 'PROXY_TRANSPORT_FAILED'}:
                                raise http_error from None
                        if browser_error.code == 'PROXY_AUTH_FAILED':
                            raise CollectionRouteError('PROXY_UNAVAILABLE') from None
                    raise browser_error from None
                http_ip = await self._http_sample()
                if self.maintenance_only:
                    if browser_ip != http_ip or (self._maintenance_ip is not None and self._maintenance_ip != browser_ip):
                        _error('PROXY_EXIT_CHANGED')
                    self._maintenance_ip = browser_ip
                    receipt = ProbeReceipt(self.snapshot.proxy_id, self.snapshot.config_revision,
                                           self.context_id, browser_ip, time.time(),
                                           self.snapshot.exit_generation)
                    # Observe safely without resetting historical baseline/stop.
                    event_code = ('PROXY_EXIT_CHANGED' if self.snapshot.observed_exit_ip is not None
                                  and self.snapshot.observed_exit_ip != browser_ip else 'ROUTE_OBSERVED')
                    self._event(event_code, receipt)
                else:
                    receipt = self.guard.confirm_pair(self.context_id, browser_ip, http_ip)
                    self.budget.confirm_baseline(self.snapshot, receipt.ip)
                    self._event('ROUTE_OBSERVED', receipt)
                    self._receipt_delivered = emit_probe_receipt(self.snapshot, receipt)
                self.receipt = receipt
            if not self.maintenance_only and self.snapshot.expires_at is not None:
                self._gate = self._make_gate()
                if self.trial_limits is None:
                    from tools.trial_limits import from_env
                    try:
                        trial_env = None if self._trial_id is None else {
                            **os.environ, 'MEDIACRAWLER_PROXY_TRIAL_ID': self._trial_id}
                        self.trial_limits = from_env(self.snapshot.scheduler_db_path, env=trial_env)
                    except RuntimeError:
                        _error('PROBE_BUDGET_EXHAUSTED')
            return self.receipt

    def _safety_fence(self):
        if not self.snapshot.safety_db_path:
            return None
        return dict(db_path=self.snapshot.safety_db_path, exit_ip=self.snapshot.observed_exit_ip,
            account_id=self.snapshot.account_id, generation=self.snapshot.exit_generation,
            operation_id=self.snapshot.operation_id, ttl=300, recovery_claim=self.snapshot.recovery_claim, owner_id=self.snapshot.owner_id)

    def _make_gate(self):
        from tools.persistent_request_gate import RequestScheduler, MultiScopeGate, configured_gate
        if self._reuse_existing_policy:
            scopes = self.guard.require_business(self.receipt, self.context_id)
            try:
                with self.budget._connect() as db:
                    rows = dict(db.execute('SELECT platform,limits_json FROM request_scheduler_policy'))
                if 'dy' not in rows:
                    _error('SCHEDULER_POLICY_CONFLICT')
                baseline = json.loads(rows['dy'])
                if not isinstance(baseline, list) or len(baseline) != 4:
                    _error('SCHEDULER_POLICY_CONFLICT')
                for scope in scopes:
                    if scope in rows:
                        limits = json.loads(rows[scope])
                    elif scope.startswith('dy:service_exit:') and self.snapshot.route_mode=='direct_degraded':
                        limits=[max(9.0,baseline[0]),min(6,baseline[1]),baseline[2],baseline[3]]
                    elif scope.startswith('dy:account:'):
                        # A new account may have stricter slow-mode limits than
                        # globaldy. Never silently assign it the global quota.
                        fallback = self._account_policy
                        if (fallback is None or type(fallback['per_minute']) is not int
                                or fallback['per_minute'] < 1
                                or type(fallback['min_interval']) not in (int, float)
                                or not math.isfinite(fallback['min_interval'])
                                or fallback['min_interval'] < 0):
                            _error('SCHEDULER_POLICY_CONFLICT')
                        limits = [max(baseline[0], fallback['min_interval']),
                                  min(baseline[1], fallback['per_minute']), baseline[2], baseline[3]]
                    else:
                        limits = baseline
                    if not isinstance(limits, list) or len(limits) != 4:
                        _error('SCHEDULER_POLICY_CONFLICT')
                    RequestScheduler(self.snapshot.scheduler_db_path, platform=scope,
                        min_interval=limits[0], per_minute=limits[1], max_concurrency=limits[2],
                        media_interval=limits[3], replace_policy=False)
                return MultiScopeGate(self.snapshot.scheduler_db_path, scopes,
                                      stop_checker=self.check_stop, safety_fence=self._safety_fence())
            except (ValueError, sqlite3.Error, TypeError, OverflowError):
                raise CollectionRouteError('SCHEDULER_POLICY_CONFLICT') from None
        if self._policy is None:
            import config
            try:
                # Reuse the existing application's conservative env overrides;
                # snapshot DB is authoritative, existing dy policy is not erased.
                global_state = configured_gate(db_path=self.snapshot.scheduler_db_path,
                    min_interval=config.DY_REQUEST_MIN_INTERVAL, per_minute=config.DY_REQUESTS_PER_MINUTE,
                    max_concurrency=config.DY_REQUEST_CONCURRENCY, media_interval=config.DY_MEDIA_REQUEST_INTERVAL,
                    cooldown_seconds=config.DY_REQUEST_COOLDOWN_SECONDS, jitter=config.DY_PACING_JITTER).state
            except (ValueError, sqlite3.Error):
                raise CollectionRouteError('SCHEDULER_POLICY_CONFLICT') from None
            policy = dict(min_interval=global_state.min_interval, per_minute=global_state.per_minute,
                          max_concurrency=global_state.max_concurrency, media_interval=global_state.media_interval,
                          cooldown_seconds=global_state.cooldown_seconds)
            account_policy = {**policy, 'min_interval': config.DY_ACCOUNT_MIN_INTERVAL,
                              'per_minute': config.DY_ACCOUNT_REQUESTS_PER_MINUTE}
        else:
            policy = dict(self._policy)
            account_policy = policy
        scopes = self.guard.require_business(self.receipt, self.context_id)
        if self.snapshot.route_mode == 'direct_degraded':
            account_policy = {**account_policy, 'min_interval': max(9.0, account_policy['min_interval']), 'per_minute': min(6, account_policy['per_minute'])}
        try:
            for scope in scopes:
                scope_policy = account_policy if scope.startswith('dy:account:') else policy
                if self.snapshot.route_mode=='direct_degraded' and scope.startswith('dy:service_exit:'):
                    scope_policy={**policy,'min_interval':max(9.0,policy['min_interval']),'per_minute':min(6,policy['per_minute'])}
                RequestScheduler(self.snapshot.scheduler_db_path, platform=scope,
                                 **scope_policy, replace_policy=scope.startswith('dy:account:'))
        except (ValueError, sqlite3.Error):
            raise CollectionRouteError('SCHEDULER_POLICY_CONFLICT') from None
        return MultiScopeGate(self.snapshot.scheduler_db_path, scopes, stop_checker=self.check_stop,
                              cooldown_seconds=policy['cooldown_seconds'], safety_fence=self._safety_fence())

    async def ensure_business(self):
        if self.maintenance_only:
            _error('PROXY_UNAVAILABLE')
        self.check_stop()
        self.snapshot.require_not_expired()
        if self.receipt is None or self.context is None:
            _error('PROXY_UNAVAILABLE')
        if time.time() - self.receipt.observed_at >= self.guard.ttl:
            await self._probe('boundary')
        self.guard.require_business(self.receipt, self.context_id)
        return self._gate

    @asynccontextmanager
    async def slot(self, operation='api', *, timeout=60, max_wait=300):
        gate = await self.ensure_business()
        try:
            async with gate.slot(operation, timeout=timeout, max_wait=max_wait) as waited:
                self.check_stop()
                self.guard.require_business(self.receipt, self.context_id)
                if self.trial_limits is not None:
                    try:
                        self.trial_limits.reserve_operation()
                    except RuntimeError:
                        raise self.fail('PROBE_BUDGET_EXHAUSTED') from None
                yield waited
        except RuntimeError as exc:
            if str(exc)=='ACCOUNT_CHALLENGE':
                raise CollectionRouteError('PROXY_UNAVAILABLE') from None
            raise

    def enter_cooldown(self, reason, seconds=None):
        if self.maintenance_only:
            _error('PROXY_UNAVAILABLE')
        self.check_stop()
        if self._gate is None:
            _error('PROXY_UNAVAILABLE')
        self._gate.enter_cooldown(reason, seconds)

    def provenance(self, *, observation_id, source_task_id, source_batch_id, collected_at=None):
        if self.maintenance_only:
            _error('PROXY_UNAVAILABLE')
        self.check_stop()
        return collection_route_provenance(self.snapshot, self.receipt, observation_id=observation_id,
                                          source_task_id=source_task_id, source_batch_id=source_batch_id,
                                          collected_at=time.time() if collected_at is None else collected_at,
                                          context_id=self.context_id)
