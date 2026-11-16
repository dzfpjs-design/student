"""Historical authority, mixed review identity and actual outbound serialization."""
import json
from datetime import datetime, timedelta, timezone

from home_study_agent.child_model import ChildModelService
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.learning_feedback import build_learning_feedback
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.strategy_evolution import StrategyEvolutionService
from test_school_open_reviews import school_case, answer_and_review, CHILD
from test_writing_stage_review_consumers import advance_clock


def test_historical_basis_survives_window_then_real_retraction(school_case, monkeypatch, tmp_path):
    db, client, headers = school_case[:3]
    _, saved, assessment, path, _ = answer_and_review(school_case, score=0)
    service = StrategyEvolutionService(db)
    candidate = service.maybe_propose(CHILD)
    assert candidate['status'] == 'draft'
    clock = advance_clock(monkeypatch, datetime.now(timezone.utc) + timedelta(days=91))
    with db.connect() as c:
        from home_study_agent.writing_score import current_written_score
        current = current_written_score(c, CHILD, assessment['assessment_id'], now=clock.now(timezone.utc))
        assert current['decision']['status'] == 'reviewed_human'
        assert build_learning_feedback(c, CHILD, clock.now(timezone.utc))['nodes'] == []
    after = service.get(candidate['experiment_id'])
    (tmp_path / 'window-before-retract.json').write_text(json.dumps({
        'strategy': candidate['experiment_id'], 'authority_still_current': True,
        'current_planning_nodes': [], 'after_91_days': after['status']}))
    assert after['status'] == 'draft', 'Planning-window expiry must not revoke still-current historical authority'
    assert after['candidate_config']['selection'] == candidate['candidate_config']['selection']
    unlock = client.post('/v1/parent/unlock', json={'pin': '8264'})
    headers = {'X-HSC-Parent': unlock.json()['parent_token']}
    result = client.post(path, headers=headers, json={'operation': 'retract', 'expected_revision': 1,
        'client_action_id': 'old-observation-retract', 'reason': 'Withdraw historical original score'})
    assert result.status_code == 200, result.text
    assert service.get(candidate['experiment_id'])['status'] == 'rolled_back'


def test_mixed_generic_and_school_reviews_choose_original_stage_without_writes(school_case, monkeypatch, tmp_path):
    from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
    db, client, headers, sid = school_case[:4]
    supply = SpecializedDiagnosticSupplyService(db)
    job = supply.enqueue_local('diag-j3-discriminant-1', CHILD, 'mixed-local-exact', count=1)
    supply.process_local_pending()
    with db.connect() as c:
        cid = c.execute('SELECT candidate_id FROM specialized_supply_candidate WHERE job_id=?', (job['job_id'],)).fetchone()[0]
    item = supply.qualify_local(cid, CHILD, 'mixed-local-qualify')['diagnostic_item_id']
    exact = LearningStateService(db).submit_diagnostic(sid, item, 'unknown', 'generic-before-original')
    assert exact['correctness'] == 0
    _, saved, _, _, _ = answer_and_review(school_case, score=0)
    original = saved['attempt']['evidence_id']
    clock = advance_clock(monkeypatch, datetime.now(timezone.utc) + timedelta(days=8))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        now = clock.now(timezone.utc)
        reviews = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE child_id=? ORDER BY due_at', (CHILD,))]
        school = next(r for r in reviews if r['source_evidence_id'] == original)
        assert reviews[0]['source_evidence_id'] == exact['evidence_id']
        for review in reviews:
            StudyRepository._validate_review_source(c, review['review_task_id'], CHILD, allowed_statuses=('pending',))
        feedback = build_learning_feedback(c, CHILD, now)
        node = next(n for n in feedback['nodes'] if n['knowledge_node_id'] == 'node-j3-math-discriminant')
        assert set(node['evidence_refs']) == {exact['evidence_id'], original}
        roadmap = {'source_version': 'isolated-current-route',
            'next_daily_action': {'action': 'clarify_then_recheck', 'dispatchable': True,
                'knowledge_node_id': node['knowledge_node_id']},
            'prerequisite_path': [{**node, 'blocked_by': []}]}
        cycle = {'track': 'junior_exam_readiness', 'child_id': CHILD, 'cycle_id': 'isolated-cycle'}
        before = '\n'.join(c.iterdump())
        result = GoalProgramService._feedback_execution(c, cycle, feedback, 30, prepare=True, roadmap=roadmap)
        assert '\n'.join(c.iterdump()) == before
        (tmp_path / 'mixed-review-result.json').write_text(json.dumps({
            'result': result, 'expected_school_review': school['review_task_id'],
            'expected_school_item': school['diagnostic_item_id'], 'all_sources_valid': True,
            'whole_db_unchanged': True}))
        assert result['source_ref'] == school['review_task_id'], result
        assert result['diagnostic_item_id'] == school['diagnostic_item_id']
        assert result['status'] == 'review_scheduled'
        c.rollback()


def test_actual_planner_http_payload_excludes_original_dimension_sentinel(school_case, monkeypatch, tmp_path):
    from home_study_agent.model_gateway import ModelGateway, ModelEndpoint
    from home_study_agent.agent_runtime import build_planner_messages, CapabilityRegistry
    db, client, headers, sid = school_case[:4]
    _, saved, _, _, _ = answer_and_review(school_case, score=0)
    candidate = StrategyEvolutionService(db).maybe_propose(CHILD)
    compact = ChildModelService(db).compact_for_agent(CHILD)
    facts = compact['learning_feedback']['nodes'][0]['qualified_original_observations']
    criteria = [d['criterion'] for d in facts[0]['dimensions']]
    assert criteria and all(criteria)
    local = json.dumps(compact, ensure_ascii=False)
    assert all(criterion in local for criterion in criteria)
    sentinel = 'PRIVATE-DERIVED-CRITERION-f7028624-keep-local'
    real_compact = ChildModelService.persisted_compact_for_agent
    def marked_compact(self, child):
        value = real_compact(self, child)
        dimensions = value['learning_feedback']['nodes'][0]['qualified_original_observations'][0]['dimensions']
        dimensions[0]['criterion'] += sentinel
        return value
    monkeypatch.setattr(ChildModelService, 'persisted_compact_for_agent', marked_compact)
    runtime = client.app.state.agent_orchestrator.agent_runtime
    context = runtime.assemble_context(sid, ())
    assert sentinel in json.dumps(context, ensure_ascii=False)
    messages = build_planner_messages(context, '继续练习', CapabilityRegistry())
    # The production HTTP serializer, not a hand-written sanitized payload.
    payload = ModelGateway.request_payload(ModelEndpoint('test', 'synthetic-unused',
        'http://127.0.0.1:1/v1', 'synthetic-planner'), messages, 'agent_plan')
    outbound = json.dumps(payload, ensure_ascii=False)
    for private in [sentinel, *criteria, 'qualified_original_observations', 'original_binding_version',
                    'score_authority_id', saved['attempt']['evidence_id'], candidate['experiment_id']]:
        assert private not in outbound, private
    (tmp_path / 'outbound-result.json').write_text(json.dumps({
        'request_types': ['agent_plan'],
        'local_criterion_count': len(criteria), 'criteria_or_observation_identity_transmitted': False,
        'sentinel': sentinel, 'provider': 'production context/messages/HTTP-payload builders; no POST'}))
