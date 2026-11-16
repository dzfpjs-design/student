import asyncio
import json
from datetime import datetime, timezone

import pytest

from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from home_study_agent.model_gateway import ModelResult
from test_specialized_supply import prepared
from home_study_agent.database import Database
from home_study_agent.local_supply import LocalSupplyError, validate_item, score_local_item
from home_study_agent.specialized_program import SpecializedProgramService
from home_study_agent.specialized_stages import _item_source, revalidate_stage_action
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_specialized_stage_source_review import decision

VERSION = 'specialized_closed_exact_v1'


def generated(stage='parallel', index=0):
    prompts = ['从四种水果中任取两种，不考虑顺序，共有多少种选法？只写整数。',
               '三张不同的卡片排成一列，第一张有三种选法，总共可以排出几种顺序？只写整数。',
               '两件不同颜色的外套和三条不同的裤子，每次各选一件，共有几套搭配？只写整数。']
    return {'stage': stage, 'prompt': prompts[index % 3],
            'scoring_contract': {'schema': VERSION, 'answer_type': 'text', 'assessment_mode': 'objective',
                                 'expected_answer': {'local_rule': 'local_exact_v1', 'exact_answers': ['6']}},
            'explanation': '按题目限定的选择规则枚举，可以得到六种。', 'difficulty': 2}


class ClosedGateway:
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        context = json.loads(request.messages[1]['content'])
        return ModelResult(content=json.dumps({'candidates': [generated(stage, index)
            for index, stage in enumerate(context['required_stages'])]}, ensure_ascii=False),
            provider='test-only', model='typed-fixture', latency_ms=1, prompt_tokens=0, completion_tokens=0)


def context(tmp_path):
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db, ClosedGateway())
    jobs = service.enqueue_gaps('child-primary-4', 'competition_math', 'contract-queue')
    with db.connect() as c:
        job_id = next(r[0] for r in c.execute('SELECT job_id FROM specialized_supply_job WHERE domain_id=?',
                                             ('competition_math.counting',)))
        value = service._context(c, job_id)
    return db, service, value, job_id


def test_typed_contract_survives_validation_and_uses_whole_answer_scoring(tmp_path):
    _, service, value, _ = context(tmp_path)
    result = service._validate(value, {'candidates': [generated()]})
    assert len(result) == 1
    assert result[0]['expected_answer'] == {'local_rule': 'local_exact_v1',
        'exact_answers': ['6'], 'answer_format': 'reviewed_exact_text_v1'}
    assert result[0]['validation']['qualification'] == 'structural_contract_only_parent_reference_review_required'


def test_keywords_only_cannot_become_a_reviewable_closed_candidate(tmp_path):
    _, service, value, _ = context(tmp_path)
    item = generated()
    item.pop('scoring_contract')
    item['expected_answer'] = {'keywords': ['six', 'reason']}
    assert service._validate(value, {'candidates': [item]}) == []


@pytest.mark.parametrize('answers', [[], [''], [6], ['6', '６'], ['yes'] * 25])
def test_malformed_or_ambiguous_exact_contract_is_rejected(tmp_path, answers):
    _, service, value, _ = context(tmp_path)
    item = generated()
    item['scoring_contract']['expected_answer']['exact_answers'] = answers
    assert service._validate(value, {'candidates': [item]}) == []


