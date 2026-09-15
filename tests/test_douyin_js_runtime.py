import os
import subprocess
import sys
from pathlib import Path


def test_signing_uses_bundled_node_outside_repository_without_service_path(tmp_path):
    env = os.environ.copy()
    env['PATH'] = str(tmp_path)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    env['EXECJS_RUNTIME'] = 'JavaScriptCore'
    result = subprocess.run([
        sys.executable, '-c',
        "from media_platform.douyin.help import get_a_bogus_from_js; "
        "assert get_a_bogus_from_js('/fixture/', 'keyword=fixture', 'fixture-UA')"
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
