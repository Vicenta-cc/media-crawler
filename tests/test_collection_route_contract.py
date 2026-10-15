import importlib.util
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('collection_route_test_module', Path(__file__).parents[1] / 'tools' / 'collection_route.py')
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)


def envelope(**overrides):
    value = {
        'schema_version': 1, 'route_mode': 'proxy_required', 'platform': 'dy',
        'account_id': 'acct-1', 'profile_key': route.derive_profile_key('acct-1'),
        'proxy_id': 'proxy-1', 'binding_revision': 1, 'config_revision': 2,
        'endpoint': {'scheme': 'http', 'host': 'proxy.example', 'port': 8080},
        'credentials': {'username': 'u#/?@:', 'password': '密#/?@:'},
        'expires_at': time.time()+3600, 'observed_exit': None,
        'scheduler': {'db_path': '/tmp/scheduler.sqlite', 'scope_ids': list(route.derive_scope_ids('dy', 'acct-1', 'proxy-1'))},
    }
    value.update(overrides)
    observed = value['observed_exit']
    if observed is not None and 'scheduler' not in overrides:
        value['scheduler']['scope_ids'] = list(route.derive_scope_ids('dy', 'acct-1', 'proxy-1', observed['ip']))
    return value


def pipe_payload(payload, *, close_write=True):
    r, w = os.pipe()
    def write():
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(w, payload[offset:])
        except BrokenPipeError:
            pass
        finally:
            if close_write:
                os.close(w)
    threading.Thread(target=write, daemon=True).start()
    return r, w


def test_valid_fd_is_read_only_and_formats_credentials_without_secret_repr():
    r, _ = pipe_payload(json.dumps(envelope(), ensure_ascii=False).encode())
    snap = route.load_route_from_fd(r, account_id='acct-1')
    assert snap.playwright_proxy()['server'] == 'http://proxy.example:8080'
    assert snap.httpx_proxy().startswith('http://u%23%2F%3F%40%3A:')
    assert '%E5%AF%86' in snap.httpx_proxy()
    assert '密' not in repr(snap)


def test_fd_envelope_rejects_unknown_version_field_before_factory():
    bad = envelope(extra='x')
    r, _ = pipe_payload(json.dumps(bad).encode())
    with pytest.raises(route.CollectionRouteError) as error:
        route.load_route_from_fd(r, account_id='acct-1')
    assert error.value.code == 'PROXY_CONFIG_INVALID'


def test_fd_empty_eof_timeout_and_oversize_fail_closed():
    r, w = os.pipe()
    os.close(w)
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(r, account_id='acct-1', timeout=.1)

    r, w = os.pipe()
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(r, account_id='acct-1', timeout=.05)
    os.close(w)

    payload = b'{' + b'a' * route.MAX_ENVELOPE_BYTES
    r, w = pipe_payload(payload)
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(r, account_id='acct-1')


def test_fd_timeout_does_not_wait_longer_than_five_seconds():
    r, w = os.pipe()
    started = time.monotonic()
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(r, account_id='acct-1', timeout=.05)
    assert time.monotonic() - started < 1
    os.close(w)


def test_proxy_required_missing_fd_fails_legacy_missing_fd_is_none():
    assert route.load_route_from_env({}, account_id='acct-1', proxy_required=False) is None
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_env({}, account_id='acct-1', proxy_required=True)


def test_identity_and_profile_scope_derivation():
    assert route.derive_profile_key('acct-1') == route.derive_profile_key('acct-1', 'dy')
    assert route.derive_scope_ids('dy', 'acct-1', 'proxy-1') == (
        'dy', 'dy:account:acct-1', 'dy:proxy:proxy-1')
    r, _ = pipe_payload(json.dumps(envelope()).encode())
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(r, account_id='other')