def model_candidates(tmp_path):
    db, service, value, job_id = context(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE specialized_supply_job SET status='blocked' WHERE job_id!=?", (job_id,))
        c.commit()
    job = asyncio.run(service.process_next())
    assert job['job_id'] == job_id and job['status'] == 'completed'
    assert len(service.gateway.requests) == 1
    return db, service, service.list('child-primary-4', 'needs_review')['items']


def counting(db):
    progress = SpecializedProgramService(db).stage_progress('child-primary-4', 'competition_math')
    return next(d for d in progress['domains'] if d['domain_id'] == 'competition_math.counting')['nodes'][0]


def publish(service, candidates, explicit=True):
    items = {}
    for row in candidates:
        stage = {'discrimination': 'near_transfer', 'transfer': 'far_transfer'}.get(row['stage'])
        review = decision(service.stage_review_context(row['child_id'], row['candidate_id']), stage) if stage and explicit else None
        items[row['stage']] = service.review(row['candidate_id'], 'approve', 'Fixture human checked full reference and context',
            'review-' + row['candidate_id'], stage_review=review)
    return items


def test_generated_reviewed_contract_executes_real_current_stages_and_survives_restart(tmp_path):
    db, service, candidates = model_candidates(tmp_path)
    assert not counting(db)['next_action']['diagnostic_item_id']
    items = publish(service, candidates)
    repo = StudyRepository(db)
    sid = repo.start_session('child-primary-4', 'closed-stage-session')['session_id']
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    prompts = DiagnosticPromptService(db)
    for label, stage in [('parallel', 'baseline'), ('discrimination', 'near_transfer'), ('transfer', 'far_transfer')]:
        before = counting(db)
        action = before['next_action']
        assert before['current_stage'] == stage and action['execution_status'] == 'ready'
        item_id = items[label]['diagnostic_item_id']
        assert action['diagnostic_item_id'] == item_id
        with db.connect() as c:
            assert revalidate_stage_action(c, 'child-primary-4', 'competition_math', binding=action)['status'] == 'ready'
            item = validate_item(c, 'child-primary-4', item_id)
            assert score_local_item(item, '6')[0] == 1
            assert score_local_item(item, '6 but maybe 7')[0] == 0
        prompt = prompts.prepare(sid, track='competition_math', preferred_item_id=item_id,
                                 client_action_id='closed-prepare-' + stage)['prompt']
        receipt = prompts.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                                 answer='6', client_action_id='closed-answer-' + stage)['attempt']
        assert receipt['score_status'] == 'verified_local' and receipt['correctness'] == 1
        assert counting(db)['qualified_stage'] == stage
    restart = Database(db.path)
    assert counting(restart) == counting(db)
    assert counting(restart)['current_stage'] == 'delayed_retention'
    assert counting(restart)['next_action']['execution_status'] != 'ready'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 3


def test_model_stage_labels_without_explicit_review_cannot_grant_transfer(tmp_path):
    db, service, candidates = model_candidates(tmp_path)
    items = publish(service, candidates, explicit=False)
    with db.connect() as c:
        for stage, row in items.items():
            item = validate_item(c, 'child-primary-4', row['diagnostic_item_id'])
            source, reason = _item_source(c, 'child-primary-4', item, datetime.now(timezone.utc))
            assert reason is None and source['score_qualified'] is True
            assert source['stage'] == ('baseline' if stage == 'parallel' else None)


@pytest.mark.parametrize('fault', ['retract', 'seed_withdrawn', 'both_payloads_changed', 'old_ruleset', 'answer_type', 'assessment_mode'])
def test_current_contract_changes_reject_old_stage_binding_and_preserve_review(tmp_path, fault):
    db, service, candidates = model_candidates(tmp_path)
    item = publish(service, candidates)['parallel']
    action = counting(db)['next_action']
    with db.connect() as c:
        if fault == 'retract':
            c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE candidate_id=?", (item['candidate_id'],))
        elif fault == 'seed_withdrawn':
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (item['seed_item_id'],))
        elif fault == 'both_payloads_changed':
            c.execute("UPDATE specialized_supply_candidate SET prompt=prompt||' changed' WHERE candidate_id=?", (item['candidate_id'],))
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (item['diagnostic_item_id'],))
        elif fault == 'old_ruleset':
            validation = {**item['validation'], 'ruleset': 'specialized_supply_local_gate_v1'}
            c.execute('UPDATE specialized_supply_candidate SET validation_json=? WHERE candidate_id=?',
                      (json.dumps(validation), item['candidate_id']))
        else:
            c.execute(f'UPDATE diagnostic_item SET {fault}=? WHERE diagnostic_item_id=?',
                      ('writing' if fault == 'answer_type' else 'pet_reference_rubric', item['diagnostic_item_id']))
        c.commit()
    with Database(db.path).connect() as c:
        with pytest.raises(LocalSupplyError):
            validate_item(c, 'child-primary-4', item['diagnostic_item_id'])
        assert revalidate_stage_action(c, 'child-primary-4', 'competition_math', binding=action)['status'] == 'blocked'
        assert c.execute('SELECT review_action_id FROM specialized_supply_candidate WHERE candidate_id=?',
                         (item['candidate_id'],)).fetchone()[0] == item['review_action_id']


