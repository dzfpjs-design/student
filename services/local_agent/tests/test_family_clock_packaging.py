"""Exercise tzdata fallback without borrowing the development host's IANA files."""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[3]


def test_windows_bundle_explicitly_collects_declared_timezone_data():
    project = (ROOT / 'services/local_agent/pyproject.toml').read_text()
    scripts = json.loads((ROOT / 'package.json').read_text())['scripts']
    assert '"tzdata==2026.3"' in project
    assert '--collect-all tzdata' in scripts['build:agent:win']


def test_family_calendar_imports_and_converts_without_system_timezone_files():
    code = '''from datetime import datetime
from zoneinfo import TZPATH
from importlib.metadata import version
from home_study_agent.family_clock import family_time, calendar_as_of
from home_study_agent.learning_input_replan import ZONE
assert not TZPATH
assert version('tzdata') == '2026.3'
instant = datetime.fromisoformat('2026-09-13T16:30:00+00:00')
assert family_time(instant).isoformat() == '2026-09-14T00:30:00+08:00'
assert calendar_as_of(None, instant)[0].isoformat() == '2026-09-14'
assert str(ZONE) == 'Asia/Shanghai'
print('bundled-timezone-fallback-ok')
'''
    result = subprocess.run([sys.executable, '-c', code], env={**os.environ,
        'PYTHONTZPATH': '', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONPATH': str(ROOT / 'services/local_agent/src')},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == 'bundled-timezone-fallback-ok'
