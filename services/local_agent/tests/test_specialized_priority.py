from datetime import datetime, timedelta, timezone
from uuid import uuid4

from home_study_agent.database import Database
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.specialized_program import SpecializedProgramService
from test_specialized_program_reliability import flow, answer, CHILD, DOMAIN


def reference(service, score=1.0, days=0):
    return service.record_assessment(CHILD, 'junior_exam', '数学', 'parent_reference', {DOMAIN: score}, str(uuid4()),
        occurred_at=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat())


def domain(service):
    return next(row for row in service.roadmap(CHILD, 'junior_exam')['domains'] if row['domain_id'] == DOMAIN)


def test_old_high_reference_cannot_mask_new_independent_failures(flow):
    db, service, _, _, _, _ = flow
    reference(service, days=7)
    attempts = [answer(flow, i, correct=False, capture=False) for i in range(2)]
    current = domain(service)
    assert current['reference_score'] == 0
    assert current['assessment_reference_score'] == 100
    assert current['reference_score_basis'] == 'qualified_independent_observations'
    assert current['priority_status'] == 'recheck_needed'
    assert current['evidence_conflict'] is True
    action = service.next_agent_action(CHILD, 'junior_exam')
    assert action['domain_id'] == DOMAIN
    assert action['action_type'] == 'remediation'
    assert set(action['evidence_refs']) == {item['evidence_id'] for item in attempts}
    reopened = SpecializedProgramService(Database(db.path))
    assert domain(reopened)['priority_evidence_refs'] == current['priority_evidence_refs']


def test_expired_reference_is_preserved_but_not_used_as_current_planning_score(flow):
    _, service, _, _, _, _ = flow
    reference(service, days=31)
    current = domain(service)
    assert current['assessment_reference_score'] == 100
    assert current['reference_score'] is None
    assert current['assessment_planning_status'] == 'expired_reference'
    assert current['status'] == 'baseline_needed'
    history = service.roadmap(CHILD, 'junior_exam')['assessment_history']
    assert history[-1]['current_evidence_status'] == 'expired_reference'


def test_current_independent_success_takes_precedence_over_a_low_reference_without_certification(flow):
    _, service, _, _, _, _ = flow
    reference(service, score=.2)
    answer(flow, 0, capture=False)
    current = domain(service)
    assert current['reference_score'] == 100
    assert current['assessment_reference_score'] == 20
    assert current['evidence_conflict'] is True
    assert current['mastery_claim_allowed'] is False
    assert service.roadmap(CHILD, 'junior_exam')['stage_requirements']['certified_stage'] is None


def test_withdrawing_failure_removes_priority_and_references_without_deleting_history(flow):
    db, service, _, _, _, _ = flow
    reference(service)
    attempt = answer(flow, 0, correct=False, capture=False)
    assert domain(service)['priority_status'] == 'recheck_needed'
    book = MistakeBookService(db)
    mistake = book.capture_from_attempt(attempt)
    book.confirm(mistake['mistake_id'], CHILD, 'exclude', str(uuid4()))
    current = domain(service)
    assert current['priority_evidence_refs'] == []
    assert current['priority_status'] == 'baseline_needed'
    assert current['reference_score'] == 100
    assert current['learning_evidence_count'] == 0
    assert service._next_action('junior_exam', current, 0)['action_type'] == 'baseline'


def test_high_reference_without_independent_evidence_still_requires_baseline(flow):
    _, service, _, _, _, _ = flow
    reference(service)
    current = domain(service)
    assert current['reference_score'] == 100
    assert current['priority_status'] == 'baseline_needed'
    action = service._next_action('junior_exam', current, 0)
    assert action['action_type'] == 'baseline'
    assert action['mastery_claim_allowed'] is False
