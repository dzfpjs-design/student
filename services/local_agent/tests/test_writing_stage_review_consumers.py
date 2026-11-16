"""Production writing review transactions on synthetic originals, never fake exact scores."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.learning_rubric import RubricEvidenceService
from test_writing_stage_reviews import writing_chain, complete_transfer_chain, writing_node
from test_writing_response_origin import CHILD


def advance_clock(monkeypatch, at):
    import sys
    # This projection is otherwise imported lazily after the synthetic day jump.
    # Its source/answer timestamps must use the same clock as their writers.
    from home_study_agent import oral_teaching
    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    # A frozen clock must still accept real datetime values parsed in modules
    # imported after the clock was installed.
    class Clock(datetime, metaclass=ClockType):
        current = at
        _hsc_test_clock = True

        @classmethod
        def now(cls, tz=None):
            cls.current += timedelta(microseconds=10)
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    for name, module in list(sys.modules.items()):
        if name.startswith('home_study_agent.') or name in {'test_pet_media_evidence','test_writing_stage_reviews'}:
            current_datetime = getattr(module, 'datetime', None)
            if current_datetime is datetime or getattr(current_datetime, '_hsc_test_clock', False):
                monkeypatch.setattr(module, 'datetime', Clock)
            if callable(getattr(module, 'utc_now', None)):
                returns_datetime = isinstance(module.utc_now(),datetime)
                monkeypatch.setattr(module, 'utc_now',
                    (lambda: Clock.now(timezone.utc)) if returns_datetime else (lambda: Clock.now(timezone.utc).isoformat()))
    return Clock


def due_answer(case, monkeypatch, *, original=True):
    from home_study_agent.database import Database
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.writing_response_origin import WritingOriginalService
    _, far, _ = complete_transfer_chain(case)
    clock = advance_clock(monkeypatch,datetime.fromisoformat(far['attempt']['created_at'])+timedelta(days=8))
    db = Database(case[0].path)
    prompts = DiagnosticPromptService(db)
    with db.connect() as c:
        review = dict(c.execute("SELECT * FROM review_task WHERE interval_label='writing_stage_7d'").fetchone())
    p = prompts.prepare(case[1],track='pet',preferred_item_id=review['diagnostic_item_id'],
        review_task_id=review['review_task_id'],client_action_id='retention-prompt')['prompt']
    text = 'Dear Ben, our science club meets on Tuesdays in the library. I want to build a tall bridge with my friends because we enjoy designing useful things. Please join us next Tuesday and bring your ideas. Best wishes, Mia.'
    artifact = WritingOriginalService(db).capture_native_response(case[1],prompt_id=p['prompt_id'],item_version=p['item_version'],
        text=text,client_action_id='retention-original') if original else None
    if original=='previous':
        with db.connect() as c:
            artifact = dict(c.execute("SELECT * FROM capture_artifact WHERE json_extract(details_json,'$.attempt_binding.attempt_id')=?",
                (far['attempt']['attempt_id'],)).fetchone())
    scored = prompts.answer(case[1],prompt_id=p['prompt_id'],item_version=p['item_version'],answer=text,
        client_action_id='retention-answer',**({'writing_original_artifact_id':artifact['artifact_id']} if artifact else {}))
    return db,review,p,text,scored,clock


def review_due(db, sid, p, text, scored, *, score=3):
    from test_pet_media_evidence import local_review
    from home_study_agent.writing_rubric import prepare_writing_review
    with db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed' WHERE child_id=? AND status='active'", (CHILD,))
        c.commit()
    assessment = local_review((db,None,sid,p,None,None),p,text,approve=False,scored=scored)
    with db.connect() as c:
        binding = prepare_writing_review(c,CHILD,assessment['assessment_id'])
    body = dict(expected_revision=0,client_action_id='retention-rubric',reviewer_id='parent',reviewer_role='parent',
        operation='approve',reason='Isolated original review',independent=True,hint_level=0,review_profile='pet_writing_text_v1',
        writing_evidence={'binding_version':binding['binding_version'],
            'task_point_evidence':[{'task_point':v,'response_quote':text} for v in binding['task_points']]},
        dimensions=[dict(dimension_id=d['dimension_id'],score=score,response_quotes=[text],rationale='Actual original evidence') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=v,response_quote=text) for v in assessment['criteria']])
    return assessment,body


@pytest.mark.parametrize('score', [0,3])
def test_due_new_original_and_actual_rubric_complete_performance_not_reference(writing_chain,monkeypatch,score):
    from home_study_agent.writing_stage_reviews import validate_writing_review_result
    from home_study_agent.database import Database
    db,task,p,text,scored,clock = due_answer(writing_chain,monkeypatch)
    assert scored['attempt']['score_status']=='needs_review'
    kwargs = dict(child_id=CHILD,session_id=writing_chain[1],review_task_id=task['review_task_id'],
        prompt_id=p['prompt_id'],evidence_id=scored['attempt']['evidence_id'])
    with db.connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(task['review_task_id'],)).fetchone()[0]=='pending'
        with pytest.raises(ValueError):
            validate_writing_review_result(c,**kwargs)
        before = dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?',(kwargs['evidence_id'],)).fetchone())
    assessment,body = review_due(db,writing_chain[1],p,text,scored,score=score)
    service = RubricEvidenceService(db)
    service.review(CHILD,assessment['assessment_id'],**body)
    service.review(CHILD,assessment['assessment_id'],**body)
    with Database(db.path).connect() as c:
        after = dict(c.execute('SELECT * FROM learning_evidence WHERE evidence_id=?',(kwargs['evidence_id'],)).fetchone())
        assert before==after and after['independent']==0
        result = validate_writing_review_result(c,**kwargs)
        assert result['performed'] and result['passed']==bool(score) and not result['mastery_claim_allowed']
        stage = writing_node(c)
        assert (stage['qualified_stage']=='delayed_retention')==bool(score), stage
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==4
    service.review(CHILD,assessment['assessment_id'],expected_revision=1,client_action_id='withdraw-retention',
        reviewer_id='parent',reviewer_role='parent',operation='retract',reason='Withdraw independent judgment')
    with Database(db.path).connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(task['review_task_id'],)).fetchone()[0]=='completed'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==4
        with pytest.raises(ValueError):
            validate_writing_review_result(c,**kwargs)
        assert writing_node(c)['qualified_stage']!='delayed_retention'


def test_due_answer_without_new_original_cannot_score(writing_chain,monkeypatch):
    with pytest.raises(ValueError,match='ORIGINAL'):
        due_answer(writing_chain,monkeypatch,original=False)
    with writing_chain[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==3
        assert c.execute("SELECT status FROM review_task WHERE interval_label='writing_stage_7d'").fetchone()[0]=='pending'


def test_due_answer_cannot_reuse_far_original(writing_chain,monkeypatch):
    with pytest.raises(ValueError,match='ORIGINAL'):
        due_answer(writing_chain,monkeypatch,original='previous')
    with writing_chain[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==3


def test_original_answer_restart_exact_retry_does_not_create_second_review_attempt(writing_chain,monkeypatch):
    from home_study_agent.database import Database
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.learning_state import LearningStateService
    db,task,p,text,scored,clock = due_answer(writing_chain,monkeypatch)
    reopened = Database(db.path)
    with reopened.connect() as c:
        artifact = c.execute("SELECT artifact_id FROM capture_artifact WHERE json_extract(details_json,'$.prompt_id')=?",
            (p['prompt_id'],)).fetchone()[0]
    same = DiagnosticPromptService(reopened).answer(writing_chain[1],prompt_id=p['prompt_id'],item_version=p['item_version'],
        answer=text,client_action_id='retention-answer',writing_original_artifact_id=artifact)
    assert same['attempt']['attempt_id']==scored['attempt']['attempt_id']
    with reopened.connect() as c:
        # Reach the legacy API's actual review guard, not its unrelated phase guard.
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (writing_chain[1],))
        c.commit()
    with pytest.raises(ValueError):
        LearningStateService(reopened).submit_diagnostic(writing_chain[1],task['diagnostic_item_id'],text,'new-untrusted-key',
            review_task_id=task['review_task_id'])
    with reopened.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?',(task['review_task_id'],)).fetchone()[0]==1


@pytest.mark.parametrize('fault', ['foreign_child','foreign_session','wrong_prompt','wrong_result',
    'source_withdrawal','result_withdrawal','original_withdrawal','source_change','rubric_reference','hint'])
def test_completed_read_gate_rechecks_exact_sources_without_writing(writing_chain,monkeypatch,fault):
    from home_study_agent.writing_stage_reviews import validate_writing_review_result
    from home_study_agent.privacy_source_withdrawal import ensure_schema,record
    db,task,p,text,scored,clock = due_answer(writing_chain,monkeypatch)
    assessment,body = review_due(db,writing_chain[1],p,text,scored)
    RubricEvidenceService(db).review(CHILD,assessment['assessment_id'],**body)
    if fault=='rubric_reference':
        correction = {k:v for k,v in body.items() if k not in {'review_profile','writing_evidence'}}
        correction.update(expected_revision=1,client_action_id='generic-reference-correction')
        RubricEvidenceService(db).review(CHILD,assessment['assessment_id'],**correction)
    kwargs = dict(child_id=CHILD,session_id=writing_chain[1],review_task_id=task['review_task_id'],
        prompt_id=p['prompt_id'],evidence_id=scored['attempt']['evidence_id'])
    with db.connect() as c:
        if fault=='foreign_child':kwargs['child_id']='child-junior-3'
        elif fault=='foreign_session':kwargs['session_id']='another-session'
        elif fault=='wrong_prompt':kwargs['prompt_id']='another-prompt'
        elif fault=='wrong_result':kwargs['evidence_id']=task['source_evidence_id']
        elif fault in {'source_withdrawal','result_withdrawal'}:
            ensure_schema(c)
            c.execute('BEGIN IMMEDIATE')
            record(c,CHILD,{'isolated-deleted-source'},
                {task['source_evidence_id'] if fault=='source_withdrawal' else kwargs['evidence_id']})
        elif fault=='original_withdrawal':
            c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE json_extract(details_json,'$.prompt_id')=?", (p['prompt_id'],))
        elif fault=='source_change':
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (task['diagnostic_item_id'],))
        elif fault=='hint':
            c.execute('UPDATE learning_evidence SET hint_level=1 WHERE evidence_id=?', (kwargs['evidence_id'],))
        c.commit()
        before = list(c.iterdump())
        with pytest.raises(ValueError):
            validate_writing_review_result(c,**kwargs)
        assert before==list(c.iterdump())
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(task['review_task_id'],)).fetchone()[0]=='completed'


def test_retention_completion_and_rubric_roll_back_on_reconcile_failure(writing_chain,monkeypatch):
    import home_study_agent.writing_stage_reviews as module
    db,task,p,text,scored,clock = due_answer(writing_chain,monkeypatch)
    assessment,body = review_due(db,writing_chain[1],p,text,scored)
    def fail(c, child_id, **kwargs):
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',(task['review_task_id'],)).fetchone()[0]=='completed'
        raise RuntimeError('isolated completion failure')
    monkeypatch.setattr(module,'reconcile_writing_stage_reviews',fail)
    with pytest.raises(RuntimeError,match='completion failure'):
        RubricEvidenceService(db).review(CHILD,assessment['assessment_id'],**body)
    with db.connect() as c:
        assert c.execute('SELECT status,result_evidence_id FROM review_task WHERE review_task_id=?',(task['review_task_id'],)).fetchone()[:]==('pending',None)
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',(assessment['assessment_id'],)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==4


def test_actual_far_rubric_transaction_creates_one_review_and_exact_retry(writing_chain):
    case = writing_chain
    assessment, scored, body = complete_transfer_chain(case)
    with case[0].connect() as c:
        rows = c.execute("SELECT * FROM review_task WHERE source_evidence_id=?", (scored['attempt']['evidence_id'],)).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row['interval_label'] == 'writing_stage_7d' and row['status'] == 'pending'
        assert row['result_evidence_id'] is None
    RubricEvidenceService(case[0]).review(CHILD,assessment['assessment_id'],**body)
    with case[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 3


def test_actual_far_retraction_cancels_pending_in_same_review_transaction(writing_chain):
    case = writing_chain
    assessment, scored, _ = complete_transfer_chain(case)
    RubricEvidenceService(case[0]).review(CHILD,assessment['assessment_id'],expected_revision=1,
        client_action_id='retract-actual-far',reviewer_id='parent',reviewer_role='parent',operation='retract',reason='Withdraw current judgment')
    with case[0].connect() as c:
        row = c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (scored['attempt']['evidence_id'],)).fetchone()
        assert row and row['status'] == 'cancelled'
        assert writing_node(c)['qualified_stage'] != 'far_transfer'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 3


def test_review_event_and_task_creation_roll_back_together_on_reconcile_failure(writing_chain, monkeypatch):
    import home_study_agent.writing_stage_reviews as reviews
    from test_writing_stage_reviews import reviewed_answer
    case = writing_chain
    db,sid,prompts,originals,_,near,far = case
    original_reconcile = reviews.reconcile_writing_stage_reviews

    def fail(c, child_id, **kwargs):
        result = original_reconcile(c, child_id, **kwargs)
        if result['created']:
            raise RuntimeError('isolated scheduling write failure')
        return result

    monkeypatch.setattr(reviews, 'reconcile_writing_stage_reviews', fail)
    with pytest.raises(RuntimeError, match='scheduling write failure'):
        complete_transfer_chain(case)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 3
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 0
