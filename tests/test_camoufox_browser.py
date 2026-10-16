import base64
import json
import stat
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from media_platform.douyin.core import DouYinCrawler
from tools import camoufox_browser as engine


@pytest.fixture
def launcher(monkeypatch):
    from tools import navigation_guard_addon
    monkeypatch.setattr(navigation_guard_addon, "require_navigation_guard", AsyncMock())
    monkeypatch.setenv("MEDIACRAWLER_CAMOUFOX_PROFILE_GENERATION", "m1")
    launches = []
    def factory(**kwargs):
        manager = SimpleNamespace(browser=None)
        manager.context = SimpleNamespace(close=AsyncMock(), pages=[])
        async def enter():
            manager.browser = manager.context
            return manager.context
        manager.__aenter__ = enter
        manager.__aexit__ = AsyncMock()
        launches.append((kwargs, manager))
        return manager
    monkeypatch.setitem(sys.modules, "camoufox.async_api", SimpleNamespace(AsyncCamoufox=factory))
    monkeypatch.setattr(engine, "_host_os", lambda: "macos")
    monkeypatch.setattr(engine, "_installed_executable", lambda v: "/fixture/camoufox")
    monkeypatch.setattr(engine, "_generate_identity", lambda executable, guard: {
        "env": {"CAMOU_CONFIG_1": "fixed-fixture"}, "args": [], "firefox_user_prefs": {}})
    return launches


@pytest.mark.asyncio
async def test_identity_survives_restart_without_persisting_runtime_env(launcher, monkeypatch, tmp_path):
    monkeypatch.setenv("DISPLAY", ":123")
    monkeypatch.setenv("FIXTURE_SECRET", "not-saved")
    a, profile = await engine.launch_account_context("a", tmp_path)
    first = (profile["directory"] / "launch-identity.json").read_bytes()
    assert b"not-saved" not in first and b":123" not in first
    assert stat.S_IMODE((profile["directory"] / "launch-identity.json").stat().st_mode) == 0o600
    await a.close()
    monkeypatch.setattr(engine, "_generate_identity", lambda *_: pytest.fail("regenerated identity"))
    b, second = await engine.launch_account_context("a", tmp_path)
    assert second == profile
    assert launcher[-1][0]["from_options"]["env"]["DISPLAY"] == ":123"
    assert launcher[-1][0]["from_options"]["headless"] is True
    await b.close()


@pytest.mark.asyncio
async def test_account_lock_and_close_failure_fence(launcher, tmp_path):
    a, _ = await engine.launch_account_context("a", tmp_path)
    underlying = launcher[0][1]
    # A failed driver teardown also leaves the account fenced.
    underlying.__aexit__.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await a.close()
    with pytest.raises(RuntimeError, match="already in use"):
        await engine.launch_account_context("a", tmp_path)
    underlying.__aexit__.side_effect = None
    await a.close()
    b, _ = await engine.launch_account_context("a", tmp_path)
    await b.close()


def test_identity_generation_migration_and_corruption(launcher, tmp_path):
    a = engine.initialize_account_profile("a", root=tmp_path, generation="m1")
    b = engine.initialize_account_profile("b", root=tmp_path, generation="m1")
    assert a["directory"] != b["directory"]
    assert engine.initialize_account_profile("a", root=tmp_path, generation="m1") == a
    with pytest.raises(ValueError, match="generation mismatch"):
        engine.initialize_account_profile("a", root=tmp_path, generation="m2")
    with pytest.raises(ValueError, match="identity mismatch"):
        engine.profile_for("a", tmp_path, "other-version")
    identity = a["directory"] / "launch-identity.json"
    identity.write_text(identity.read_text().replace("fixed-fixture", "corrupted"))
    with pytest.raises(RuntimeError, match="integrity"):
        engine.initialize_account_profile("a", root=tmp_path, generation="m1")


@pytest.mark.asyncio
async def test_headless_rejected_before_launch(launcher, tmp_path):
    with pytest.raises(ValueError, match="headed"):
        await engine.launch_account_context("a", tmp_path, headless=False)
    assert not launcher


@pytest.mark.asyncio
async def test_old_engine_and_cookie_snapshot_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIACRAWLER_DY_BROWSER_ENGINE", "cloakbrowser")
    with pytest.raises(ValueError, match="fallback is disabled"):
        await DouYinCrawler()._launch_browser_context(None, None, False, False)
    crawler = DouYinCrawler()
    crawler._camoufox_profile = engine.profile_for("a", tmp_path, engine.DEFAULT_BROWSER_VERSION)
    monkeypatch.setenv("MEDIACRAWLER_ACCOUNT_AUTH_STATE_B64", base64.b64encode(json.dumps({"cookies": []}).encode()).decode())
    context = SimpleNamespace(add_cookies=AsyncMock())
    assert not await crawler.apply_account_auth_state(context)
    context.add_cookies.assert_not_awaited()


