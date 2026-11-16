"""A reviewed spoken observation reaches planning, not fabricated mastery."""
import json
from datetime import datetime, timezone

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.learning_feedback import build_learning_feedback
from home_study_agent.strategy_evolution import StrategyEvolutionService
from test_school_open_reviews import school_case, next_day, CHILD
from test_school_spoken_reviews import spoken_review
from test_school_feedback_observation_lifecycle import model


@pytest.mark.parametrize('school_case', ['spoken','spoken_physics'], indirect=True)
def test_oral_observation_child_model_strategy_daily_plan_and_withdrawal(school_case, monkeypatch):
    db, client, headers = school_case[:3]
    saved, assessment, path, body, voice = spoken_review(school_case, monkeypatch, approve=False)
    with db.connect() as c:
        assert build_learning_feedback(c, CHILD)['nodes'] == []
    assert client.post(path, headers=headers, json=body).status_code == 200
    learning = model(client, headers)['learning']
    assert learning['qualified_original_count'] == 1
    assert learning['evidenced_node_count'] == 0
    node = learning['reliable_feedback']['nodes'][0]
    assert node['status'] == 'recheck_needed'
    assert '口头说明' in node['reason'] and '原稿' not in node['reason']
    assert node['attempt_count'] == node['independent_correct'] == node['retention_success_count'] == 0
    fact = node['qualified_original_observations'][0]
    assert fact['response_mode'] == 'spoken_explanation'
    assert fact['evidence_id'] == saved['attempt']['evidence_id']
    assert fact['mastery_claim_allowed'] is False and fact['cloud_transmission_allowed'] is False
    priority = ChildModelService(db).compact_for_agent(CHILD)['learning_priorities'][0]
    assert priority['next_action'] == 'clarify_then_recheck'
    candidate = StrategyEvolutionService(db).maybe_propose(CHILD)
    assert candidate['status'] == 'draft'
    with db.connect() as c:
        basis = json.loads(c.execute('SELECT candidate_config_json FROM strategy_experiment WHERE experiment_id=?',
            (candidate['experiment_id'],)).fetchone()[0])['selection']['basis']
        assert len(basis['original_observations']) == 1 and not basis['learning']
        assert build_learning_feedback(c, 'child-primary-4')['nodes'] == []
    client = client.school_reopen()
    token = client.post('/v1/parent/unlock', json={'pin':'8264'}).json()['parent_token']
    headers = {'X-HSC-Parent':token}
    assert model(client, headers)['learning']['qualified_original_count'] == 1
    assert StrategyEvolutionService(db).maybe_propose(CHILD)['experiment_id'] == candidate['experiment_id']
    case, slot, _, _, _ = next_day((db, client, headers, *school_case[3:]), monkeypatch,
        key='oral-feedback-day', execute=False)
    assert '口头说明' in slot['completion_standard']
    withdrawn = case[1].post(path, headers=case[2], json=dict(operation='retract', expected_revision=1,
        client_action_id='oral-model-withdraw', reason='Review observation withdrawn for rechecking'))
    assert withdrawn.status_code == 200, withdrawn.text
    assert model(case[1], case[2])['learning']['reliable_feedback']['nodes'] == []
    assert StrategyEvolutionService(db).get(candidate['experiment_id'])['status'] == 'rolled_back'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',
            (slot['source_ref'],)).fetchone()[0] == 'cancelled'


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_spoken_weekly_monthly_reports_revalidate_audio_on_restart(school_case, monkeypatch):
    from test_period_written_observations_http import finish_daily, generate
    from home_study_agent.period_reports import PeriodReportService
    db, client, headers = school_case[:3]
    scored, _, _, _, voice = spoken_review(school_case, monkeypatch)
    finish_daily(school_case)
    reports = [generate(client, headers, kind) for kind in ('weekly','monthly')]
    for report in reports:
        summary = report['content']['learning_summary']
        observations = summary['qualified_observations']
        assert len(observations) == 1
        assert observations[0]['response_mode'] == 'spoken_explanation'
        assert observations[0]['score_status'] == 'reviewed_human'
        assert observations[0]['mastery_claim_allowed'] is False
        assert summary['coverage']['answered_evidence_count'] == 0
        assert summary['coverage']['sufficient_for_mastery_claim'] is False
    client.school_reopen()
    service = PeriodReportService(db)
    for old in reports:
        assert service.get(old['period_report_id'])['content']['learning_summary']['qualified_observations'] == old['content']['learning_summary']['qualified_observations']
    with db.connect() as c:
        c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?', (voice,))
        c.commit()
    for old in reports:
        assert service.get(old['period_report_id'])['content']['learning_summary']['qualified_observations'] == []
    with db.connect() as c:
        assert build_learning_feedback(c, CHILD)['nodes'] == []
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',
            (scored['attempt']['attempt_id'],)).fetchone()[0] is None
