"""Real local app deletion boundary; synthetic media is never opened.

The companion native journal is deliberately outside the family database.
This test must not treat a missing backend receipt as native closure evidence.
"""
import json
import subprocess
from pathlib import Path

import pytest

from home_study_agent.database import Database
from test_controlled_cloud_semantics_http import prepare
from test_controlled_start_atomic_http import CHILD
from test_controlled_launch_intent_http import client_for
from test_learning_runtime_http import NoCloud
from test_privacy_operation_recovery_http import identity


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def native_probe(tmp_path):
    # Execute the real native SQLite journal, not a Python imitation of its states.
    entry = tmp_path / 'probe.ts'
    entry.write_text('''
import {ControlledLaunchIntents} from MODULE;
const [directory,command,sessionId,childId,otherChildId,cancellation]=process.argv.slice(2);
const scope={childId,sessionId,kind:'video' as const,requestId:'privacy-lost-start'};
const journal=new ControlledLaunchIntents(directory,{read:async()=>null,
  close:async()=>({closed:false}),hasResource:async()=>false,
  cancelMissing:async()=>cancellation==='confirmed'});
if(command==='prepare')console.log(JSON.stringify(journal.prepare(scope)));
else {
  const original=journal.list(scope)[0];
  if(original)await journal.cancel(original.intentId);
  const unresolved=journal.list(scope).length>0;
  let error=null;
  try{journal.prepare({...scope,childId:otherChildId,sessionId:'other-session',requestId:'other-request'})}
  catch(e){error=(e as Error).message}
  console.log(JSON.stringify({unresolved,otherChildError:error}));
}
'''.replace('MODULE', json.dumps(str(ROOT / 'src/main/controlled-launch-intent.ts'))))
    bundle = tmp_path / 'probe.mjs'
    subprocess.run([str(ROOT / 'node_modules/.bin/esbuild'), str(entry), '--bundle',
                    '--platform=node', '--format=esm', '--outfile=' + str(bundle)],
                   check=True, capture_output=True, text=True)
    journal = tmp_path / 'native-profile' / 'native-launch-intents'

    def run(command, sid, cancellation=False):
        other = 'child-primary-4' if CHILD != 'child-primary-4' else 'child-junior-3'
        result = subprocess.run(['node', str(bundle), str(journal), command, sid, CHILD, other,
                                 'confirmed' if cancellation else 'unconfirmed'],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout)
    return run


@pytest.mark.parametrize('scope', ['media', 'all_history'])
def test_parent_delete_must_not_orphan_external_native_launch(tmp_path, native_probe, scope):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud()) as client:
        sid, _, path, headers = prepare(client, db, tmp_path, 'content')
        intent = native_probe('prepare', sid)
        started = client.post(path + '/start', json={
            'query': '图形练习', 'client_action_id': intent['requestId'],
            'native_intent_id': intent['intentId']})
        assert started.status_code == 200, started.text
        lookup = f"/v1/sessions/{sid}/controlled-launch-intents/{intent['intentId']}"
        original = client.get(lookup)
        assert original.status_code == 200
        assert original.json()['childId'] == CHILD == intent['childId']
        operation = identity(client, headers)
        deleted = client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers,
                              json={'scope': scope, 'confirmation': '确认删除',
                                    'client_action_id': operation['request_id'], 'operation': operation})
        after = client.get(lookup)
        with db.connect() as c:
            receipt_count = c.execute('SELECT COUNT(*) FROM controlled_launch_receipt WHERE intent_id=?',
                                      (intent['intentId'],)).fetchone()[0]
            other_child = c.execute('SELECT COUNT(*) FROM child_profile WHERE child_id<>?', (CHILD,)).fetchone()[0]
        # Follow the existing recovery contract before calling anything an orphan:
        # media deletion can leave the session valid, so authoritative cancellation
        # may still fence a genuinely never-dispatched request. A 404 alone cannot.
        cancellation = client.post(lookup + '/cancel-unstarted', json={
            'kind': intent['kind'], 'request_id': intent['requestId']})
        cancelled = cancellation.status_code == 200 and cancellation.json().get('status') == 'cancelled_before_start'
        probe = native_probe('probe', sid, cancelled)
        facts = {'delete_status': deleted.status_code, 'delete_state': deleted.json().get('status'),
                 'lookup_status': after.status_code, 'receipt_count': receipt_count,
                 'other_child_present': other_child, 'native': probe}
        facts['cancel_missing_status'] = cancellation.status_code
        facts['cancel_missing_confirmed'] = cancelled
        (tmp_path / 'privacy-boundary-facts.json').write_text(json.dumps(facts, indent=2))
        # A correct preflight must reject BEFORE deletion if native settlement has
        # not happened; alternatively it must actually settle and free the lease.
        assert (deleted.status_code == 409 and receipt_count == 1) or (
            deleted.status_code == 200 and not probe['unresolved']
            and probe['otherChildError'] is None), facts
