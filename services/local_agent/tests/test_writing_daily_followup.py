"""Actual paper/closure receipts authorize a new day's bounded writing work."""
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, CHILD, OTHER
from test_paper_writing_runtime_http import synthetic_page
from test_writing_feedback_focus_http import PaperJourney
from test_writing_stage_review_consumers import advance_clock


def test_closed_paper_feedback_resolves_current_day_without_rewriting_history(environment, monkeypatch):
    settings, db = environment
    journey = PaperJourney(settings, db, monkeypatch)
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        journey.begin(client)
        _, evaluated = journey.submit(client, journey.prompt, 'daily-source', *synthetic_page(monkeypatch))
        outcome = evaluated['execution']['result']['outcome_id']
        assert client.post(f'/v1/sessions/{journey.sid}/transition', json={
            'expected_state': 'study', 'target_state': 'reflection', 'reason': 'Finished today',
            'client_action_id': 'daily-reflection'}).status_code == 200
        assert client.post(f'/v1/sessions/{journey.sid}/reflection/complete', json={
            'learned_text': '', 'difficult_text': '', 'client_action_id': 'daily-close'}).status_code == 200
        with db.connect() as c:
            commitment = dict(c.execute("SELECT * FROM agent_commitment WHERE child_id=? AND json_extract(payload_json,'$.source_outcome_id')=?",
                (CHILD, outcome)).fetchone())
            end = c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (journey.sid,)).fetchone()[0]
    advance_clock(monkeypatch, datetime.fromisoformat(end) + timedelta(days=1))
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        created = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'daily-next-session'})
        assert created.status_code == 201, created.text
        sid = created.json()['session_id']
        from home_study_agent.writing_daily_followup import resolve_daily_writing_followup
        with db.connect() as c:
            c.execute('BEGIN')
            before = c.total_changes
            result = resolve_daily_writing_followup(c, child_id=CHILD, session_id=sid,
                commitment_id=commitment['commitment_id'], as_of=datetime.fromisoformat(end) + timedelta(days=1))
            assert result['status'] == 'ready', json.dumps(result, ensure_ascii=False)
            action = result['action']
            assert action['session_id'] == sid and action['parent_session_id'] == journey.sid
            assert action['parent_prompt_id'] == journey.prompt['prompt_id']
            assert action['diagnostic_item_id'] == journey.prompt['diagnostic_item_id']
            assert action['kind'] == 'revision' and action['required_hint_level'] == 1
            assert action['mastery_claim_allowed'] is False
            assert result['cloud_transmission_allowed'] is False
            assert result['commitment_id'] == commitment['commitment_id']
            assert result['commitment_version'] and result['binding_version']
            assert c.total_changes == before
            assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',
                (commitment['commitment_id'],)).fetchone()) == commitment
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert resolve_daily_writing_followup(c, child_id=OTHER, session_id=sid,
                commitment_id=commitment['commitment_id'])['status'] == 'blocked'
            assert resolve_daily_writing_followup(c, child_id=CHILD, session_id=sid,
                commitment_id=commitment['commitment_id'], as_of=datetime.fromisoformat(end) - timedelta(days=1))['reason'] == 'writing_followup_not_due'
            for mutation, args in (
                ("UPDATE agent_commitment SET status='cancelled' WHERE commitment_id=?", (commitment['commitment_id'],)),
                ("UPDATE agent_commitment SET payload_json=json_set(payload_json,'$.source_step_id','missing') WHERE commitment_id=?", (commitment['commitment_id'],)),
                ("UPDATE diagnostic_prompt SET status='invalidated' WHERE prompt_id=?", (journey.prompt['prompt_id'],)),
                ("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,)),
                ("UPDATE agent_commitment SET payload_json='invalid-json' WHERE commitment_id=?", (commitment['commitment_id'],)),
                ("UPDATE agent_commitment SET payload_json=json_set(payload_json,'$.source_outcome_id','other-outcome') WHERE commitment_id=?", (commitment['commitment_id'],)),
                ("UPDATE diagnostic_item SET prompt='Changed question' WHERE diagnostic_item_id=?", (journey.prompt['diagnostic_item_id'],)),
                ("UPDATE agent_objective SET status='cancelled' WHERE objective_id=?", (commitment['objective_id'],)),
                ("UPDATE capture_artifact SET status='deleted' WHERE artifact_type='writing_original' AND session_id=?", (journey.sid,)),
            ):
                c.execute('SAVEPOINT damaged')
                c.execute(mutation, args)
                assert resolve_daily_writing_followup(c, child_id=CHILD, session_id=sid,
                    commitment_id=commitment['commitment_id'], as_of=datetime.fromisoformat(end) + timedelta(days=1))['status'] == 'blocked'
                c.execute('ROLLBACK TO damaged')
                c.execute('RELEASE damaged')