def test_two_children_and_repeated_review_keep_original_binding(tmp_path):
    db, service, candidates = model_candidates(tmp_path)
    item = publish(service, candidates)['parallel']
    assert service.review(item['candidate_id'], 'approve', item['review_note'], item['review_action_id']) == item
    with db.connect() as c:
        with pytest.raises(LocalSupplyError):
            validate_item(c, 'child-junior-3', item['diagnostic_item_id'])
        action = counting(db)['next_action']
        assert revalidate_stage_action(c, 'child-junior-3', 'competition_math', binding=action)['status'] == 'blocked'
        assert c.execute('SELECT count(*) FROM diagnostic_item WHERE diagnostic_item_id=?', (item['diagnostic_item_id'],)).fetchone()[0] == 1


def test_legacy_keywords_and_empty_human_review_cannot_publish(tmp_path):
    db, service, candidates = model_candidates(tmp_path)
    row = candidates[0]
    with pytest.raises(ValueError, match='REVIEW_NOTE_REQUIRED'):
        service.review(row['candidate_id'], 'approve', '', 'empty-reference-review')
    with db.connect() as c:
        c.execute('UPDATE specialized_supply_candidate SET expected_answer_json=?,validation_json=? WHERE candidate_id=?',
                  (json.dumps({'keywords': ['6', 'reason']}), json.dumps({'passed': True, 'ruleset': 'specialized_supply_local_gate_v1'}), row['candidate_id']))
        c.commit()
    with pytest.raises(LocalSupplyError, match='TYPED_CLOSED_REVIEW_REQUIRED'):
        service.review(row['candidate_id'], 'approve', 'Old untyped model reference', 'old-model-review')
    with db.connect() as c:
        assert c.execute('SELECT diagnostic_item_id FROM specialized_supply_candidate WHERE candidate_id=?',
                         (row['candidate_id'],)).fetchone()[0] is None


def test_legacy_pending_model_rows_do_not_hide_blueprint_replacement_gap(tmp_path):
    db = prepared(tmp_path)
    from test_specialized_supply import FakeSupplyGateway
    service = SpecializedDiagnosticSupplyService(db, FakeSupplyGateway())
    service.enqueue_blueprint_gaps('child-primary-4', 'pet', '英语', 'old-blueprint-queue')
    while asyncio.run(service.process_next()) is not None:
        pass
    with db.connect() as c:
        c.execute("UPDATE specialized_supply_candidate SET validation_json=?",
                  (json.dumps({'passed': True, 'ruleset': 'specialized_supply_local_gate_v1'}),))
        c.commit()
    readiness = service.blueprint_gaps('child-primary-4', 'pet', '英语')
    assert readiness['pending_review_count'] == 0
    queued = service.enqueue_blueprint_gaps('child-primary-4', 'pet', '英语', 'replacement-blueprint-queue')
    assert queued['created_count'] > 0
    with db.connect() as c:
        for job_id in queued['created_job_ids']:
            job = c.execute('SELECT * FROM specialized_supply_job WHERE job_id=?', (job_id,)).fetchone()
            domain = next(d for section in readiness['sections'] for d in section['domains']
                          if d['domain_id'] == job['domain_id'])
            expected = max(3, readiness['recommended_domain_targets'][job['domain_id']] - domain['active_count'])
            assert len(json.loads(job['stages_json'])) == expected