def test_probe_guard_requires_matching_pair_and_ttl():
    base = envelope(observed_exit={'ip': '203.0.113.4', 'observed_at': 100.0})
    r, _ = pipe_payload(json.dumps(base).encode())
    snap = route.load_route_from_fd(r, account_id='acct-1')
    guard = route.ProbeGuard(snap, max_calls=2)
    guard.reserve_pair()
    receipt = guard.confirm_pair('ctx-1', '203.0.113.4', '203.0.113.4', observed_at=101)
    assert guard.require_business(receipt, 'ctx-1', now=102)
    with pytest.raises(route.CollectionRouteError) as error:
        guard.require_business(receipt, 'ctx-1', now=500)
    assert error.value.code == 'PROXY_UNAVAILABLE'
    with pytest.raises(route.CollectionRouteError) as error:
        guard.confirm_pair('ctx-2', '203.0.113.4', '203.0.113.5', observed_at=103)
    assert error.value.code == 'PROXY_EXIT_CHANGED'


def test_probe_budget_is_atomic_and_exhaustion_is_public_code():
    r, _ = pipe_payload(json.dumps(envelope()).encode())
    snap = route.load_route_from_fd(r, account_id='acct-1')
    guard = route.ProbeGuard(snap, max_calls=2)
    guard.reserve_pair()
    with pytest.raises(route.CollectionRouteError) as error:
        guard.reserve_pair()
    assert error.value.code == 'PROBE_BUDGET_EXHAUSTED'


def test_public_provenance_is_exact_and_secret_free():
    r, _ = pipe_payload(json.dumps(envelope()).encode())
    snap = route.load_route_from_fd(r, account_id='acct-1')
    guard = route.ProbeGuard(snap)
    guard.reserve_pair()
    receipt = guard.confirm_pair('ctx-1', '203.0.113.4', '203.0.113.4', observed_at=100)
    public = route.collection_route_provenance(snap, receipt, observation_id='obs-1', source_task_id='task-1', source_batch_id='batch-1', collected_at=101, context_id='ctx-1')
    assert set(public) == route.PROVENANCE_FIELDS
    assert 'password' not in public and '密' not in repr(public)


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(schema_version=True),
    lambda v: v.update(schema_version=2),
    lambda v: v.update(profile_key='wrong'),
    lambda v: v.update(binding_revision=0),
    lambda v: v['endpoint'].update(scheme='https'),
    lambda v: v['endpoint'].update(host='a@evil'),
    lambda v: v['endpoint'].update(port=True),
    lambda v: v['scheduler'].update(scope_ids=['dy']),
    lambda v: v['credentials'].update(extra='secret'),
])
def test_invalid_contract_uses_safe_error(mutation):
    value = envelope()
    mutation(value)
    with pytest.raises(route.CollectionRouteError) as error:
        route.validate_route_envelope(value, account_id='acct-1')
    assert str(error.value) == repr(error.value.args[0]).strip("'") == 'PROXY_CONFIG_INVALID'
    assert '密' not in repr(error.value)


@pytest.mark.parametrize('payload', [b'', b'\xff', b'{', b'{"schema_version":1,"schema_version":2}', b'[]'])
def test_malformed_fd_is_closed(payload):
    r, _ = pipe_payload(payload)
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(r, account_id='acct-1')
    with pytest.raises(OSError):
        os.fstat(r)


def test_writable_fd_and_regular_file_rejected(tmp_path):
    r, w = os.pipe()
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(w, account_id='acct-1')
    os.close(r)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(envelope()))
    fd = os.open(path, os.O_RDONLY)
    with pytest.raises(route.CollectionRouteError):
        route.load_route_from_fd(fd, account_id='acct-1')


