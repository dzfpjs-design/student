"""Disposable real TCP/SQLite host with synthetic providers and paper only."""
import argparse
from contextlib import redirect_stdout
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--phase', choices=('candidate', 'answer', 'confirm', 'diagnosis', 'remediation'), default='diagnosis')
    parser.add_argument('--source', choices=('paper', 'diagnostic'), default='paper')
    parser.add_argument('--inspect', action='store_true')
    args = parser.parse_args()
    if args.source == 'diagnostic' and args.phase not in {'diagnosis', 'remediation'}:
        raise ValueError('diagnostic fixture has no paper facts to confirm')
    root = Path(args.data_root).resolve()
    if not root.name.startswith('test_') or not any(p.name.startswith('pytest-') for p in root.parents):
        raise ValueError('only disposable test data are allowed')
    path = root / 'test.db'
    if args.inspect:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
            value = {name: c.execute('SELECT * FROM ' + name).fetchall() for name in (
                'mistake_case', 'mistake_attempt', 'mistake_diagnosis', 'mistake_dialogue',
                'learning_evidence', 'diagnostic_attempt', 'rubric_assessment')}
            value['independent_tutor_turns'] = c.execute(
                "SELECT turn_id FROM tutor_turn WHERE client_action_id LIKE 'foreground-question:%'").fetchall()
        print(json.dumps(value))
        return
    # Isolate the module-level default app before importing any product module.
    import_root = root / 'import-only'
    os.environ['HSC_DATA_DIR'] = str(import_root)
    os.environ['HSC_DATABASE_PATH'] = str(import_root / 'home-study.db')
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app, app as imported_app
    from test_mistake_voice_journey import settings, fixture, begin, say
    from test_pet_vocabulary_prompt_integration import Planner
    from pending_dialogue_ui import SyntheticGateway
    assert imported_app.state.settings.data_dir == import_root
    from original_money_owner_fixture import close_unused_imported_default_app_owner
    close_unused_imported_default_app_owner(imported_app)
    config = settings(root)
    identity_path = root / 'identity.json'
    if not path.exists():
        gateway = Planner()
        gateway.configured = False
        with redirect_stdout(sys.stderr), TestClient(create_app(config, model_gateway=gateway)) as client:
            if args.source == 'diagnostic':
                from home_study_agent.database import Database
                from home_study_agent.learning_state import LearningStateService
                from home_study_agent.mistake_dialogue import MistakeDialogueService
                response = client.post('/v1/sessions', json={'child_id': 'child-primary-4',
                    'client_action_id': 'diagnostic-stage-session'})
                assert response.status_code == 201, response.text
                sid = response.json()['session_id']
                db = Database(config.database_path)
                with db.connect() as c:
                    c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
                    c.commit()
                receipt = LearningStateService(db).submit_diagnostic(sid,
                    'diag-p4-arithmetic-closed-1', '54', 'diagnostic-stage-wrong')
                assert receipt['score_status'] == 'verified_local'
                case = client.app.state.mistake_book.capture_from_attempt(receipt)
                dialogue = MistakeDialogueService(db)
                dialogue.open_review(sid, 'diagnostic-stage-open', mistake_id=case['mistake_id'])
                if args.phase == 'remediation':
                    dialogue.respond(sid, '我不知道为什么错了', 'diagnostic-stage-reason')
                    result = dialogue.respond(sid, '答案是42', 'diagnostic-stage-probe')
                    assert result['phase'] == 'remediation'
            else:
                sid, _ = fixture(client)
                begin(client, sid)
                quotes = {'candidate': (), 'answer': ('对',), 'confirm': ('对', '我选B')}.get(
                    args.phase, ('第四题', '对', '我选B', '是的'))
                for index, quote in enumerate(quotes):
                    say(client, sid, quote, 'stage-prepare-' + str(index))
                if args.phase == 'remediation':
                    say(client, sid, '我是看拼写选的，不知道比较读音的方法', 'stage-prepare-reason')
        identity_path.write_text(json.dumps({'session_id': sid, 'child_id': 'child-primary-4'}))
    identity = json.loads(identity_path.read_text())
    serving = replace(config, session_token='public-independent-ui-fixture')
    app = create_app(serving, model_gateway=SyntheticGateway(root, tutor=True))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    print(json.dumps({**identity, 'port': sock.getsockname()[1],
                      'synthetic_models': True, 'import_isolated': True}), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on')).run(sockets=[sock])


if __name__ == '__main__':
    main()
