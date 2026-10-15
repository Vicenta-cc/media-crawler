"""Opt-in silent-Mac/Linux-display real-kernel identity/storage regression without external traffic."""
import os
import time

import pytest

from tools.camoufox_browser import launch_account_context, prepare_account_login

pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(os.getenv('RUN_CAMOUFOX_ENGINE_TESTS') != '1',
                                 reason='requires installed paired Camoufox kernel and desktop/Xvfb')]


async def test_real_identity_profile_restart_and_explicit_reset(tmp_path, monkeypatch):
    monkeypatch.setenv('MEDIACRAWLER_CAMOUFOX_PROFILE_GENERATION', 'engine-fixture')
    monkeypatch.setenv('TZ', 'UTC')
    monkeypatch.delenv('MEDIACRAWLER_CAMOUFOX_TIMEZONE', raising=False)
    expression = """() => {
      const c=document.createElement('canvas'); const x=c.getContext('2d');
      x.fillText('account identity',1,20);
      return {ua:navigator.userAgent, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone, screen:[screen.width,screen.height],
              canvas:Array.from(x.getImageData(0,0,120,30).data)};
    }"""
    async def open_account(account):
        context, profile = await launch_account_context(account, tmp_path)
        await context.route('https://www.douyin.com/fixture', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<html><body>fixture</body></html>'))
        page = await context.new_page()
        await page.goto('https://www.douyin.com/fixture')
        return context, profile, page
    first, profile, page = await open_account('fixture-a')
    try:
        identity = await page.evaluate(expression)
        await page.evaluate("localStorage.setItem('fixture', 'saved')")
        await first.add_cookies([{'name':'fixture','value':'a','url':'https://www.douyin.com', 'expires':int(time.time())+86400}])
        assert any(cookie['name']=='fixture' for cookie in await first.cookies())
    finally:
        await first.close()
    monkeypatch.setenv('TZ', 'America/New_York')
    monkeypatch.setenv('MEDIACRAWLER_CAMOUFOX_TIMEZONE', 'Europe/Berlin')
    second, resumed, page = await open_account('fixture-a')
    try:
        assert identity['timezone'] == 'Asia/Shanghai'
        assert resumed['identity_hash'] == profile['identity_hash']
        assert await page.evaluate(expression) == identity
        assert await page.evaluate("localStorage.getItem('fixture')") == 'saved'
        assert any(cookie['name']=='fixture' for cookie in await second.cookies())
        await prepare_account_login(second, resumed)
        page = await second.new_page()
        await page.goto('https://www.douyin.com/fixture')
        assert await page.evaluate("localStorage.getItem('fixture')") is None
        assert not any(cookie['name']=='fixture' for cookie in await second.cookies())
    finally:
        await second.close()
    other, other_profile, page = await open_account('fixture-b')
    try:
        assert other_profile['identity_hash'] != profile['identity_hash']
        assert await page.evaluate("localStorage.getItem('fixture')") is None
        assert not any(cookie['name']=='fixture' for cookie in await other.cookies())
    finally:
        await other.close()


async def test_real_explicit_executable_is_version_and_hash_checked(monkeypatch):
    import hashlib
    from pathlib import Path
    from tools.camoufox_browser import _installed_executable, DEFAULT_BROWSER_VERSION
    version = os.getenv('MEDIACRAWLER_CAMOUFOX_BROWSER_VERSION', DEFAULT_BROWSER_VERSION)
    monkeypatch.delenv('MEDIACRAWLER_CAMOUFOX_EXECUTABLE', raising=False)
    executable = _installed_executable(version)
    monkeypatch.setenv('MEDIACRAWLER_CAMOUFOX_EXECUTABLE', executable)
    monkeypatch.setenv('MEDIACRAWLER_CAMOUFOX_EXECUTABLE_SHA256', hashlib.sha256(Path(executable).read_bytes()).hexdigest())
    assert _installed_executable(version) == str(Path(executable).resolve())
    with pytest.raises(RuntimeError, match='version metadata mismatch'):
        _installed_executable('999.0.0-beta.99')
    monkeypatch.setenv('MEDIACRAWLER_CAMOUFOX_EXECUTABLE_SHA256', '0' * 64)
    with pytest.raises(RuntimeError, match='SHA256 mismatch'):
        _installed_executable(version)


async def test_fresh_private_home_uses_verified_explicit_binary_for_first_identity(tmp_path):
    import asyncio
    import hashlib
    import json
    from pathlib import Path
    import subprocess
    import sys
    from tools.camoufox_browser import _installed_executable, DEFAULT_BROWSER_VERSION

    version = os.getenv('MEDIACRAWLER_CAMOUFOX_BROWSER_VERSION', DEFAULT_BROWSER_VERSION)
    executable = _installed_executable(version)
    state = tmp_path / 'private-state'
    for directory in (state/'home'/'.camoufox', state/'cache', state/'tmp', state/'profiles'):
        directory.mkdir(parents=True)
    crawler = Path(__file__).resolve().parents[1]
    environment = {name: os.environ[name] for name in ('PATH','LANG','LC_ALL','TZ') if name in os.environ}
    environment.update(HOME=str(state/'home'), XDG_CACHE_HOME=str(state/'cache'),
        TMPDIR=str(state/'tmp'), PYTHONPATH=str(crawler),
        PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', CRAWLER_RESTRICTED_CHILD='1',
        MEDIACRAWLER_DY_BROWSER_ENGINE='camoufox',
        MEDIACRAWLER_CAMOUFOX_BROWSER_VERSION=version,
        MEDIACRAWLER_CAMOUFOX_EXECUTABLE=executable,
        MEDIACRAWLER_CAMOUFOX_EXECUTABLE_SHA256=hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
        MEDIACRAWLER_CAMOUFOX_PROFILE_GENERATION='fresh-private-home-fixture')
    script = r'''
import asyncio,json,os,sys
from pathlib import Path
from tools.camoufox_browser import launch_account_context
async def main():
 root=Path(sys.argv[1])
 assert not list(root.rglob('launch-identity.json'))
 context,profile=await launch_account_context('fresh-private-home',root)
 try:
  assert profile['engine']=='camoufox'
  assert profile['generation']=='fresh-private-home-fixture'
  assert profile['identity_hash']
  page=await context.new_page()
  value=await page.evaluate('({ua:navigator.userAgent,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})')
  assert ('Firefox/'+os.environ['MEDIACRAWLER_CAMOUFOX_BROWSER_VERSION'].split('.',1)[0]+'.') in value['ua']
  assert value['timezone']=='Asia/Shanghai'
  print(json.dumps({'first_identity_generated':True,'headed':False,'version':profile['version']}))
 finally:
  await context.close()
asyncio.run(main())
'''
    result = await asyncio.to_thread(subprocess.run, [sys.executable,'-c',script,str(state/'profiles')],
        env=environment,cwd=crawler,capture_output=True,text=True,timeout=45)
    assert result.returncode == 0, result.stderr[-4000:]
    assert json.loads(result.stdout.splitlines()[-1])['first_identity_generated'] is True
    assert not list((state/'home').rglob('version.json'))
    assert not list((state/'cache').rglob('version.json'))
