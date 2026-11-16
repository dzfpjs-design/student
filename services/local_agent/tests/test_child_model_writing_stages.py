"""Actual writing judgments must reach the durable child state, not raw scores."""
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_rubric import RubricEvidenceService
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, CHILD


def writing_node(payload):
    stage = payload['specialized_stage_evidence']
    assert stage['local_only'] and stage['cloud_transmission_allowed'] is False
    return next(n for track in stage['tracks'] for domain in track['domains']
                for n in domain['nodes'] if n['knowledge_node_id'] == 'node-pet-writing-email')


def test_reliable_writing_stage_reaches_model_and_retraction_survives_restart(writing_chain):
    db = writing_chain[0]
    service = ChildModelService(db)
    before = service.snapshot(CHILD)
    initial = writing_node(before['payload'])
    assert initial['qualified_stage'] == 'baseline'
    assessment, _, _ = complete_transfer_chain(writing_chain)
    changed = service.snapshot(CHILD)
    after = writing_node(changed['payload'])
    assert changed['snapshot_id'] != before['snapshot_id']
    assert after['qualified_stage'] == 'far_transfer'
    assert after['current_stage'] == 'delayed_retention'
    assert after['mastery_claim_allowed'] is False
    assert after['certification_claim_allowed'] is False
    assert writing_node(service.compact_for_agent(CHILD))['gates'] == after['gates']
    RubricEvidenceService(db).review(CHILD, assessment['assessment_id'],
        expected_revision=1, client_action_id='retract-model-writing-far', reviewer_id='parent',
        reviewer_role='parent', operation='retract', reason='Original scoring requires correction')
    restored = ChildModelService(Database(db.path)).snapshot(CHILD)
    assert restored['snapshot_id'] != changed['snapshot_id']
    assert writing_node(restored['payload'])['qualified_stage'] == 'near_transfer'
    assert service.snapshot(CHILD)['snapshot_id'] == restored['snapshot_id']


def test_writing_stage_model_source_withdrawal_and_child_isolation(writing_chain):
    db, _, _, _, supply, _, far = writing_chain
    complete_transfer_chain(writing_chain)
    service = ChildModelService(db)
    before = service.snapshot(CHILD)
    other_before = service.snapshot('child-junior-3')
    assert {t['track'] for t in other_before['payload']['specialized_stage_evidence']['tracks']} == {'junior_exam'}
    supply.withdraw(CHILD, far['candidate_id'], actor='parent', note='Source needs revision',
                    action_id='withdraw-child-model-writing-source', expected_version=4)
    after = service.snapshot(CHILD)
    assert after['snapshot_id'] != before['snapshot_id']
    assert writing_node(after['payload'])['qualified_stage'] != 'far_transfer'
    assert service.snapshot('child-junior-3')['snapshot_id'] == other_before['snapshot_id']


def test_retention_judgment_updates_local_model_without_promoting_reference_score(writing_chain, monkeypatch):
    from home_study_agent.agent_runtime import _without_local_only
    from test_writing_stage_review_consumers import due_answer, review_due
    db, _, prompt, text, scored, _ = due_answer(writing_chain, monkeypatch)
    service = ChildModelService(db)
    before = service.snapshot(CHILD)
    assert writing_node(before['payload'])['qualified_stage'] == 'far_transfer'
    with db.connect() as c:
        raw_before = dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?',
            (scored['attempt']['evidence_id'],)).fetchone())
    assessment, body = review_due(db, writing_chain[1], prompt, text, scored)
    RubricEvidenceService(db).review(CHILD, assessment['assessment_id'], **body)
    after = service.snapshot(CHILD)
    assert after['snapshot_id'] != before['snapshot_id']
    assert writing_node(after['payload'])['qualified_stage'] == 'delayed_retention'
    context = service.compact_for_agent(CHILD)
    assert writing_node(context)['qualified_stage'] == 'delayed_retention'
    assert 'specialized_stage_evidence' not in _without_local_only(context)
    with Database(db.path).connect() as c:
        raw_after = dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?',
            (scored['attempt']['evidence_id'],)).fetchone())
    assert raw_after == raw_before and raw_after['independent'] == 0
