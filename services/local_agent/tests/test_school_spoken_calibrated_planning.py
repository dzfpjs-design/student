"""Synthetic automatic authority through actual child-model/report consumers."""
import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.strategy_evolution import StrategyEvolutionService
from test_school_open_reviews import school_case, next_day, CHILD
from test_school_feedback_observation_lifecycle import model
from test_school_spoken_calibrated_score import authority


@pytest.mark.parametrize('school_case', ['spoken','spoken_physics'], indirect=True)
def test_automatic_oral_observation_changes_plan_and_rolls_back_without_review(school_case, monkeypatch):
    _, assessment, _, _, _, receipt = authority(school_case, monkeypatch, score=0)
    db, client, headers = school_case[:3]
    # The injected certificate is installed after fixture assessment creation;
    # invoke the same persisted-score consumer as the real evaluator.
    from home_study_agent.school_open_reviews import apply_school_review
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        apply_school_review(c, CHILD, assessment['assessment_id'])
        c.commit()
    learning = model(client, headers)['learning']
    assert learning['qualified_original_count'] == 1
    node = learning['reliable_feedback']['nodes'][0]
    assert node['status'] == 'recheck_needed' and '口头说明' in node['reason']
    assert node['attempt_count'] == node['independent_correct'] == 0
    assert node['qualified_original_observations'][0]['score_status'] == 'calibrated_model'
    assert ChildModelService(db).compact_for_agent(CHILD)['learning_priorities'][0]['next_action'] == 'clarify_then_recheck'
    candidate = StrategyEvolutionService(db).maybe_propose(CHILD)
    assert candidate['status'] == 'draft'
    client = client.school_reopen()
    headers = {'X-HSC-Parent': client.post('/v1/parent/unlock',json={'pin':'8264'}).json()['parent_token']}
    case, slot, _, _, _ = next_day((db,client,headers,*school_case[3:]),monkeypatch,
        key='auto-oral-plan',execute=False)
    assert '口头说明' in slot['completion_standard']
    assert StrategyEvolutionService(db).maybe_propose(CHILD)['experiment_id'] == candidate['experiment_id']
    receipt['calibration_receipt']['score_qualified'] = False
    assert model(case[1],case[2])['learning']['reliable_feedback']['nodes'] == []
    assert StrategyEvolutionService(db).get(candidate['experiment_id'])['status'] == 'rolled_back'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_automatic_oral_reports_revalidate_certificate_after_restart(school_case, monkeypatch):
    from test_period_written_observations_http import finish_daily, generate
    from home_study_agent.period_reports import PeriodReportService
    _, _, _, _, _, receipt = authority(school_case,monkeypatch)
    db, client, headers = school_case[:3]
    finish_daily(school_case)
    reports = [generate(client,headers,kind) for kind in ('weekly','monthly')]
    for report in reports:
        observation = report['content']['learning_summary']['qualified_observations'][0]
        assert observation['response_mode'] == 'spoken_explanation'
        assert observation['score_status'] == 'calibrated_model'
        assert not observation['mastery_claim_allowed']
    client.school_reopen()
    service = PeriodReportService(db)
    receipt['calibration_receipt']['score_qualified'] = False
    for report in reports:
        current = service.get(report['period_report_id'])
        assert current['content']['learning_summary']['qualified_observations'] == []
