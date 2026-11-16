"""An incomplete manuscript submission must not poison application startup."""
from home_study_agent.app import create_app
from home_study_agent.writing_response_origin import WritingOriginalService
from test_scheduled_writing_natural_runtime_http import scheduled, TEXT
from test_school_review_start_integrity_http import _http_client


def test_unbound_saved_manuscript_survives_real_http_restart_without_scoring(tmp_path):
    with scheduled(tmp_path) as case:
        prompt = case['prompt']
        original = WritingOriginalService(case['db']).capture_native_response(
            case['sid'], prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
            text=TEXT, client_action_id='unbound-native-original')
        body = dict(prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                    answer=TEXT, client_action_id='unbound-original-answer')
        path = f'/v1/sessions/{case["sid"]}/diagnostic-prompt/answer'
        first = case['client'].post(path, json=body)
        with case['db'].connect() as c:
            saved = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE diagnostic_item_id=?',
                                  (prompt['diagnostic_item_id'],)).fetchone())
            assert saved['submitted_answer'] == TEXT
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 0
        assert original['artifact_id']
    application = create_app(case['settings'], model_gateway=case['gateway'])
    with _http_client(application) as client:
        if case['settings'].session_token:
            client.headers['X-HSC-Session'] = case['settings'].session_token
        again = client.post(path, json=body)
        assert first.status_code == again.status_code == 200
        for response in (first, again):
            payload = response.json()
            assert payload['attempt']['attempt_id'] == saved['attempt_id']
            assert payload['local_learning_evaluation']['status'] == 'blocked'
        with case['db'].connect() as c:
            assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',
                                  (saved['attempt_id'],)).fetchone()) == saved
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM agent_turn WHERE intent='local_evaluation'").fetchone()[0] == 0
            assert c.execute('PRAGMA foreign_key_check').fetchall() == []
