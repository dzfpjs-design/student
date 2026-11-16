"""Owned Mac Electron + actual Runtime TCP; no supplier or microphone."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest
from test_realtime_output_authority import case, DOMAIN
from test_realtime_output_authority_tcp import server, call


@pytest.mark.parametrize('lose_bind', [False, True])
def test_runtime_grant_to_current_native_player_and_restart(case, lose_bind, tmp_path):
    db, scope, _ = case
    root = Path(__file__).resolve().parents[3]
    electron = root / 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'
    if not electron.exists(): pytest.skip('Explicit Mac-only qualification; not target Windows proof')
    evidence = root / 'docs/evidence/2026-10-05-realtime-tool-output-bridge'
    env = dict(os.environ); env.pop('ELECTRON_RUN_AS_NODE', None)
    with server(db, lose_bind=lose_bind) as (origin, _), tempfile.TemporaryDirectory(prefix='hsc-tool-output-20261005-', dir='/private/tmp') as directory:
        ran = subprocess.run([str(electron), str(evidence / 'native-probe.cjs'), directory],
            input=json.dumps({'identity': scope, 'lose_bind': lose_bind, 'runtime': {
                'status': 'ready', 'localApiUrl': origin, 'sessionToken': 'synthetic-output-session-token'}}),
            env=env, text=True, capture_output=True, timeout=25)
        result = json.loads((Path(directory) / 'result.json').read_text())
        # A rerun must not replace a historical acceptance artifact.
        destination = tmp_path / ('native-bind-loss.json' if lose_bind else 'native-normal.json')
        destination.write_text(json.dumps(result, indent=2))
        assert ran.returncode == 0 and result['status'] == 'observed', result
        assert result['counts']['requests'] == result['counts']['closed'] == 1
        assert result['contexts_closed'] and result['remaining_audio_elements'] == 0
        assert result['supplier_requests'] == 0 and result['hardware_audible_verified'] is False
    with server(db) as (origin, _):
        receipt = call(origin, scope, 'inspect')
        assert receipt['state'] == 'retired' and not receipt['dispatch_allowed']
    with db.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 3
