"""Current real report/queue consumers, synthetic inputs; no runtime changes."""
import json
from datetime import datetime, timezone
import pytest

from home_study_agent.parent_reports import ParentReportService
from home_study_agent.parent_work_queue import ParentWorkQueueService
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_supply_gap_resume_http import supplied, CHILD
from test_supply_obligation_cross_day import close_and_plan
from home_study_agent.parent_supply_followup import current_followups


@pytest.fixture(autouse=True)
def daytime(monkeypatch):
    from test_writing_stage_review_consumers import advance_clock
    from home_study_agent.learning_input_replan import ZONE
    import test_supply_obligation_cross_day as cross_day
    def move(patch, at):
        local = at.astimezone(ZONE).replace(hour=16, minute=0, second=0, microsecond=0)
        return advance_clock(patch, local.astimezone(timezone.utc))
    monkeypatch.setattr(cross_day, 'advance_clock', move)


def test_parent_summary_preserves_actual_unfulfilled_root_and_today_action(supplied, monkeypatch):
    case = supplied
    today, root, workspace, attempts = close_and_plan(case, monkeypatch)
    child = CHILD
    assert StudyRepository(case['db']).get_session(today)['child_id'] == child
    current = next(o for o in workspace['supply_obligations']
        if o.get('root_obligation_id') == root['decision_id'])
    assert current['status'] == 'ready'
    repo = StudyRepository(case['db'])
    evidence = repo.report_evidence(case['sid'])
    summary, coaching = build_daily_report(evidence)
    assert '今天没有需要执行的计划' not in summary['headline'], 'Unfulfilled original obligations are not an empty plan'
    saved = repo.save_daily_report(case['sid'], summary, coaching)
    service = ParentReportService(case['db'])
    service.initialize()
    snapshot = service.create_from_daily(saved)
    queue = ParentWorkQueueService(case['db']).list(child_id=child, limit=50)
    proof = dict(root=root, today_session_id=today, child_workspace=current,
        report_tasks=evidence['tasks'], report_summary=saved['summary'],
        report_coaching=saved['parent_coaching'], parent_snapshot=snapshot, parent_queue=queue)
    (case['tmp_path'] / 'parent-summary-proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
    with case['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == attempts
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
        assert dict(c.execute('SELECT * FROM plan_constraint_decision WHERE decision_id=?',
            (root['decision_id'],)).fetchone()) == root
    parent_data = json.dumps(dict(snapshot=snapshot, queue=queue), ensure_ascii=False)
    assert root['decision_id'] in parent_data, (
        'Child workspace carries the exact ready original, but neither actual parent report snapshot '
        'nor parent work queue exposes its original identity/reason/current next action. '
        'Generic PET goals or supply jobs are not the same deferred obligation.',
        root['decision_id'], current, evidence['tasks'], saved['summary']['facts'])
