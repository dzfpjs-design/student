"""Real owned WebSocket -> main controller -> real Runtime SQLite authority.

The supplier qualification is opt-in, bounded and synthetic; no user devices.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest
from test_realtime_output_authority import case, DOMAIN
from test_realtime_output_authority_tcp import server, call


def run_connection(db, scope, lose_bind=False, supplier=False):
    root = Path(__file__).resolve().parents[3]
    script = root / 'docs/evidence/2026-10-05-realtime-supplier-connection/connection-client.cjs'
    with server(db, lose_bind=lose_bind) as (origin, _):
        result = subprocess.run(['node', str(script)], input=json.dumps({'identity': scope, 'supplier': supplier,
            'result_path': os.environ.get('HSC_SYNTHETIC_SUPPLIER_CONNECTION_RESULT') if supplier else None,
            'runtime': {'status': 'ready', 'localApiUrl': origin, 'sessionToken': 'synthetic-output-session-token'}}),
            text=True, capture_output=True, timeout=55)
        assert 'signature' not in result.stdout and 'Bearer' not in result.stdout
        observed = json.loads(result.stdout)
        assert result.returncode == 0, observed
    with server(db) as (origin, _):
        recovered = call(origin, observed['identity'], 'inspect')
        assert recovered['state'] == 'retired' and not recovered['dispatch_allowed'] and not recovered['has_reply']
    with db.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 3
    return observed


@pytest.mark.parametrize('lose_bind', [False, True])
def test_owned_websocket_original_tool_runtime_bridge_restart(case, lose_bind):
    db, scope, _ = case
    observed = run_connection(db, scope, lose_bind=lose_bind)
    assert observed['paid_response_requests'] == 0 and observed['owned_socket_closed'] is True
    assert observed['sent'].count('response.create') == 1 and observed['runtime_identity_resolved'] is True
    assert observed['rendered_completed'] is False and observed['supplier_real'] is False
    if lose_bind:
        assert observed['generation_completed'] is False and observed['frames'] == []
        assert observed['failures'] == ['QWEN_CONNECTION_OUTPUT_UNCONFIRMED']
    else:
        assert observed['generation_completed'] is True and observed['failures'] == []
        assert [frame['sequence'] for frame in observed['frames']] == [1, 2, 3]


@pytest.mark.skipif(os.environ.get('HSC_SYNTHETIC_SUPPLIER_CONNECTION_PROBE') != '1', reason='Explicit bounded paid synthetic qualification only')
def test_official_supplier_original_tool_runtime_output_qualification(case):
    db, scope, _ = case
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET response_text='你好，先读题。'")
        c.commit()
    observed = run_connection(db, scope, supplier=True)
    output = Path(os.environ['HSC_SYNTHETIC_SUPPLIER_CONNECTION_RESULT'])
    assert str(output).startswith('/private/tmp/hsc-supplier-connection-20261005-') and output.exists()
    assert observed['supplier_real'] is True and observed['paid_response_requests'] == 2
    assert observed['generation_completed'] is True and observed['owned_socket_closed'] is True
    assert observed['output_text_matches'] is True and observed['transcript_matches'] is True
    assert observed['voice_returned'] == 'Tina' and observed['failures'] == []
