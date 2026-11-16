"""Plan-host contract for PET stage allocation; source selection has separate tests."""
import pytest

from test_report_followup_host import intake
from test_daily_report_evidence_versions import flow, CHILD


@pytest.mark.parametrize('ready', [True, False])
def test_plan_preserves_stage_binding_slot_identity_and_deferred_reason(flow, monkeypatch, ready):
    db, repo, _, _ = flow
    sid = intake(flow)
    with db.connect() as c:
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''', (CHILD,))
        c.commit()
    binding = {'domain_id': 'pet.writing.email', 'knowledge_node_id': 'node-pet-writing-email',
        'diagnostic_item_id': 'exact-writing', 'source_version': 'a' * 64,
        'review_task_id': None, 'not_before': None, 'execution_status': 'ready',
        'stage_constraint': {'version': 'specialized_stages_v1', 'current_stage': 'baseline', 'prerequisite_version': 'b' * 64},
        'mastery_claim_allowed': False}
    task = {'homework_id': None, 'subject': '英语', 'title': '写作阶段原题', 'minutes': 12,
        'source_type': 'pet_daily', 'source_ref': 'exact-writing', 'task_type': 'writing',
        'status': 'pending', 'completion_standard': '保存原稿', 'cognitive_load': 'medium',
        'planning_evidence': binding}
    def allocate(c, *_args, **_kwargs):
        assert c.in_transaction
        return {'items': [task] if ready else [], 'opportunities': [], 'missing_skills': [],
            'version': 'isolated-allocation', 'mastery_claim_allowed': False,
            'unplaced_obligations': [] if ready else [{**task, 'reason_code': 'stage_not_due', 'reason': '还未到复测时间。'}]}
    checked = []
    def revalidate(c, child, track, **kwargs):
        assert c.in_transaction and child == CHILD and track == 'pet'
        checked.append(kwargs['binding'])
        return {'status': 'ready'}
    monkeypatch.setattr('home_study_agent.pet_training_plan.allocate_pet_practice', allocate)
    monkeypatch.setattr('home_study_agent.specialized_stages.revalidate_stage_action', revalidate)
    result = repo.generate_plan(sid, 'pet-stage-plan')
    decisions = [row for row in result['constraint_decisions'] if row['source_ref'] == 'exact-writing']
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision['metadata']['planning_evidence']['stage_constraint'] == binding['stage_constraint']
    if ready:
        assert checked and decision['decision'] == 'scheduled'
        slot = next(item for item in result['plan_items'] if item['source_ref'] == 'exact-writing')
        assert decision['metadata']['plan_item_id'] == slot['plan_item_id']
        assert slot['expected_minutes'] == 12
    else:
        assert not checked and decision['decision'] == 'deferred'
        assert decision['requested_minutes'] == 12 and decision['allocated_minutes'] == 0
        assert decision['reason'] == '还未到复测时间。'