def test_profile_symlink_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlinks"):
        engine.profile_for("a", link, engine.DEFAULT_BROWSER_VERSION)

@pytest.mark.asyncio
async def test_missing_login_closes_profile_without_second_browser(monkeypatch):
    from media_platform.douyin import core
    monkeypatch.setattr(core.config, 'ENABLE_IP_PROXY', False)
    crawler = DouYinCrawler()
    context = SimpleNamespace(close=AsyncMock())
    async def launch(**kwargs):
        crawler.browser_context = context
    crawler._launch_browser_context = AsyncMock(side_effect=launch)
    crawler._open_index_page = AsyncMock()
    crawler.apply_account_auth_state = AsyncMock()
    crawler.create_douyin_client = AsyncMock(return_value=SimpleNamespace(pong=AsyncMock(return_value=False)))
    crawler._wait_for_login_readiness = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match='ACCOUNT_AUTH_INVALID'):
        await crawler.start()
    assert crawler._launch_browser_context.await_count == 1
    context.close.assert_awaited_once()
    assert crawler.browser_context is None


def test_missing_identity_never_regenerates(launcher, tmp_path):
    profile = engine.initialize_account_profile('a', root=tmp_path, generation='m1')
    (profile['directory'] / 'launch-identity.json').unlink()
    with pytest.raises(RuntimeError, match='refusing to regenerate'):
        engine.initialize_account_profile('a', root=tmp_path, generation='m1')


def test_identity_file_symlink_rejected(launcher, tmp_path):
    profile = engine.initialize_account_profile('a', root=tmp_path, generation='m1')
    identity = profile['directory'] / 'launch-identity.json'
    target = tmp_path / 'other.json'
    identity.rename(target)
    identity.symlink_to(target)
    with pytest.raises(OSError):
        engine.initialize_account_profile('a', root=tmp_path, generation='m1')


def test_dependency_entries_agree_on_only_camoufox_and_paired_playwright():
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / 'pyproject.toml').read_text())
    requirements = (root / 'requirements.txt').read_text().splitlines()
    lock = tomllib.loads((root / 'uv.lock').read_text())
    packages = {p['name']: p['version'] for p in lock['package']}
    for name, version in [('camoufox', '0.5.8'), ('playwright', '1.62.0')]:
        assert f'{name}=={version}' in requirements
        assert f'{name}=={version}' in project['project']['dependencies']
        assert packages[name] == version
    assert 'cloakbrowser' not in packages

@pytest.mark.asyncio
async def test_generation_mismatch_and_missing_fail_before_browser(launcher, monkeypatch, tmp_path):
    first, _ = await engine.launch_account_context('a', tmp_path)
    await first.close()
    monkeypatch.setenv('MEDIACRAWLER_CAMOUFOX_PROFILE_GENERATION', 'other')
    with pytest.raises(ValueError, match='generation'):
        await engine.launch_account_context('a', tmp_path)
    monkeypatch.delenv('MEDIACRAWLER_CAMOUFOX_PROFILE_GENERATION')
    with pytest.raises(ValueError, match='generation'):
        await engine.launch_account_context('a', tmp_path)
    assert len(launcher) == 1


def test_linux_display_is_private_only_when_owner_did_not_provide_one(monkeypatch):
    from contextlib import contextmanager
    from tools import account_display
    entered = []
    @contextmanager
    def display():
        entered.append(True)
        yield
    monkeypatch.setattr(account_display.sys, 'platform', 'linux')
    monkeypatch.setattr(account_display, 'private_display', display)
    monkeypatch.delenv('DISPLAY', raising=False)
    with account_display.account_display():
        pass
    monkeypatch.setenv('DISPLAY', ':123')
    with account_display.account_display():
        pass
    assert entered == [True]


def test_legacy_direct_http_explicitly_bypasses_environment_proxy(monkeypatch, tmp_path):
    import httpx
    from dataclasses import replace
    from tests.test_proxy_exit_observation import snapshot
    from tools.httpx_util import make_async_client
    route = replace(snapshot(tmp_path), route_mode='legacy_direct')
    monkeypatch.setenv('HTTP_PROXY', 'http://trap.invalid:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://trap.invalid:8080')
    options = []
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: options.append(kwargs))
    make_async_client(route_snapshot=route)
    assert options[0]['proxy'] is None
    assert options[0]['trust_env'] is False

