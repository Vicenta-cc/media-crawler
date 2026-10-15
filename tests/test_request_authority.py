"""Real private-FD worker tests, with SQLite categorically denied in child."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict

import pytest

from tools.collection_route import BoundRoute, derive_profile_key, derive_scope_ids
from tools.persistent_request_gate import RequestScheduler, _try_reserve_scopes
from tools.authority_ledger import initialize

APP = Path(os.environ.get('MEDIACRAWLER_APP_TEST_ROOT', str(Path(__file__).resolve().parents[2] / 'wukong-proxy-app')))
if not (APP / 'backend/audit_agent/crawler_request_authority.py').is_file():
    pytest.skip('paired App authority workspace required for FD integration tests', allow_module_level=True)
spec = importlib.util.spec_from_file_location('authority_owner_test', APP / 'backend/audit_agent/crawler_request_authority.py')
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)


def route(tmp_path):
    return BoundRoute(platform='dy', account_id='account1', profile_key=derive_profile_key('account1'),
        proxy_id='proxy1', binding_revision=1, config_revision=1, host='example.invalid', port=8080,
        username='test', password='test', expires_at=time.time()+600, observed_exit_ip='203.0.113.1',
        observed_exit_at=time.time(), scheduler_db_path=str(tmp_path/'scheduler.sqlite3'),
        scope_ids=derive_scope_ids('dy','account1','proxy1','203.0.113.1'))


CHILD = '''
import asyncio,json,os,sqlite3,sys
from tools.collection_route import BoundRoute,ProbeReceipt
from tools.bound_proxy import BoundProxySession
payload=json.loads(sys.stdin.readline()); snap=BoundRoute(**payload)
def denied(*a,**kw): raise AssertionError('CHILD_DATABASE_ACCESS')
sqlite3.connect=denied
session=BoundProxySession(snap,echo_url='https://echo.invalid/ip',budget_id='testbudget')
session.check_stop(); session._validate_browser_safety()
assert session.budget.reserve('open')==2
session.budget.confirm_baseline(snap,'203.0.113.1')
assert session.budget.snapshot()['used']==4
session.context_id='testcontext'
session.receipt=ProbeReceipt(snap.proxy_id,snap.config_revision,'testcontext','203.0.113.1',__import__('time').time())
session.guard.confirm_pair('testcontext','203.0.113.1','203.0.113.1')
session._gate=session._make_gate()
async def run():
    ticks=[]
    async def heartbeat():
        for _ in range(5):
            await asyncio.sleep(.001)
            ticks.append(1)
    beat=asyncio.create_task(heartbeat())
    for _ in range(5):
        await session.check_stop_async()
        await session._validate_browser_safety_async()
    await beat
    assert len(ticks)==5
    session.context=object()
    async with session.slot('api', timeout=2):
        assert session.trial_limits.snapshot()['operations']==1
    provenance=await session.provenance_async(observation_id='observation',source_task_id='task',source_batch_id='batch')
    assert provenance['source_account_id']==snap.account_id
    token=session.trial_limits.begin_media()
    assert session.trial_limits.media_timeout(token)>0
    session.trial_limits.reserve_media_bytes(token,7)
    session.trial_limits.finish_media(token)
    assert session.trial_limits.snapshot()['media_bytes']==7
    session.enter_cooldown('test',.01)
asyncio.run(run())
assert not os.get_inheritable(int(os.environ['MEDIACRAWLER_REQUEST_AUTHORITY_FD']))
import subprocess
check = subprocess.run([sys.executable,'-c',"import os,sys; sys.exit(1 if os.path.exists('/dev/fd/'+sys.argv[1]) else 0)",os.environ['MEDIACRAWLER_REQUEST_AUTHORITY_FD']])
assert check.returncode==0
print('ok',flush=True)
'''


def test_child_all_database_touchpoints_are_remote(tmp_path):
    snap = route(tmp_path)
    RequestScheduler(snap.scheduler_db_path, min_interval=0, per_minute=100, media_interval=0)
    handle = owner.open_authority(snap, crawler_root=Path.cwd(), session_options=dict(
        echo_url='https://echo.invalid/ip', budget_id='testbudget', trial_id='testtrial',
        policy=dict(min_interval=0,per_minute=100,max_concurrency=1,media_interval=0,cooldown_seconds=1)))
    proc = subprocess.Popen([sys.executable,'-c',CHILD], stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,text=True,env={**os.environ,**handle.env,'MEDIACRAWLER_DY_BROWSER_ENGINE':'camoufox'},pass_fds=handle.pass_fds)
    handle.bind_child(proc.pid)
    try:
        stdout,stderr=proc.communicate(json.dumps(asdict(snap))+'\n',timeout=15)
        assert proc.returncode==0, stderr
        assert stdout.strip()=='ok'
        with sqlite3.connect(snap.scheduler_db_path) as db:
            assert db.execute('SELECT COUNT(*) FROM crawler_authority_ledger WHERE consumed=1').fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM request_scheduler_leases').fetchone()[0]==0
            assert all(len(json.loads(row[0]))==1 for row in db.execute('SELECT request_times_json FROM request_scheduler_state'))
    finally:
        handle.close()
        if proc.poll() is None:
            proc.kill();proc.wait()


def test_missing_authority_fails_before_sqlite(tmp_path,monkeypatch):
    from tools.bound_proxy import BoundProxySession
    monkeypatch.setenv('MEDIACRAWLER_DY_BROWSER_ENGINE','camoufox')
    monkeypatch.delenv('MEDIACRAWLER_REQUEST_AUTHORITY_FD',raising=False)
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:pytest.fail('child DB opened'))
    with pytest.raises(RuntimeError,match='PROXY_UNAVAILABLE'):
        BoundProxySession(route(tmp_path))


def test_reserve_replay_uses_same_transaction_and_never_refunds(tmp_path):
    snap=route(tmp_path)
    for scope in snap.scope_ids:
        RequestScheduler(snap.scheduler_db_path,platform=scope,min_interval=0,per_minute=1,max_concurrency=1,media_interval=0)
    with sqlite3.connect(snap.scheduler_db_path) as db:
        initialize(db)
    request=dict(channel='channel',request_id='request',digest='same',pid=os.getpid())
    result=_try_reserve_scopes(snap.scheduler_db_path,snap.scope_ids,'api',10,time.time(),authority=request)
    replay=_try_reserve_scopes(snap.scheduler_db_path,snap.scope_ids,'api',10,time.time(),authority=request)
    assert result==replay
    with pytest.raises(RuntimeError,match='PROXY_REVISION_MISMATCH'):
        _try_reserve_scopes(snap.scheduler_db_path,snap.scope_ids,'api',10,time.time(),authority={**request,'digest':'changed'})
    from tools.persistent_request_gate import release_scope_tokens
    release_scope_tokens(snap.scheduler_db_path,result['scope_tokens'])
    assert _try_reserve_scopes(snap.scheduler_db_path,snap.scope_ids,'api',10,time.time())['delay']>0
    with sqlite3.connect(snap.scheduler_db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM crawler_authority_ledger').fetchone()[0]==1
        assert all(len(json.loads(row[0]))==1 for row in db.execute('SELECT request_times_json FROM request_scheduler_state'))

COMMAND_CHILD = '''
import json,sys
from tools.collection_route import BoundRoute,ProbeReceipt
from tools.request_authority import AuthorityClient
snap=BoundRoute(**json.loads(sys.stdin.readline()))
client=AuthorityClient(snap)
client.call('initialize')
client.call('probe_reserve',dict(category='open',use_remaining=False))
client.call('probe_confirm',dict(ip='203.0.113.1'))
client.call('make_gate',dict(receipt=dict(proxy_id=snap.proxy_id,config_revision=snap.config_revision,
    context_id='context',ip='203.0.113.1',observed_at=__import__('time').time(),exit_generation=snap.exit_generation)))
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
    value=json.loads(line)
    try:
        result=client.call(value['op'],value.get('payload'),request_id=value.get('id'))
        print(json.dumps({'ok':True,'result':result}),flush=True)
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)}),flush=True)
'''


class Worker:
    def __init__(self,snap,cache_options=None,**policy):
        initial_options = {}
        if snap.managed:
            from tools.collection_route import route_ack
            now = time.time()
            ack = route_ack(snap,owner_id=snap.owner_id,lease_version=snap.lease_version,
                now=now,ttl=min(300,snap.expires_at-now))
            # Explicit fixture-owner ticket, never an implicit server fallback.
            initial_options['initial_ack']={**ack,'expires_at':min(snap.expires_at,now+300)}
        RequestScheduler(snap.scheduler_db_path,min_interval=0,per_minute=policy.get('per_minute',100),media_interval=0)
        self.handle=owner.open_authority(snap,crawler_root=Path.cwd(),session_options=dict(
            echo_url='https://echo.invalid/ip',budget_id='testbudget',trial_id='sharedtrial',
            policy=dict(min_interval=0,per_minute=policy.get('per_minute',100),max_concurrency=1,media_interval=0,cooldown_seconds=1),**(cache_options or {}),**initial_options))
        self.proc=subprocess.Popen([sys.executable,'-c',COMMAND_CHILD],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,env={**os.environ,**self.handle.env},pass_fds=self.handle.pass_fds)
        self.handle.bind_child(self.proc.pid)
        self.proc.stdin.write(json.dumps(asdict(snap))+'\n');self.proc.stdin.flush()
        line=self.proc.stdout.readline()
        assert line, self.proc.stderr.read()
        assert json.loads(line)=={'ready':True}
    def call(self,op,payload=None,id=None):
        self.proc.stdin.write(json.dumps(dict(op=op,payload=payload,id=id))+'\n');self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())
    def close(self):
        self.handle.close()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


def test_lost_reply_replay_grant_once_pause_and_release(tmp_path):
    snap=route(tmp_path)
    worker=Worker(snap)
    try:
        first=worker.call('reserve',dict(operation='api',lease_seconds=10),id='a'*32)
        replay=worker.call('reserve',dict(operation='api',lease_seconds=10),id='a'*32)
        assert first==replay
        assert not worker.call('reserve',dict(operation='media',lease_seconds=10),id='a'*32)['ok']
        grant=first['result']['grant']
        from tools.persistent_request_gate import enter_cooldown_scopes
        enter_cooldown_scopes(snap.scheduler_db_path,snap.scope_ids,'pause',30)
        assert not worker.call('consume',dict(grant=grant))['ok']
        assert worker.call('release',dict(scope_tokens=first['result']['scope_tokens']))['ok']
        assert worker.call('release',dict(scope_tokens=first['result']['scope_tokens']))['ok']
        with sqlite3.connect(snap.scheduler_db_path) as db:
            assert db.execute('SELECT COUNT(*) FROM request_scheduler_leases').fetchone()[0]==0
            assert all(len(json.loads(row[0]))==1 for row in db.execute('SELECT request_times_json FROM request_scheduler_state'))
    finally:
        worker.close()


def test_grant_consumed_once_and_ttl_not_extended_by_replay(tmp_path):
    snap=route(tmp_path);worker=Worker(snap)
    try:
        first=worker.call('reserve',dict(operation='api',lease_seconds=10),id='b'*32)['result']
        assert worker.call('consume',dict(grant=first['grant']))['ok']
        assert not worker.call('consume',dict(grant=first['grant']))['ok']
        replay=worker.call('reserve',dict(operation='api',lease_seconds=10),id='b'*32)['result']
        assert replay['grant_expires_at']==first['grant_expires_at']
        assert not worker.call('consume',dict(grant=replay['grant']))['ok']
    finally:
        worker.close()


def test_parent_eof_stops_requests_keeps_live_child_capacity_then_reaps_dead_pid(tmp_path):
    snap=route(tmp_path);worker=Worker(snap)
    try:
        result=worker.call('reserve',dict(operation='api',lease_seconds=10))['result']
        worker.handle.close()
        assert not worker.call('check_stop')['ok']
        with sqlite3.connect(snap.scheduler_db_path) as db:
            assert set(row[0] for row in db.execute('SELECT pid FROM request_scheduler_leases'))=={worker.proc.pid}
        assert _try_reserve_scopes(snap.scheduler_db_path,snap.scope_ids,'api',10,time.time())['reason']=='concurrency'
        worker.proc.kill();worker.proc.wait()
        next_result=_try_reserve_scopes(snap.scheduler_db_path,snap.scope_ids,'api',10,time.time())
        assert next_result['delay']==0
    finally:
        worker.close()


def test_trial_shared_final_budget_and_replayed_bytes(tmp_path):
    snap=route(tmp_path);worker=Worker(snap)
    try:
        for _ in range(39):
            assert worker.call('trial_operation')['ok']
        one=worker.call('trial_operation',id='c'*32)
        assert one['result']==40
        assert worker.call('trial_operation',id='c'*32)==one
        assert not worker.call('trial_operation')['ok']
        assert not worker.call('trial_begin_media')['ok']
        with sqlite3.connect(snap.scheduler_db_path) as db:
            assert db.execute('SELECT operations,stopped FROM proxy_trial_limits').fetchone()==(40,1)
    finally:
        worker.close()


def test_process_start_identity_mismatch_invalidates_live_pid_lease(tmp_path):
    snap=route(tmp_path)
    RequestScheduler(snap.scheduler_db_path,min_interval=0,per_minute=100,media_interval=0)
    with sqlite3.connect(snap.scheduler_db_path) as db:
        initialize(db)
        db.execute('INSERT INTO request_scheduler_leases VALUES(?,?,?,?)',('old','dy',os.getpid(),time.time()+60))
        db.execute('INSERT INTO crawler_authority_process_leases VALUES(?,?,?)',('old',os.getpid(),'previous-process-start'))
    result=_try_reserve_scopes(snap.scheduler_db_path,['dy'],'api',10,time.time())
    assert result['delay']==0
    with sqlite3.connect(snap.scheduler_db_path) as db:
        assert not db.execute("SELECT 1 FROM request_scheduler_leases WHERE token='old'").fetchone()


def test_two_real_children_compete_for_final_quota_without_partial_debit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    snap=route(tmp_path);one=Worker(snap,per_minute=1);two=Worker(snap,per_minute=1)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda worker:worker.call('reserve',dict(operation='api',lease_seconds=10)),[one,two]))
        assert sum(reply['ok'] and reply['result']['delay']==0 for reply in replies)==1
        with sqlite3.connect(snap.scheduler_db_path) as db:
            assert all(len(json.loads(row[0]))==1 for row in db.execute('SELECT request_times_json FROM request_scheduler_state'))
            assert db.execute('SELECT COUNT(*) FROM request_scheduler_leases').fetchone()[0]==len(snap.scope_ids)
    finally:
        one.close();two.close()


def test_pause_racing_reserve_never_allows_consumption_after_pause(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from tools.persistent_request_gate import enter_cooldown_scopes
    snap=route(tmp_path);worker=Worker(snap);barrier=Barrier(2)
    try:
        def reserve():
            barrier.wait()
            return worker.call('reserve',dict(operation='api',lease_seconds=10))
        def pause():
            barrier.wait()
            enter_cooldown_scopes(snap.scheduler_db_path,snap.scope_ids,'pause',30)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(reserve);paused=pool.submit(pause)
            reply=pending.result();paused.result()
        if reply['ok'] and reply['result']['delay']==0:
            assert not worker.call('consume',dict(grant=reply['result']['grant']))['ok']
        else:
            assert not reply['ok'] or reply['result']['delay']>0
    finally:
        worker.close()


def test_media_reply_replay_does_not_double_bytes_or_file_count(tmp_path):
    snap=route(tmp_path);worker=Worker(snap)
    try:
        first=worker.call('trial_begin_media',id='d'*32)
        assert worker.call('trial_begin_media',id='d'*32)==first
        payload=dict(token=first['result'],size=13)
        result=worker.call('trial_media_bytes',payload,id='e'*32)
        assert worker.call('trial_media_bytes',payload,id='e'*32)==result
        assert result['result']==13
        assert worker.call('trial_finish_media',dict(token=first['result']))['ok']
        snapshot=worker.call('trial_snapshot')['result']
        assert snapshot['media_bytes']==13 and snapshot['media_files']==1
    finally:
        worker.close()


def test_page_safety_checks_never_debit_business_slots(tmp_path):
    snap=route(tmp_path);worker=Worker(snap)
    try:
        for _ in range(12):
            assert worker.call('browser_validate')['ok']
        with sqlite3.connect(snap.scheduler_db_path) as db:
            assert db.execute('SELECT COUNT(*) FROM request_scheduler_leases').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM request_scheduler_state').fetchone()[0]==0
    finally:
        worker.close()


def test_shared_exit_pause_and_generation_change_rechecked_in_attached_transaction(tmp_path):
    from dataclasses import replace
    snap=route(tmp_path)
    safety=str(tmp_path/'accounts.sqlite3')
    with sqlite3.connect(safety) as db:
        db.execute('CREATE TABLE crawler_shared_exits(exit_key TEXT,generation INTEGER,observed_at REAL,paused_until REAL,pause_reason TEXT)')
        db.execute('INSERT INTO crawler_shared_exits VALUES(?,?,?,?,?)',(snap.observed_exit_ip,1,time.time(),None,''))
        db.execute('CREATE TABLE crawler_route_admissions(operation_id TEXT,account_id TEXT,exit_key TEXT,generation INTEGER,state TEXT)')
        db.execute('INSERT INTO crawler_route_admissions VALUES(?,?,?,?,?)',('op',snap.account_id,snap.observed_exit_ip,1,'active'))
        db.execute('CREATE TABLE crawler_accounts(id TEXT,recovery_claim TEXT,recovery_state TEXT)')
        db.execute('INSERT INTO crawler_accounts VALUES(?,?,?)',(snap.account_id,'',''))
    snap=replace(snap,safety_db_path=safety,exit_generation=1,operation_id='op',
        owner_id='owner',lease_id='lease',lease_version=1,context_id='context',snapshot_digest='a'*64)
    worker=Worker(snap)
    try:
        first=worker.call('reserve',dict(operation='api',lease_seconds=10))['result']
        with sqlite3.connect(safety) as db:
            db.execute('UPDATE crawler_shared_exits SET pause_reason=?,paused_until=?',('challenge',time.time()+60))
        assert not worker.call('consume',dict(grant=first['grant']))['ok']
        assert not worker.call('browser_validate')['ok']
        assert not worker.call('reserve',dict(operation='api',lease_seconds=10))['ok']
        with sqlite3.connect(safety) as db:
            db.execute("UPDATE crawler_shared_exits SET pause_reason='',paused_until=NULL,generation=2")
        assert not worker.call('browser_validate')['ok']
    finally:
        worker.close()


def test_actual_authority_process_kill_leaves_child_fail_closed(tmp_path):
    """Kill the owner process, rather than merely calling its close method."""
    snap=route(tmp_path)
    RequestScheduler(snap.scheduler_db_path,min_interval=0,per_minute=100,media_interval=0)
    child_code=COMMAND_CHILD[:COMMAND_CHILD.index('for line in sys.stdin:')]+'''
from pathlib import Path
root=Path(sys.argv[1])
client.call('reserve',dict(operation='api',lease_seconds=10))
(root/'ready').write_text(str(os.getpid()))
while not (root/'check').exists(): __import__('time').sleep(.01)
try:
    client.call('check_stop')
    result='unexpected_allowed'
except Exception:
    result='stopped'
(root/'result').write_text(result)
while not (root/'finish').exists(): __import__('time').sleep(.01)
'''
    # COMMAND_CHILD uses os only in this extension.
    child_code='import os\n'+child_code
    broker_code='''
import importlib.util,json,os,subprocess,sys,time
from pathlib import Path
from tools.collection_route import BoundRoute
value=json.loads(sys.stdin.readline())
spec=importlib.util.spec_from_file_location('owner',value['module']);owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner)
snap=BoundRoute(**value['snapshot'])
handle=owner.open_authority(snap,crawler_root=Path.cwd(),session_options=dict(echo_url='https://echo.invalid/ip',budget_id='crash',policy=dict(min_interval=0,per_minute=100,max_concurrency=1,media_interval=0,cooldown_seconds=1)))
proc=subprocess.Popen([sys.executable,'-c',value['child_code'],value['directory']],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True,env={**os.environ,**handle.env},pass_fds=handle.pass_fds)
handle.bind_child(proc.pid);proc.stdin.write(json.dumps(value['snapshot'])+'\\n');proc.stdin.flush()
print(proc.pid,flush=True)
while True: time.sleep(1)
'''
    broker=subprocess.Popen([sys.executable,'-c',broker_code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    child_pid=None
    try:
        broker.stdin.write(json.dumps(dict(module=str(APP/'backend/audit_agent/crawler_request_authority.py'),
            snapshot=asdict(snap),child_code=child_code,directory=str(tmp_path)))+'\n');broker.stdin.flush()
        child_pid=int(broker.stdout.readline())
        deadline=time.monotonic()+5
        while not (tmp_path/'ready').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert (tmp_path/'ready').exists()
        broker.kill();broker.wait()
        (tmp_path/'check').touch()
        deadline=time.monotonic()+5
        while not (tmp_path/'result').exists() and time.monotonic()<deadline:
            time.sleep(.01)
        assert (tmp_path/'result').read_text()=='stopped'
        with sqlite3.connect(snap.scheduler_db_path) as db:
            assert {row[0] for row in db.execute('SELECT pid FROM request_scheduler_leases')}=={child_pid}
        assert _try_reserve_scopes(snap.scheduler_db_path,snap.scope_ids,'api',10,time.time())['reason']=='concurrency'
    finally:
        (tmp_path/'finish').touch()
        if broker.poll() is None:
            broker.kill();broker.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid,9)
            except ProcessLookupError:
                pass


def test_cache_lookup_owner_validates_payload_media_and_raw_path_without_exposing_db(tmp_path):
    outputs=tmp_path/'outputs';task=outputs/'task';raw=task/'raw_items';raw.mkdir(parents=True)
    def item(key, *, root=task, complete=True):
        path=root/'raw_items'/f'dy_{key}.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'item':{'aweme_id':key,'aweme_url':'https://www.douyin.com/video/'+key,'video_download_url':'https://cdn.invalid/video'}}))
        video=root/'crawler/douyin/videos'/key/'video.mp4';video.parent.mkdir(parents=True,exist_ok=True)
        video.write_bytes(b'\x00\x00\x00\x18ftypmp42' if complete else b'invalid')
        return str(path)
    index=tmp_path/'index.sqlite3'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE contents(id INTEGER PRIMARY KEY,platform TEXT,collection_status TEXT,content_key TEXT,raw_item_path TEXT)')
        db.execute('CREATE TABLE content_matches(content_id INTEGER,task_id TEXT,keyword TEXT)')
        for number,key,path in [(1,'111',item('111')),(2,'222',item('222',complete=False)),(3,'333',item('333',root=tmp_path/'outside'))]:
            db.execute('INSERT INTO contents VALUES(?,?,?,?,?)',(number,'dy','complete',key,path))
            db.execute('INSERT INTO content_matches VALUES(?,?,?)',(number,'task-bound','keyword-bound'))
    worker=Worker(route(tmp_path),cache_options=dict(reusable_db=str(index),reusable_outputs_root=str(outputs),current_task_id='task-bound',keywords=('keyword-bound',)))
    try:
        first=worker.call('cache_lookup',dict(keyword='keyword-bound',offset=0,limit=1))
        assert first=={'ok':True,'result':{'ids':['111'],'next_offset':1}}
        assert worker.call('cache_lookup',dict(keyword='keyword-bound',offset=1,limit=1))['result']=={'ids':[],'next_offset':2}
        assert worker.call('cache_lookup',dict(keyword='keyword-bound',ids=['111','222','333']))['result']=={'ids':['111'],'next_offset':None}
        assert not worker.call('cache_lookup',dict(keyword='other-account',ids=['111']))['ok']
        assert not worker.call('cache_lookup',dict(keyword='keyword-bound',ids=['111'],db='/arbitrary'))['ok']
        assert not worker.call('cache_lookup',dict(keyword='keyword-bound',ids=['../secret']))['ok']
        assert not worker.call('cache_lookup',dict(keyword='keyword-bound',ids=['111']*101))['ok']
        assert worker.call('cache_lookup',dict(keyword='keyword-bound',offset=3,limit=128))['result']=={'ids':[],'next_offset':None}
    finally:
        worker.close()


def test_managed_short_lived_snapshot_owner_ack_uses_one_clock_sample(tmp_path):
    from dataclasses import replace
    snap=replace(route(tmp_path),expires_at=time.time()+120,operation_id='operation',owner_id='owner',
        lease_id='lease',lease_version=1,snapshot_digest='a'*64,context_id='context')
    worker=Worker(snap)
    try:
        assert worker.call('check_stop')['ok']
    finally:
        worker.close()


def test_login_request_kinds_are_fixed_and_charged_without_accepting_arbitrary_operations(tmp_path):
    worker=Worker(route(tmp_path))
    try:
        for kind in ('login_action','login_navigation'):
            response=worker.call('reserve',dict(operation=kind,lease_seconds=10))
            assert response['ok'] and response['result']['delay']==0
            assert worker.call('consume',dict(grant=response['result']['grant']))['ok']
            assert worker.call('release',dict(scope_tokens=response['result']['scope_tokens']))['ok']
        assert not worker.call('reserve',dict(operation='arbitrary SQL/action',lease_seconds=10))['ok']
    finally:
        worker.close()


@pytest.mark.asyncio
async def test_authority_renewed_ticket_updates_periodic_probe_deadline(monkeypatch,tmp_path):
    import threading
    from types import SimpleNamespace
    from dataclasses import replace
    from tools.bound_proxy import BoundProxySession
    from tools.collection_route import route_ack
    start=time.time(); clock=[start]
    snap=replace(route(tmp_path),expires_at=start+1200,operation_id='operation',owner_id='owner',
        lease_id='lease',lease_version=1,snapshot_digest='a'*64,context_id='context')
    initial=route_ack(snap,owner_id='owner',lease_version=1,now=start,ttl=300)
    initial={**initial,'expires_at':start+300}
    renewals=[]
    def renew(prior):
        renewals.append(prior['lease_version'])
        return {**prior,'lease_version':prior['lease_version']+1,'expires_at':clock[0]+300,'ttl':300}
    parent=BoundProxySession(snap,authority_owner=True,ack=initial,lease_renewer=renew,
        echo_url='https://echo.invalid/ip',budget_id='renewal-budget')
    child=object.__new__(BoundProxySession)
    child.snapshot=snap;child._lease_ack=dict(initial);child._lease_lock=threading.RLock()
    child._stopped=None;child.stop_checker=None;child.guard=SimpleNamespace(ttl=300)
    class Remote:
        async def acall(self,op,payload=None):
            parent.check_stop()
            return {'ack':parent._lease_ack}
    child.authority=Remote()
    monkeypatch.setattr(time,'time',lambda:clock[0])
    assert child._next_probe_delay()==275
    for expected_version in (2,3):
        clock[0]+=275
        await child.check_stop_async()
        assert child._lease_ack['lease_version']==expected_version
        assert child._next_probe_delay()==275
        await child.check_stop_async()
        assert child._lease_ack['lease_version']==expected_version
    assert renewals==[1,2]
    previous=dict(child._lease_ack)
    for bad in ({**previous,'owner_id':'other'}, {**previous,'lease_version':1},
            {**previous,'expires_at':snap.expires_at+1}):
        with pytest.raises(RuntimeError):
            child._accept_authority_ticket({'ack':bad})
    assert child._lease_ack==previous


def test_managed_owner_authority_requires_explicit_trusted_initial_ticket_before_db(tmp_path,monkeypatch):
    from dataclasses import replace
    snap=replace(route(tmp_path),operation_id='operation',owner_id='owner',lease_id='lease',
        lease_version=1,context_id='context',snapshot_digest='a'*64)
    monkeypatch.setattr(sqlite3,'connect',lambda *args,**kwargs:pytest.fail('missing ticket reached DB'))
    with pytest.raises(ValueError,match='AUTHORITY_INITIAL_TICKET_REQUIRED'):
        owner.open_authority(snap,crawler_root=Path.cwd())


def test_maintenance_authority_cannot_be_promoted_into_business_operations(tmp_path):
    handle=owner.open_authority(route(tmp_path),crawler_root=Path.cwd(),session_options=dict(
        echo_url='https://echo.invalid/ip',budget_id='maintenance-only',maintenance_only=True))
    try:
        handle._execute('initialize',{}, {})
        for operation in ('make_gate','reserve','consume','release','cooldown','gate_snapshot','trial_operation'):
            with pytest.raises(RuntimeError,match='PROXY_UNAVAILABLE'):
                handle._execute(operation,{}, {})
    finally:
        handle.close()
