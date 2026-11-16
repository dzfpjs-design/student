"""Local source-to-review wiring through the real HTTP application."""
from uuid import uuid4
from datetime import timedelta

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_learning_runtime_local_integration import journey, gate, flow, start, CHILD


class NoCloud:
    async def complete(self, *args, **kwargs):
        raise AssertionError('Local answer review must not call a cloud model')


def test_bound_prompt_answer_records_local_candidate_once_and_recovers_after_restart(journey, tmp_path):
    j = journey
    settings = Settings(data_dir=tmp_path, database_path=j.db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        prepared = start(j)
        prompt = prepared['prompt']
        path = f'/v1/sessions/{j.sid}/diagnostic-prompt/answer'
        payload = {'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                   'answer': '90', 'client_action_id': str(uuid4())}
        first = client.post(path, json=payload)
        assert first.status_code == 200, first.text
        local = first.json()['local_learning_evaluation']
        assert local == {'status': 'candidate_recorded', 'local_only': True, 'cloud_transmission_allowed': False}
        second = client.post(path, json=payload)
        assert second.status_code == 200, second.text
        assert second.json()['attempt']['attempt_id'] == first.json()['attempt']['attempt_id']
        with j.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        token = client.post('/v1/parent/setup', json={'pin': '7391'}).json()['parent_token']
        result = client.get(f'/v1/parent/children/{CHILD}/learning-rubrics', headers={'X-HSC-Parent': token})
        assert result.status_code == 200, result.text
        assert result.json()['total'] == 1
        snapshot = result.json()['items'][0]
        assert snapshot['response'] == '90'
        assert snapshot['source_kind'] == 'local_criteria_mapping'
        assert snapshot['score_status'] == 'candidate'
        with j.db.connect() as c:
            assert c.execute('SELECT phase FROM agent_objective WHERE objective_id=?', (j.objective,)).fetchone()[0] == 'diagnose'
        from test_learning_rubric_http import payload as review_payload
        path = f"/v1/parent/children/{CHILD}/learning-rubrics/{snapshot['assessment_id']}/review"
        client.headers['X-HSC-Parent'] = token
        body = review_payload(snapshot, 'apply-original-answer')
        approved = client.post(path, json=body)
        assert approved.status_code == 200, approved.text
        transition = approved.json()['learning_transition']
        assert transition['objective_phase'] == 'transfer_check'
        with j.db.connect() as c:
            original_time = c.execute('SELECT updated_at FROM agent_objective WHERE objective_id=?', (j.objective,)).fetchone()[0]
            assert c.execute("SELECT COUNT(*) FROM agent_commitment WHERE status='pending'").fetchone()[0] == 1
        j.clock[0] += timedelta(minutes=5)
        replay = client.post(path, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['learning_transition']['status'] == 'already_applied'
        with j.db.connect() as c:
            assert c.execute('SELECT updated_at FROM agent_objective WHERE objective_id=?', (j.objective,)).fetchone()[0] == original_time
        withdrawn = client.post(path, json={'expected_revision': 1, 'client_action_id': 'withdraw-original-answer',
            'operation': 'retract', 'reason': 'The answer was assisted'})
        assert withdrawn.status_code == 200, withdrawn.text
        old_replay = client.post(path, json=body)
        assert old_replay.status_code == 200, old_replay.text
        assert old_replay.json()['assessment']['score_status'] == 'withdrawn'
        assert old_replay.json()['learning_transition']['status'] == 'not_applied'
        with j.db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM agent_commitment WHERE status IN ('pending','due')").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_unbound_legacy_diagnostic_does_not_guess_a_learning_objective(journey, tmp_path):
    j = journey
    settings = Settings(data_dir=tmp_path, database_path=j.db.path, maintenance_enabled=False)
    from test_learning_runtime_local_integration import ITEM
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        result = client.post(f'/v1/sessions/{j.sid}/diagnostics/answer', json={
            'diagnostic_item_id': ITEM, 'answer': '90', 'client_action_id': str(uuid4())})
        assert result.status_code == 200, result.text
        with j.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
