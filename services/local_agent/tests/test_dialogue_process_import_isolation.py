"""Invalid fixture scope must fail before default app initialization."""
import os
from pathlib import Path
import subprocess
import sys


def test_invalid_fixture_root_does_not_initialize_the_import_default(tmp_path):
    untouched = tmp_path / 'import-default-must-not-exist'
    fixture = Path(__file__).parent / 'fixtures/document_dialogue_process.py'
    source = Path(__file__).parents[1] / 'src'
    result = subprocess.run([sys.executable, str(fixture), '--data-root', str(tmp_path / 'unexpected')],
        env={'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path), 'PYTHONPATH': str(source),
             'HSC_DATA_DIR': str(untouched), 'HSC_DATABASE_PATH': str(untouched / 'home-study.db'),
             'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert 'only disposable pytest fixture data are allowed' in result.stderr
    assert not untouched.exists(), 'a rejected fixture initialized the default app before validating scope'
