"""Disposable real HTTP/database; synthetic voice and model providers only."""
import argparse
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

QUOTE = '等评价的时候，你能告诉我有没有PET学习资料吗？'
TUTOR_QUOTE = '数学里两个负数相乘为什么得到正数？'


class SyntheticGateway:
    configured = True

    def __init__(self, root, *, tutor=False):
        self.path = root / 'model-calls.json'
        self.calls = json.loads(self.path.read_text()) if self.path.exists() else []
        self.tutor = tutor

    async def complete(self, request):
        from home_study_agent.model_gateway import ModelResult
        request.pre_dispatch()
        self.calls.append(request.request_type)
        self.path.write_text(json.dumps(self.calls), encoding='utf-8')
        quote = TUTOR_QUOTE if self.tutor else QUOTE
        if request.request_type == 'voice_interaction':
            value = {'role': 'question', 'control': None, 'quote': quote,
                     'question_scope': 'independent', 'confidence': .97}
        elif request.request_type.startswith('tutor_hint_level_'):
            value = {'blackboard': {'title': '负数乘法', 'segments': [{
                'narration': '保持分配律一致时，两个负数相乘就得到正数。',
                'blocks': [{'kind': 'text', 'text': '负数乘负数得到正数。'}],
            }]}}
        else:
            value = {'objective': {'mode': 'none', 'phase': 'query'},
                     'next_step': {'capability': 'learning.explain' if self.tutor else 'system.inspect_learning',
                                   'arguments': {'question': quote, 'subject': '数学'} if self.tutor else
                                                {'scope': 'resources', 'track': 'pet'}},
                     'utterance_binding': {'role': 'question', 'quote': quote},
                     'spoken_transition': '先讲解这个独立问题。' if self.tutor else '先核对本机PET资料。',
                     'requires_child_reply': False, 'confidence': .97}
        return ModelResult(content=json.dumps(value, ensure_ascii=False), provider='test',
                           model='synthetic-only', latency_ms=1, prompt_tokens=20, completion_tokens=20)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--tutor', action='store_true')
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    if not root.name.startswith('test_') or not any(p.name.startswith('pytest-') for p in root.parents):
        raise ValueError('only disposable test data are allowed')
    path = root / 'evaluation.db'
    if args.inspect:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
            snapshot = {t: c.execute('SELECT * FROM ' + t).fetchall() for t in
                        ('diagnostic_attempt', 'learning_evidence', 'rubric_assessment',
                         'learning_evaluation_execution')}
            snapshot['independent_turns'] = c.execute(
                "SELECT agent_turn_id FROM agent_turn WHERE agent_turn_id != 'eval'").fetchall()
            snapshot['independent_receipts'] = [json.loads(row[0]) for row in c.execute(
                "SELECT s.result_json FROM agent_step s JOIN agent_plan p USING(plan_id) "
                "WHERE p.agent_turn_id != 'eval' AND s.status='completed'")]
            snapshot['independent_tutor_turns'] = c.execute(
                "SELECT turn_id FROM tutor_turn WHERE client_action_id LIKE 'foreground-question:%'").fetchall()
        print(json.dumps(snapshot))
        return
    # app.py constructs a default app on import; isolate that too, not just
    # the explicit fixture app. No product import is allowed before this guard.
    import_root = root / 'import-only'
    os.environ['HSC_DATA_DIR'] = str(import_root)
    os.environ['HSC_DATABASE_PATH'] = str(import_root / 'home-study.db')
    from test_learning_outcome_evaluator import evaluation_fixture
    from home_study_agent.app import create_app, app as imported_app
    from home_study_agent.config import Settings
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    assert imported_app.state.settings.data_dir == import_root
    from original_money_owner_fixture import close_unused_imported_default_app_owner
    close_unused_imported_default_app_owner(imported_app)
    if not path.exists():
        db, sid, evaluator, _, _ = evaluation_fixture(root)
        privacy = PrivacyLifecycleService(db, root)
        privacy.initialize()
        privacy.update_preferences({'cloud_model_text_allowed': True})
        evaluator._claim(evaluator._load('eval'))
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
    settings = Settings(data_dir=root, database_path=path, maintenance_enabled=False,
                        session_token='public-independent-ui-fixture')
    app = create_app(settings, model_gateway=SyntheticGateway(root, tutor=args.tutor))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    print(json.dumps({'port': sock.getsockname()[1], 'session_id': 'session-runtime-test',
                      'child_id': 'child-runtime', 'synthetic_models': True,
                      'import_isolated': True}), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='error')).run(sockets=[sock])


if __name__ == '__main__':
    main()
