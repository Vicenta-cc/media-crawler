"""Fixed collection route, isolated exit probes and shared finite echo budget."""
import asyncio
import fcntl
import stat
import json
import math
import os
import sqlite3
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


class BoundProxySession:
    """A frozen route with one live browser context and a validated receipt."""
    def __init__(self, snapshot, *, echo_url=None, budget_id=None, stop_checker=None,
                 ttl=None, max_calls=None, verify=True, http_client_factory=None,
                 policy=None, maintenance_only=False, trial_id=None, reuse_existing_policy=False,
                 account_policy=None, login_bootstrap=False):
        self.snapshot = snapshot
        if trial_id is not None and not isinstance(trial_id, str):
            _error()
        self._trial_id = trial_id
        if type(login_bootstrap) is not bool:
            _error()
        effective_trial_id = trial_id if trial_id is not None else os.getenv('MEDIACRAWLER_PROXY_TRIAL_ID', '')
        if login_bootstrap and not effective_trial_id:
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
        self._echo_context_handler = None
        self._receipt_delivered = False
        self.trial_limits = None

    def check_stop(self):
        if self._stopped:
            _error(self._stopped)
        self.snapshot.require_not_expired(allow_unknown=True)
        try:
            with self.budget._connect() as db:
                row = db.execute('SELECT stop_code FROM collection_route_health WHERE proxy_id=? AND config_revision=?', (self.snapshot.proxy_id, self.snapshot.config_revision)).fetchone()
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

    def fail(self, code):
        error = CollectionRouteError(code)
        self._stopped = error.code
        try:
            with self.budget._connect() as db:
                db.execute('INSERT OR REPLACE INTO collection_route_health VALUES(?,?,?)',
                           (self.snapshot.proxy_id, self.snapshot.config_revision, error.code))
        except sqlite3.Error:
            pass  # Local sticky stop still applies; public failure is unchanged.
        self._event(error.code)
        return error

    async def open_context(self, context, context_id=None, on_probe_page=None):
        self.check_stop()
        if self.context is not None:
            _error()
        self.context, self.context_id = context, context_id or uuid4().hex
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

    def _stop_background(self):
        self.fail('PROXY_UNAVAILABLE')
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
                self._stop_background()

        async def request_finished(request):
            if getattr(page, '_collection_proxy_probe', False):
                return
            try:
                sizes = await request.sizes()
                state['bytes'] += sizes['responseBodySize'] + sizes['responseHeadersSize']
                if state['bytes'] > 50 * 1024 * 1024:
                    self._stop_background()
            except Exception:
                # A byte counter that cannot be maintained isn't a safe run.
                self._stop_background()

        async def lifetime():
            await asyncio.sleep(1200)
            if not getattr(page, '_collection_proxy_probe', False) and not page.is_closed():
                self._stop_background()

        if callable(getattr(page, 'on', None)):
            page.on('request', request_started)
            page.on('requestfinished', lambda request: self._spawn_counter(request_finished(request)))
            self._spawn_counter(lifetime())

    async def _periodic_probe(self):
        try:
            while True:
                await asyncio.sleep(self.guard.ttl)
                await self._probe('tick')
        except asyncio.CancelledError:
            raise
        except CollectionRouteError as exc:
            self._stopped = exc.code
            if self.context:
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
        except httpx.ProxyError as exc:
            raise CollectionRouteError('PROXY_AUTH_FAILED' if '407' in str(exc) else 'PROXY_UNAVAILABLE') from None
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
                    if browser_error.code == 'PROXY_UNAVAILABLE':
                        # A generic Chromium HTTP failure doesn't identify407.
                        # Use the already-reserved pair's HTTP attempt for exact
                        # diagnosis; never relabel generic tunnel failures auth.
                        try:
                            await self._http_sample()
                        except CollectionRouteError as http_error:
                            if http_error.code == 'PROXY_AUTH_FAILED':
                                raise http_error from None
                    raise browser_error from None
                http_ip = await self._http_sample()
                if self.maintenance_only:
                    if browser_ip != http_ip or (self._maintenance_ip is not None and self._maintenance_ip != browser_ip):
                        _error('PROXY_EXIT_CHANGED')
                    self._maintenance_ip = browser_ip
                    receipt = ProbeReceipt(self.snapshot.proxy_id, self.snapshot.config_revision,
                                           self.context_id, browser_ip, time.time())
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
                                      stop_checker=self.check_stop)
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
        try:
            for scope in scopes:
                RequestScheduler(self.snapshot.scheduler_db_path, platform=scope,
                                 **(account_policy if scope.startswith('dy:account:') else policy),
                                 replace_policy=scope.startswith('dy:account:'))
        except (ValueError, sqlite3.Error):
            raise CollectionRouteError('SCHEDULER_POLICY_CONFLICT') from None
        return MultiScopeGate(self.snapshot.scheduler_db_path, scopes, stop_checker=self.check_stop,
                              cooldown_seconds=policy['cooldown_seconds'])

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
        async with gate.slot(operation, timeout=timeout, max_wait=max_wait) as waited:
            self.check_stop()
            self.guard.require_business(self.receipt, self.context_id)
            if self.trial_limits is not None:
                try:
                    self.trial_limits.reserve_operation()
                except RuntimeError:
                    raise self.fail('PROBE_BUDGET_EXHAUSTED') from None
            yield waited

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
