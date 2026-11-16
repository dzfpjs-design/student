"""Actual reviewed writing stages and borrowed review scheduling; no child outcome claim."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_stages import project_specialized_stages
from home_study_agent.writing_response_origin import WritingOriginalService, bind_written_attempt
from home_study_agent.writing_rubric import prepare_writing_review
from test_writing_source_publication import prepared as supply_fixture, source_packet
from test_writing_teaching import ready
from test_writing_response_origin import CHILD
from test_pet_media_evidence import local_review


def writing_node(c, *, as_of=None):
    projection = project_specialized_stages(c, CHILD, 'pet', as_of=as_of)
    return next(n for d in projection['domains'] for n in d['nodes'] if n['knowledge_node_id']=='node-pet-writing-email')


def published_stage(service, spec, stage):
    draft = service.generate_draft(CHILD, {**spec,'stage':stage}, author='local:source-author',
        action_id='stage-draft-'+stage, expected_scope_version=2)
    checked = service.validate(CHILD, draft['candidate_id'], actor='parent', action_id='stage-validate-'+stage, expected_version=1)
    context = checked['stage_review_context']
    review = {'schema':context['schema'],'stage':stage,'binding_version':context['binding_version'],
        'source_quote':context['source_prompt'],'candidate_quote':context['candidate_prompt'],
        'same_skill_rationale':'Both require an organized email with the stated information and an invitation.',
        'changed_context_rationale':'This reviewed task has a different event, purpose and communicative setting.'}
    service.review(CHILD, draft['candidate_id'], actor='parent', decision='approve', note='Isolated authored transfer source',
        stage_review=review, action_id='stage-review-'+stage, expected_version=2)
    return service.publish(CHILD, draft['candidate_id'], actor='parent', action_id='stage-publish-'+stage, expected_version=3)


def reviewed_answer(db, sid, prompts, originals, item_id, text, key):
    p = prompts.prepare(sid, track='pet', preferred_item_id=item_id, client_action_id=key+'-prompt')['prompt']
    original = originals.capture_native_response(sid, prompt_id=p['prompt_id'], item_version=p['item_version'],
        text=text, client_action_id=key+'-original')
    scored = prompts.answer(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], answer=text, client_action_id=key+'-answer')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind_written_attempt(c, child_id=CHILD, session_id=sid, prompt_id=p['prompt_id'], item_version=p['item_version'],
            attempt_id=scored['attempt']['attempt_id'], original_artifact_id=original['artifact_id'])
        # The isolated reviewer helper opens its own goal for each real answer.
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",
            (datetime.now(timezone.utc).isoformat(), CHILD))
        c.commit()
    assessment = local_review((db,None,sid,p,None,None), p, text, approve=False, scored=scored)
    with db.connect() as c:
        binding = prepare_writing_review(c, CHILD, assessment['assessment_id'])
    body = dict(expected_revision=0, client_action_id=key+'-rubric', reviewer_id='parent', reviewer_role='parent',
        operation='approve', reason='Synthetic original manuscript review, not a calibrated exam score',
        independent=True, hint_level=0, review_profile='pet_writing_text_v1',
        writing_evidence={'binding_version':binding['binding_version'],
            'task_point_evidence':[{'task_point':point,'response_quote':text} for point in binding['task_points']]},
        dimensions=[dict(dimension_id=d['dimension_id'],score=3,response_quotes=[text],rationale='Read original task evidence') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=point,response_quote=text) for point in assessment['criteria']])
    RubricEvidenceService(db).review(CHILD, assessment['assessment_id'], **body)
    return assessment, scored, body


@pytest.fixture
def writing_chain(tmp_path):
    db, supply, spec, near_packet = supply_fixture(tmp_path)
    from home_study_agent.period_reports import PeriodReportService
    PeriodReportService(db).initialize()
    far_packet = {**source_packet(),
        'prompt':'Write an email to Ben. Describe your new science club, explain what you want to build, and invite him to join. Write 35-55 words.',
        'task_points':['Describe the science club','Explain what you want to build','Invite Ben to join']}
    material = KnowledgeBaseService(db).import_source(CHILD, dict(source_type='school_material',
        title='Isolated authored writing sources', subject='英语', grade_band='四年级', version_label='isolated-2026',
        source_ref='isolated:writing-stages', license_status='public', parent_reviewed=True,
        content_text='\n'.join(json.dumps(p,ensure_ascii=False) for p in (near_packet,far_packet))))
    supply.select_curriculum(CHILD, dict(grade='四年级',subject='英语',edition='isolated-2026',semester='上学期',
        standard_scope='第二学段',textbook_source_id=material['source_id']), actor='parent',action_id='stages-scope',expected_version=1)
    with db.connect() as c:
        excerpts = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (material['source_id'],)).fetchall()
    near = published_stage(supply, {**spec,'excerpt_id':excerpts[0][0]}, 'near_transfer')
    far = published_stage(supply, {**spec,'excerpt_id':excerpts[1][0]}, 'far_transfer')
    sid = StudyRepository(db).start_session(CHILD,'writing-stage-session')['session_id']
    prompts = DiagnosticPromptService(db)
    parent = prompts.prepare(sid,track='pet',preferred_item_id='diag-pet-writing-1',client_action_id='baseline')['prompt']
    originals = WritingOriginalService(db)
    ready((db,sid,prompts,parent,originals))
    return db,sid,prompts,originals,supply,near,far


def complete_transfer_chain(case):
    db,sid,prompts,originals,_,near,far = case
    reviewed_answer(db,sid,prompts,originals,near['diagnostic_item_id'],
        'Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance because the music was cheerful. Would you like to come with me to our next concert in June? Best wishes, Mia.', 'near')
    with db.connect() as c:
        stage = writing_node(c)
        assert stage['current_stage'] == 'far_transfer'
        assert stage['next_action']['diagnostic_item_id'] == far['diagnostic_item_id']
    return reviewed_answer(db,sid,prompts,originals,far['diagnostic_item_id'],
        'Dear Ben, our new science club meets every Friday in the school library. We work together on small projects. I want to build a model bridge because I enjoy designing things. Would you like to join us next week? Best wishes, Mia.', 'far')


def reconcile(c, child=CHILD, **kwargs):
    from home_study_agent.writing_stage_reviews import reconcile_writing_stage_reviews
    return reconcile_writing_stage_reviews(c, child, **kwargs)


def test_real_chain_creates_one_due_review_in_borrowed_transaction_and_restart(writing_chain):
    case = writing_chain
    assessment, scored, _ = complete_transfer_chain(case)
    db = case[0]
    with db.connect() as c:
        assert writing_node(c)['qualified_stage'] == 'far_transfer'
        with pytest.raises(ValueError, match='TRANSACTION_REQUIRED'):
            reconcile(c)
        c.execute('BEGIN IMMEDIATE')
        before_attempts = c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]
        result = reconcile(c)
        assert result['created'] == [] and len(result['retained']) == 1, result
        task = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (result['retained'][0],)).fetchone()
        assert task['source_evidence_id'] == scored['attempt']['evidence_id']
        assert task['diagnostic_item_id'] == case[6]['diagnostic_item_id']
        assert task['status'] == 'pending' and task['result_evidence_id'] is None
        assert datetime.fromisoformat(task['due_at']) >= datetime.fromisoformat(scored['attempt']['created_at'])+timedelta(days=7)
        assert reconcile(c)['created'] == []
        assert c.in_transaction
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == before_attempts
        c.rollback()
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 1
        c.execute('BEGIN IMMEDIATE')
        result = reconcile(c)
        c.commit()
    with Database(db.path).connect() as c:
        c.execute('BEGIN IMMEDIATE')
        again = reconcile(c)
        assert again['created'] == [] and again['retained'] == result['retained']
        c.rollback()


def test_baseline_and_uncalibrated_reference_do_not_create_retention(writing_chain):
    with writing_chain[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result = reconcile(c)
        assert result['created'] == []
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 0
        c.rollback()


@pytest.mark.parametrize('fault', ['review','original','source'])
def test_withdrawal_cancels_pending_owned_review_not_attempt_history(writing_chain, fault):
    case = writing_chain
    assessment, scored, _ = complete_transfer_chain(case)
    with case[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        created = reconcile(c)['retained']
        c.commit()
    if fault == 'review':
        RubricEvidenceService(case[0]).review(CHILD,assessment['assessment_id'],expected_revision=1,client_action_id='withdraw-far',
            reviewer_id='parent',reviewer_role='parent',operation='retract',reason='Withdraw review')
    elif fault == 'source':
        case[4].withdraw(CHILD,case[6]['candidate_id'],actor='parent',note='Withdraw source',action_id='withdraw-far',expected_version=4)
    else:
        with case[0].connect() as c:
            c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE json_extract(details_json,'$.attempt_binding.attempt_id')=?", (scored['attempt']['attempt_id'],))
            c.commit()
    with case[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        result = reconcile(c)
        assert result['cancelled'] == ([] if fault == 'review' else created) and result['created'] == [], result
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (created[0],)).fetchone()[0]=='cancelled'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 3
        c.rollback()


def test_real_stage_producer_near_far_and_waiting_due_not_false_missing(writing_chain):
    from home_study_agent.pet_writing_stage_plan import project_writing_stage_plan
    from home_study_agent.pet_training_plan import allocate_pet_practice
    case = writing_chain
    with case[0].connect() as c:
        result = project_writing_stage_plan(c,CHILD,as_of=datetime.now(timezone.utc).isoformat())
        email = next(i for i in result['items'] if i['planning_evidence']['knowledge_node_id']=='node-pet-writing-email')
        assert email['source_ref'] == case[5]['diagnostic_item_id']
        assert email['planning_evidence']['stage_constraint']['current_stage'] == 'near_transfer'
    complete_transfer_chain(case)
    with case[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        created = reconcile(c)['retained']
        c.commit()
        result = project_writing_stage_plan(c,CHILD,as_of=datetime.now(timezone.utc).isoformat())
        gap = next(i for i in result['unplaced_obligations'] if i['planning_evidence']['knowledge_node_id']=='node-pet-writing-email')
        assert gap['reason_code'] == 'stage_not_due'
        assert gap['planning_evidence']['review_task_id'] == created[0]
        assert not any(i['source_ref']==gap['source_ref'] for i in result['items'])
        due = gap['planning_evidence']['not_before']
        now = datetime.fromisoformat(due)+timedelta(seconds=1)
        ready = project_writing_stage_plan(c,CHILD,as_of=now.isoformat())
        item = next(i for i in ready['items'] if i['planning_evidence']['knowledge_node_id']=='node-pet-writing-email')
        assert item['source_ref'] == gap['source_ref']
        assert item['planning_evidence']['review_task_id'] == created[0]
        allocation = allocate_pet_practice(c,CHILD,15,30)
        assert 'writing' not in allocation['missing_skills']


@pytest.mark.parametrize('fault', ['regular_7d','due_later','due_earlier','cancelled','wrong_item','foreign_child','ambiguous'])
def test_existing_review_keeps_exact_binding_or_blocks_without_silent_repair(writing_chain, fault):
    case = writing_chain
    complete_transfer_chain(case)
    with case[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        rid = reconcile(c)['retained'][0]
        if fault == 'regular_7d':
            c.execute("UPDATE review_task SET interval_label='7d' WHERE review_task_id=?", (rid,))
        elif fault in {'due_later','due_earlier'}:
            due = datetime.fromisoformat(c.execute('SELECT due_at FROM review_task WHERE review_task_id=?', (rid,)).fetchone()[0])
            changed = due + timedelta(days=1 if fault == 'due_later' else -1)
            c.execute('UPDATE review_task SET due_at=? WHERE review_task_id=?', (changed.isoformat(),rid))
        elif fault == 'cancelled':
            c.execute("UPDATE review_task SET status='cancelled' WHERE review_task_id=?", (rid,))
        elif fault == 'wrong_item':
            c.execute("UPDATE review_task SET diagnostic_item_id='diag-pet-speaking-1' WHERE review_task_id=?", (rid,))
        elif fault == 'foreign_child':
            c.execute("UPDATE review_task SET child_id='child-junior-3' WHERE review_task_id=?", (rid,))
        else:
            row = dict(c.execute('SELECT * FROM review_task WHERE review_task_id=?', (rid,)).fetchone())
            row.update(review_task_id='legacy-duplicate', interval_label='7d')
            c.execute('INSERT INTO review_task ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        before = list(c.iterdump())
        result = reconcile(c)
        assert result['created'] == []
        if fault in {'regular_7d','due_later'}:
            assert result['retained'] == [rid] and result['blocked'] == []
        else:
            assert result['retained'] == [] and result['blocked']
        assert list(c.iterdump()) == before
        c.rollback()


def test_wrong_child_and_invalid_clock_do_not_write(writing_chain):
    case = writing_chain
    complete_transfer_chain(case)
    with case[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = list(c.iterdump())
        with pytest.raises(ValueError):
            reconcile(c, 'child-junior-3')
        with pytest.raises(ValueError):
            reconcile(c, as_of='2026-09-08T00:00:00')
        assert list(c.iterdump()) == before
        c.rollback()


def test_invalidated_far_review_before_scheduling_never_creates_task(writing_chain):
    case = writing_chain
    assessment, _, body = complete_transfer_chain(case)
    body.update(expected_revision=1,client_action_id='corrected-far')
    body['dimensions'][0]['score'] = 0
    RubricEvidenceService(case[0]).review(CHILD,assessment['assessment_id'],**body)
    with case[0].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert reconcile(c)['created'] == []
        assert c.execute('SELECT status FROM review_task').fetchone()[0] == 'cancelled'
        c.rollback()