def test_true_inherited_readonly_fd_no_secret_in_env_argv_ack():
    import subprocess
    import sys
    r, w = os.pipe()
    payload = json.dumps(envelope(), ensure_ascii=False).encode()
    program = '''import importlib.util, os, json
spec=importlib.util.spec_from_file_location("route_child",os.environ["CONTRACT_PATH"])
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fd=int(os.environ[m.PROXY_CONFIG_FD_ENV])
try:
    os.write(fd,b"x")
    raise AssertionError("writable config")
except OSError:
    pass
snap=m.load_route_from_env(account_id="acct-1",proxy_required=True)
print(json.dumps({"schema_version":1,"proxy_id":snap.proxy_id,"code":"OK"}))
'''
    env = {**os.environ, route.PROXY_CONFIG_FD_ENV: str(r), 'CONTRACT_PATH': str(Path(route.__file__).resolve())}
    child = subprocess.Popen([sys.executable, '-c', program], env=env, pass_fds=(r,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(r)
    os.write(w, payload)
    os.close(w)
    output, errors = child.communicate(timeout=5)
    assert child.returncode == 0, errors.decode()
    assert json.loads(output)['code'] == 'OK'
    assert '密' not in repr(env) + program + output.decode() + errors.decode()


def test_serialization_roundtrip_immutable_and_ipv6():
    value = envelope(endpoint={'scheme':'http','host':'2001:db8::1','port':8080})
    snap = route.validate_route_envelope(value, account_id='acct-1')
    assert snap.proxy_server == 'http://[2001:db8::1]:8080'
    with pytest.raises(Exception):
        snap.proxy_id = 'other'
    r, _ = pipe_payload(route.serialize_route(snap))
    assert route.load_route_from_fd(r, account_id='acct-1') == snap
    assert route.canonical_exit_ip('2001:0db8::1') == '2001:db8::1'


def test_unknown_exit_requires_receipt_and_context_reopen_rejects_old_receipt():
    snap = route.validate_route_envelope(envelope(), account_id='acct-1')
    guard = route.ProbeGuard(snap)
    with pytest.raises(route.CollectionRouteError):
        guard.require_business(None, 'ctx-1', now=100)
    guard.reserve_pair()
    receipt = guard.confirm_pair('ctx-1', '203.0.113.4', '203.0.113.4', observed_at=100)
    assert 'dy:exit:203.0.113.4' in guard.require_business(receipt, 'ctx-1', now=100)
    with pytest.raises(route.CollectionRouteError):
        guard.require_business(receipt, 'ctx-2', now=100)
    with pytest.raises(route.CollectionRouteError) as error:
        guard.confirm_pair('ctx-2', '203.0.113.5', '203.0.113.5', observed_at=101)
    assert error.value.code == 'PROXY_EXIT_CHANGED'


def high_read_pipe():
    import fcntl
    import resource
    minimum = 1100
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < minimum + 1:
        assert hard == resource.RLIM_INFINITY or hard > minimum, 'host cannot exercise required high FD contract'
        resource.setrlimit(resource.RLIMIT_NOFILE, (minimum + 16, hard))
    r, w = os.pipe()
    try:
        high = fcntl.fcntl(r, fcntl.F_DUPFD_CLOEXEC, minimum)
    finally:
        os.close(r)
    assert high >= 1100
    return high, w


def test_actual_inherited_high_fd_pipe_roundtrip():
    import subprocess
    import sys
    high, w = high_read_pipe()
    snap = route.validate_route_envelope(envelope(), account_id='acct-1')
    program = '''import importlib.util,os,json
spec=importlib.util.spec_from_file_location("high_route",os.environ["CONTRACT_PATH"])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
fd=int(os.environ[m.PROXY_CONFIG_FD_ENV])
assert fd>=1100
snap=m.load_route_from_env(account_id="acct-1",proxy_required=True)
print(json.dumps({"schema_version":snap.schema_version,"proxy_id":snap.proxy_id,"code":"OK"}))
'''
    env = {**os.environ, route.PROXY_CONFIG_FD_ENV: str(high), 'CONTRACT_PATH': str(Path(route.__file__).resolve())}
    child = subprocess.Popen([sys.executable, '-c', program], env=env, pass_fds=(high,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.close(high)
    try:
        os.write(w, route.serialize_route(snap))
    finally:
        os.close(w)
    output, errors = child.communicate(timeout=5)
    assert child.returncode == 0, errors.decode()
    assert json.loads(output) == {'schema_version': 1, 'proxy_id': 'proxy-1', 'code': 'OK'}
    assert '密' not in output.decode() + errors.decode()


@pytest.mark.parametrize('mode', ['half_packet_timeout', 'truncated_eof'])
def test_high_fd_half_packet_timeout_and_truncated_eof_keep_safe_error(mode):
    high, w = high_read_pipe()
    os.write(w, b'{"schema_version":1,')
    if mode == 'truncated_eof':
        os.close(w)
    started = time.monotonic()
    try:
        with pytest.raises(route.CollectionRouteError) as error:
            route.load_route_from_fd(high, account_id='acct-1', timeout=.05)
        assert error.value.code == 'PROXY_CONFIG_INVALID'
        assert time.monotonic() - started < 1
        with pytest.raises(OSError):
            os.fstat(high)
    finally:
        if mode == 'half_packet_timeout':
            os.close(w)


def test_unknown_expiry_is_probe_only_and_still_roundtrips():
    snap = route.validate_route_envelope(envelope(expires_at=None), account_id='acct-1')
    r, _ = pipe_payload(route.serialize_route(snap))
    assert route.load_route_from_fd(r, account_id='acct-1').expires_at is None
    guard = route.ProbeGuard(snap)
    guard.reserve_pair()
    receipt = guard.confirm_pair('ctx-1', '203.0.113.4', '203.0.113.4', observed_at=100)
    assert route.require_probe_receipt(snap, receipt, 'ctx-1', now=100) == receipt
    for business in (
        lambda: snap.require_not_expired(100),
        lambda: snap.business_scope_ids(receipt, 'ctx-1', now=100),
        lambda: guard.require_business(receipt, 'ctx-1', now=100),
        lambda: route.collection_route_provenance(snap, receipt, observation_id='obs',
            source_task_id='task', source_batch_id='batch', collected_at=100, context_id='ctx-1'),
    ):
        with pytest.raises(route.CollectionRouteError, match='PROXY_UNAVAILABLE'):
            business()


def managed_snapshot():
    base = route.validate_route_envelope(envelope(observed_exit={
        'ip': '203.0.113.4', 'observed_at': 100.0}), account_id='acct-1')
    snap = replace(base, route_mode='proxy_preferred', operation_id='op-1',
                   lease_id='lease-1', lease_version=2, owner_id='app-1',
                   exit_generation=3, context_id='ctx-1')
    return replace(snap, snapshot_digest=route.snapshot_digest(snap))


def test_managed_route_roundtrip_digest_and_ack_are_bound():
    snap = managed_snapshot()
    encoded = route.serialize_route(snap)
    decoded = route.validate_route_envelope(json.loads(encoded), account_id='acct-1')
    assert decoded == snap and decoded.managed
    ack = route.route_ack(snap, owner_id='app-1', lease_version=2,
                          context_id='ctx-1', now=100.0, ttl=30)
    assert ack['ttl'] == 30.0
    assert snap.require_managed_ack(ack, now=100.0) == ack
    with pytest.raises(route.CollectionRouteError) as error:
        snap.require_managed_ack({**ack, 'expires_at': ack['expires_at'] + 1}, now=100.0)
    assert error.value.code == 'PROXY_REVISION_MISMATCH'


def test_managed_route_requires_exit_generation_on_probe_receipt():
    snap = managed_snapshot()
    receipt = route.ProbeReceipt(snap.proxy_id, snap.config_revision, 'ctx-1',
                                 '203.0.113.4', 100.0, exit_generation=2)
    with pytest.raises(route.CollectionRouteError) as error:
        route.require_probe_receipt(snap, receipt, 'ctx-1', now=100.0)
    assert error.value.code == 'PROXY_REVISION_MISMATCH'


def test_legacy_direct_is_explicitly_unmanaged():
    snap = route.validate_route_envelope(envelope(route_mode='legacy_direct'), account_id='acct-1')
    assert snap.route_mode == 'legacy_direct' and not snap.managed