@pytest.mark.asyncio
async def test_managed_cache_lookup_is_bounded_and_uses_owner_only():
    crawler = DouYinCrawler()
    lookup = AsyncMock(side_effect=[{'ids':['a'], 'next_offset':128}, {'ids':['b'], 'next_offset':None},
                                   {'ids':['id0'], 'next_offset':None}, {'ids':['id100'], 'next_offset':None}])
    crawler._bound_proxy_session = SimpleNamespace(authority=SimpleNamespace(acall=lookup))
    assert await crawler._owner_reusable_ids('keyword') == {'a','b'}
    assert lookup.call_args_list[0].args == ('cache_lookup', {'keyword':'keyword','offset':0,'limit':128})
    assert lookup.call_args_list[1].args[1]['offset'] == 128
    assert await crawler._owner_reusable_ids('keyword', ids=['id'+str(i) for i in range(101)]) == {'id0','id100'}
    assert [len(c.args[1]['ids']) for c in lookup.call_args_list[2:]] == [100,1]


@pytest.mark.asyncio
async def test_owner_cache_rejects_ids_outside_requested_page():
    crawler = DouYinCrawler()
    crawler._bound_proxy_session = SimpleNamespace(authority=SimpleNamespace(acall=AsyncMock(
        return_value={'ids':['not-requested'], 'next_offset':None})))
    with pytest.raises(RuntimeError, match='PROXY_CONFIG_INVALID'):
        await crawler._owner_reusable_ids('keyword', ids=['requested'])


def test_initial_timezone_rejects_unknown_zone_before_sdk_import(monkeypatch):
    monkeypatch.setenv('MEDIACRAWLER_CAMOUFOX_TIMEZONE', 'Not/A_Timezone')
    with pytest.raises(ValueError, match='initial Camoufox timezone'):
        engine._generate_identity('/fixture/camoufox', {})


@pytest.mark.asyncio
async def test_login_reset_avoids_storage_export_and_third_party_origins(tmp_path):
    cookies = [
        {'domain':'.douyin.com'}, {'domain':'cookie.douyin.com'},
        {'domain':'lf-rc1.yhgfb-cn-static.com'}, {'domain':'echo.example'},
        {'domain':'douyin.com.evil.example'},
    ]
    old = SimpleNamespace(url='https://www.douyin.com/login', frames=[
        SimpleNamespace(url='https://frame.douyin.com/login'),
        SimpleNamespace(url='https://lf-rc1.yhgfb-cn-static.com/verify')], close=AsyncMock())
    keeper = SimpleNamespace(close=AsyncMock())
    reset = SimpleNamespace(route=AsyncMock(), goto=AsyncMock(), evaluate=AsyncMock(), close=AsyncMock())
    context = SimpleNamespace(pages=[old], cookies=AsyncMock(return_value=cookies),
        storage_state=AsyncMock(side_effect=AssertionError('storage export must not be called')),
        new_page=AsyncMock(side_effect=[keeper,reset]), clear_cookies=AsyncMock())
    for name in ('https+++idb-only.douyin.com', 'https+++lf-rc1.yhgfb-cn-static.com', 'https+++echo.example'):
        (tmp_path/'storage'/'default'/name).mkdir(parents=True)
    marker=tmp_path/'launch-identity.json';marker.write_text('fixed identity')
    await engine.prepare_account_login(context, {'directory':tmp_path,'bound_proxy_session':SimpleNamespace(echo_url='https://echo.example/ip')})
    context.storage_state.assert_not_awaited()
    document_policy = reset.route.call_args.args[1]
    forbidden = SimpleNamespace(request=SimpleNamespace(url='https://lf-rc1.yhgfb-cn-static.com/verify',
        is_navigation_request=lambda: True), abort=AsyncMock(), fulfill=AsyncMock())
    await document_policy(forbidden)
    forbidden.abort.assert_awaited_once()
    forbidden.fulfill.assert_not_awaited()
    origins={call.args[0] for call in reset.goto.call_args_list}
    assert {'https://idb-only.douyin.com','https://frame.douyin.com','https://cookie.douyin.com'} <= origins
    assert not any('echo.example' in value or 'yhgfb' in value or 'evil' in value for value in origins)
    pattern=context.clear_cookies.call_args.kwargs['domain']
    assert pattern.search('.douyin.com') and not pattern.search('echo.example')
    keeper.close.assert_not_awaited()
    old.close.assert_awaited_once()
    reset.close.assert_awaited_once()
    assert marker.read_text()=='fixed identity'


@pytest.mark.asyncio
async def test_login_reset_storage_failure_does_not_claim_reset_success(tmp_path):
    keeper=SimpleNamespace(close=AsyncMock())
    page=SimpleNamespace(route=AsyncMock(),goto=AsyncMock(),evaluate=AsyncMock(side_effect=RuntimeError('IDB_RESET_BLOCKED')),close=AsyncMock())
    context=SimpleNamespace(pages=[],cookies=AsyncMock(return_value=[]),new_page=AsyncMock(side_effect=[keeper,page]),clear_cookies=AsyncMock())
    with pytest.raises(RuntimeError,match='IDB_RESET_BLOCKED'):
        await engine.prepare_account_login(context,{'directory':tmp_path})
    context.clear_cookies.assert_not_awaited()
    assert not (tmp_path/'auth-imported').exists()
