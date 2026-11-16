"""Restore and network side effects must not span database generations."""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from home_study_agent.database import Database


def test_guard_is_shared_by_path_reentrant_and_released_after_error(tmp_path):
    first = Database(tmp_path / 'family.db')
    second = Database(tmp_path / '.' / 'family.db')
    def other_thread():
        with second.external_effect_guard(timeout=0.05):
            return True
    with pytest.raises(ValueError):
        with first.external_effect_guard():
            with second.external_effect_guard():
                with ThreadPoolExecutor(1) as pool:
                    with pytest.raises(TimeoutError):
                        pool.submit(other_thread).result(timeout=2)
                raise ValueError('synthetic failure')
    assert other_thread()


def test_guard_blocks_an_independent_process_and_recovers(tmp_path):
    database = Database(tmp_path / 'family.db')
    script = '''
from pathlib import Path
import sys
from home_study_agent.database import Database
try:
    with Database(Path(sys.argv[1])).external_effect_guard(timeout=0.1):
        print('acquired')
except TimeoutError:
    print('blocked')
'''
    def probe():
        return subprocess.run([sys.executable, '-c', script, str(database.path)],
            env=os.environ.copy(), text=True, capture_output=True, timeout=10, check=True).stdout.strip()
    with database.external_effect_guard():
        assert probe() == 'blocked'
    assert probe() == 'acquired'


def test_different_databases_do_not_block_each_other(tmp_path):
    first, second = Database(tmp_path / 'one.db'), Database(tmp_path / 'two.db')
    with first.external_effect_guard():
        with second.external_effect_guard(timeout=0.05):
            pass


def test_os_releases_guard_after_worker_is_terminated(tmp_path):
    database = Database(tmp_path / 'family.db')
    script = '''
from pathlib import Path
import sys
from home_study_agent.database import Database
with Database(Path(sys.argv[1])).external_effect_guard():
    print('locked', flush=True)
    sys.stdin.read()
'''
    worker = subprocess.Popen([sys.executable, '-c', script, str(database.path)],
        env=os.environ.copy(), text=True, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert worker.stdout.readline().strip() == 'locked'
        with pytest.raises(TimeoutError):
            with database.external_effect_guard(timeout=0.05):
                pass
        worker.terminate()
        worker.wait(timeout=5)
        with database.external_effect_guard(timeout=1):
            assert database.path.with_name('.family.db.external-effects.lock').exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        worker.stdin.close()
        worker.stdout.close()
        worker.stderr.close()
