"""Daily plans keep exact stage inputs and do not fabricate unavailable training."""
import pytest

from home_study_agent.specialized_program import SpecializedProgramService
from test_report_followup_host import intake
from test_daily_report_evidence_versions import flow, CHILD


@pytest.mark.parametrize('status', ['ready', 'blocked', 'waiting_due', 'missing'])
def test_plan_preserves_stage_binding_or_explicit_unplaced_obligation(flow, monkeypatch, status):
    db, repo, _, _ = flow
    sid = intake(flow)
    with db.connect() as c:
        c.execute('UPDATE family_rule SET max_learning_minutes=120,task_segment_minutes=30,pet_min_minutes=0 WHERE child_id=?', (CHILD,))
        c.execute('UPDATE family_rule SET competition_minutes=15 WHERE child_id=?', (CHILD,))
        c.commit()
    action = {'domain_id': 'competition_math.counting', 'subject': '数学', 'title': '核对计数基线', 'minutes': 15,
              'completion_standard': '保存实际原题答案', 'diagnostic_item_id': 'original-stage-item',
              'knowledge_node_id': 'node-oly-counting', 'source_version': 'stage-source-1', 'review_task_id': None,
              'not_before': '2099-01-01T00:00:00Z' if status == 'waiting_due' else None,
              'execution_status': status, 'mastery_claim_allowed': False,
              'stage_constraint': {'version': 'specialized_stages_v1', 'current_stage': 'baseline', 'qualified_stage': None,
                                   'blockers': [], 'evidence_fingerprint': 'stage-evidence-1'}}
    monkeypatch.setattr(SpecializedProgramService, 'next_agent_action', lambda *args, **kwargs: None if status == 'missing' else action)
    # This case tests budget/projection; actual source revalidation is covered below.
    monkeypatch.setattr('home_study_agent.specialized_stages.revalidate_stage_action', lambda *args, **kwargs: {'status': status})
    workspace = repo.generate_plan(sid, 'stage-plan-' + status)
    tasks = [item for item in workspace['plan_items'] if item['source_type'] == 'competition_math']
    decisions = [row for row in workspace['constraint_decisions'] if row['obligation_type'] == 'competition_math']
    assert len(decisions) == 1
    if status == 'ready':
        assert len(tasks) == 1 and tasks[0]['expected_minutes'] == 15
        evidence = decisions[0]['metadata']['planning_evidence']
        assert evidence['stage_constraint'] == action['stage_constraint']
        assert evidence['diagnostic_item_id'] == action['diagnostic_item_id']
        assert evidence['source_version'] == action['source_version']
        assert evidence['mastery_claim_allowed'] is False
    else:
        assert not tasks
        assert decisions[0]['decision'] == 'deferred'
        assert decisions[0]['allocated_minutes'] == 0
        assert decisions[0]['requested_minutes'] == 15


from test_specialized_stages import case, baseline, answer, reviewed_variants, node


@pytest.mark.parametrize('withdraw', [False, True])
def test_plan_write_transaction_revalidates_actual_original_source(case, monkeypatch, withdraw):
    from home_study_agent.repository import StudyRepository
    answer(case, baseline(case))
    reviewed_variants(case)
    action = {**node(case)['next_action'], 'minutes': 15, 'title': 'Actual local stage', 'subject': '数学'}
    db, _, sid, _ = case
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,pet_min_minutes=0,competition_minutes=15 WHERE child_id=?', (CHILD,))
        c.commit()
    def current_action(*args, **kwargs):
        if withdraw:
            with db.connect() as c:
                c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (action['diagnostic_item_id'],))
                c.commit()
        return action
    monkeypatch.setattr(SpecializedProgramService, 'next_agent_action', current_action)
    workspace = StudyRepository(db).generate_plan(sid, 'actual-stage-plan')
    tasks = [row for row in workspace['plan_items'] if row['source_type'] == 'competition_math']
    assert bool(tasks) is not withdraw
    decisions = [row for row in workspace['constraint_decisions'] if row['obligation_type'] == 'competition_math']
    assert decisions[0]['decision'] == ('deferred' if withdraw else 'scheduled')
