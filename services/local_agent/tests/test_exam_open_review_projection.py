"""Real exam capture/review contracts on disposable authored synthetic sources."""
import json
from datetime import datetime, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.exam_simulation import ExamSimulationService
from home_study_agent.school_open_reviews import _anchor
from test_school_open_reviews import school_case
from test_open_response_supply_http import CHILD
from test_voice_runtime_binding import capture, interpret


def exam(case, monkeypatch, *, mixed=False):
    db, client, headers, sid, baseline, near = case[:6]
    service = ExamSimulationService(db)
    def select(domains, count):
        available = [(domain, item) for domain, items in domains.items() for item in items]
        chosen = [next(pair for pair in available if pair[1]['diagnostic_item_id'] == baseline['diagnostic_item_id'])]
        if mixed:
            with db.connect() as c:
                closed = {r[0] for r in c.execute("SELECT diagnostic_item_id FROM diagnostic_item WHERE json_extract(expected_answer_json,'$.local_rule')='local_exact_v1' AND source_ref LIKE 'parent_approved_%'")}
            chosen.append(next(pair for pair in available if pair[1]['diagnostic_item_id'] in closed))
        else:
            chosen.append(next(pair for pair in available if pair[1]['diagnostic_item_id'] == near['diagnostic_item_id']))
        return chosen
    monkeypatch.setattr(service, '_balanced_items', select)
    assert client.put('/v1/parent/privacy', headers=headers, json={
        'raw_media_storage_allowed': True, 'cloud_audio_allowed': False}).status_code == 200
    run = service.start(sid, 'junior_exam', '数学', 'practice', 2, 30, 'exam-open-start')
    return service, run


def answer(case, service, monkeypatch, key, *, text=None):
    db, client, _, sid = case[:4]
    binding = service.arrival_binding(sid)
    text = text or '我的答案是先核对条件，再根据判别式分析实数根，并说明结论适用的范围。' + key
    voice = capture(client, case[-1], monkeypatch, sid, text,
        {'kind': 'exam', 'binding': binding}, key+'-capture', child=CHILD)
    turn = interpret(client, sid, text, key+'-turn', voice)
    assert turn['action']['type'] == 'submit_exam_answer', turn
    result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    with db.connect() as c:
        attempt = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE diagnostic_item_id=? ORDER BY rowid DESC',
            (binding['item_id'],)).fetchone())
    return binding, attempt, turn, voice


def review(client, headers, snapshot, *, score=3, revision=0, operation='approve', key='exam-review'):
    body = dict(operation=operation, expected_revision=revision, client_action_id=key,
        reason='Synthetic original exam answer checked; this is not an official exam or mastery claim.')
    if operation == 'approve':
        body.update(independent=True, hint_level=0,
            dimensions=[dict(dimension_id=d['dimension_id'], score=score,
                response_quotes=[snapshot['response']], rationale='Checked the preserved answer against the criterion')
                for d in snapshot['dimensions']],
            criterion_evidence=[dict(criterion=v, response_quote=snapshot['response']) for v in snapshot['criteria']])
    return client.post(f"/v1/parent/children/{CHILD}/learning-rubrics/{snapshot['assessment_id']}/review",
        headers=headers, json=body)


def reopen_service(db):
    reopened = Database(db.path)
    reopened.local_media_runtime = db.local_media_runtime
    return ExamSimulationService(reopened)


def assessments(case):
    response = case[1].get(f'/v1/parent/children/{CHILD}/learning-rubrics', headers=case[2])
    assert response.status_code == 200, response.text
    return {s['source_evidence_id']:s for s in response.json()['items']}


def test_legacy_answered_exam_owner_migration_preserves_real_original_and_turn(school_case,monkeypatch):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService,DiagnosticPromptError
    service,run=exam(school_case,monkeypatch)
    binding,attempt,turn,voice=answer(school_case,service,monkeypatch,'legacy-owner-original')
    db,client,headers,sid=school_case[:4]
    with db.connect() as c:
        slot=c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=1',(run['run_id'],)).fetchone()
        prompt_id=service._review_prompt_id(run,slot)
        c.execute("UPDATE diagnostic_prompt SET owner_kind='diagnostic' WHERE prompt_id=?",(prompt_id,))
        before=dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt_id,)).fetchone())
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?",(sid,))
        c.commit()
    prompts=DiagnosticPromptService(db)
    with db.connect() as c:
        after=dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt_id,)).fetchone())
        assert after['owner_kind']=='exam'
        assert {k:v for k,v in before.items() if k!='owner_kind'}=={k:v for k,v in after.items() if k!='owner_kind'}
        assert after['authorized_turn_id']==turn['agent_turn_id'] and after['attempt_id']==attempt['attempt_id']
    assert prompts.read(sid)['prompt'] is None
    with pytest.raises(DiagnosticPromptError,match='exam_prompt_requires_exam_submit'):
        prompts.read(sid,prompt_id=prompt_id)


@pytest.fixture
def mixed_source(monkeypatch):
    """Add an authored exact original through the same real source importer."""
    from home_study_agent.knowledge_base import KnowledgeBaseService
    original_import = KnowledgeBaseService.import_source
    def importing(self, child, payload):
        if payload['title'] == 'Synthetic school transfer questions':
            closed = dict(schema='curriculum_objective_source_v1',subject='数学',grade='初三',
                knowledge_node_id='node-j3-math-discriminant',assessment_mode='objective',answer_type='text',
                prompt='方程 x²+x+1=0 的判别式是多少？只写出判别式数值。',
                expected_answer=dict(local_rule='local_exact_v1',exact_answers=['-3']))
            payload = {**payload,'content_text':payload['content_text']+'\n'+json.dumps(closed,ensure_ascii=False)}
        return original_import(self,child,payload)
    monkeypatch.setattr(KnowledgeBaseService,'import_source',importing)


@pytest.fixture
def exam_mixed_case(mixed_source, school_case):
    db, supply = school_case[0], school_case[7]
    with db.connect() as c:
        scope = json.loads(c.execute('SELECT scope_json FROM curriculum_supply_selection ORDER BY version DESC LIMIT 1').fetchone()[0])
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence DESC LIMIT 1',
            (scope['textbook_source_id'],)).fetchone()[0]
        quote = c.execute("SELECT content_text FROM curriculum_standard_page WHERE source_id='open-standard'").fetchone()[0]
    spec = dict(seed_item_id='diag-j3-discriminant-1',stage='parallel',explanation='原题判别式为1-4=-3，只核对这个数值。',
        difficulty=1,excerpt_id=excerpt,requirement_id='open-requirement',standard_page=12,standard_quote=quote,
        validator='curriculum_objective_exact_source_v1')
    draft = supply.generate_draft(CHILD,spec,author='original-author',action_id='exam-closed-draft',expected_scope_version=2)
    cid = draft['candidate_id']
    supply.validate(CHILD,cid,actor='independent-validator',action_id='exam-closed-check',expected_version=1)
    supply.review(CHILD,cid,decision='approve',actor='parent',note='核对真实原题与精确数值',action_id='exam-closed-review',expected_version=2)
    supply.publish(CHILD,cid,actor='parent',action_id='exam-closed-publish',expected_version=3)
    return school_case


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_exam_original_is_parent_reviewable_and_current_score_changes_without_rewriting_attempt(school_case, monkeypatch):
    db, client, headers, sid = school_case[:4]
    service, run = exam(school_case, monkeypatch)
    originals = []
    for index in range(2):
        _, attempt, _, _ = answer(school_case, service, monkeypatch, f'open-{index}')
        originals.append(attempt)
    result = service.status(run['run_id'])['result']
    assert result['score_status'] == 'needs_review' and result['score'] is None
    listing = client.get(f'/v1/parent/children/{CHILD}/learning-rubrics', headers=headers)
    assert listing.status_code == 200, listing.text
    snapshots = {s['source_evidence_id']: s for s in listing.json()['items']}
    assert set(snapshots) == {a['evidence_id'] for a in originals}
    for index, attempt in enumerate(originals):
        snapshot = snapshots[attempt['evidence_id']]
        assert snapshot['source_kind'] == 'exam_open_review' and snapshot['source_status'] == 'current'
        assert snapshot['exam_slot']['run_id'] == run['run_id']
        reviewed = review(client, headers, snapshot, score=3 if index == 0 else 0, key=f'exam-review-{index}')
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()['learning_transition']['status'] == 'not_applied'
        with db.connect() as c:
            assert _anchor(c,CHILD,attempt['evidence_id'],datetime.now(timezone.utc))['score_decision']['passed'] == (index == 0)
    client = client.school_reopen()
    headers = {'X-HSC-Parent': client.post('/v1/parent/unlock', json={'pin':'8264'}).json()['parent_token']}
    restored = client.get(f'/v1/parent/children/{CHILD}/learning-rubrics', headers=headers)
    assert restored.status_code == 200 and restored.json()['total'] == 2
    current = reopen_service(db).status(run['run_id'])['result']
    assert current['verified_items'] == 2 and current['score'] == 1 and current['percentage'] == 50
    report = service.specialized.roadmap(CHILD, 'junior_exam')
    assert any(a['current_evidence_status'] == 'current_reference'
        for a in report['assessment_history'])
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')] == originals
        assert c.execute('SELECT COUNT(*) FROM model_call WHERE purpose=\'learning_outcome_evaluation\'').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0] == 0
        index = c.execute('SELECT * FROM specialized_assessment_event WHERE source_ref=?',(run['run_id'],)).fetchone()
        assert index['score'] is None and index['max_score'] is None and index['domain_results_json']=='{}'
    revoked = review(client, headers, snapshots[originals[0]['evidence_id']], revision=1,
        operation='retract', key='exam-review-retract')
    assert revoked.status_code == 200, revoked.text
    withdrawn = reopen_service(db).status(run['run_id'])['result']
    assert withdrawn['verified_items'] == 1 and withdrawn['score'] is None
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM diagnostic_attempt ORDER BY rowid')] == originals


@pytest.mark.parametrize('school_case',['spoken'],indirect=True)
def test_real_mixed_paper_review_replay_correction_and_parent_remediation(exam_mixed_case, monkeypatch):
    case = exam_mixed_case
    db,client,headers = case[:3]
    service,run = exam(case,monkeypatch,mixed=True)
    _,opened,turn,_ = answer(case,service,monkeypatch,'mixed-open')
    assert client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute").status_code == 200
    answer(case,service,monkeypatch,'mixed-closed',text='我的答案是-3')
    candidate = assessments(case)[opened['evidence_id']]
    first = review(client,headers,candidate,score=0,key='mixed-review')
    assert first.status_code == 200,first.text
    replay = review(client,headers,candidate,score=0,key='mixed-review')
    assert replay.status_code == 200 and replay.json()['event']==first.json()['event']
    current = service.status(run['run_id'])['result']
    assert current['verified_items']==2 and current['score']==1 and current['percentage']==50
    response = client.get(f'/v1/parent/children/{CHILD}/specialized-roadmaps',headers=headers)
    assert response.status_code == 200,response.text
    roadmap = next(r for r in response.json()['items'] if r['track']=='junior_exam')
    domain = next(r for r in roadmap['domains'] if r['domain_id']=='junior_exam.数学.方程与不等式')
    assert domain['assessment_reference_score']==50 and not domain['mastery_claim_allowed']
    assert domain['exam_reference_score']==50 and domain['exam_reference_score_scope']=='current_exam_reference_only'
    assert domain['domain_id'] in {r['domain_id'] for r in roadmap['whole_exam_analysis']['exam_reference_weak_domains']}
    assert roadmap['whole_exam_analysis']['latest_exam']['score_scope']=='current_exam_reference_only'
    assert roadmap['whole_exam_analysis']['latest_exam']['percentage']==50
    assert any(a['domain_id']==domain['domain_id'] and a['action_type']=='remediation' for a in roadmap['next_actions'])
    changed = review(client,headers,candidate,score=3,revision=1,key='mixed-corrected-review')
    assert changed.status_code==200,changed.text
    assert reopen_service(db).status(run['run_id'])['result']['percentage']==100
    service._finalize(run['run_id'],'completed')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM specialized_assessment_event WHERE source_ref=?',(run['run_id'],)).fetchone()[0]==1
        assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(opened['attempt_id'],)).fetchone())==opened
        assert c.execute('SELECT COUNT(*) FROM model_call WHERE purpose=\'learning_outcome_evaluation\'').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM agent_outcome').fetchone()[0] == 0


@pytest.mark.parametrize('school_case',['spoken'],indirect=True)
@pytest.mark.parametrize('fault',['capture_hash','capture_missing','turn_owner','turn_cancelled',
    'slot_hash','slot_session','slot_prompt','criterion_version','answer_withdrawn'])
def test_current_exam_review_rejects_changed_original_authority(school_case,monkeypatch,fault):
    db,client,headers,sid = school_case[:4]
    service,run = exam(school_case,monkeypatch)
    binding,attempt,turn,voice = answer(school_case,service,monkeypatch,'original')
    answer(school_case,service,monkeypatch,'other')
    snapshot = assessments(school_case)[attempt['evidence_id']]
    accepted = review(client,headers,snapshot,key='before-fault')
    assert accepted.status_code==200,accepted.text
    assert service.status(run['run_id'])['result']['verified_items']==1
    if fault=='slot_session':
        from home_study_agent.repository import StudyRepository
        foreign_session = StudyRepository(db).switch_active_session_child(sid,'child-primary-4','foreign-owned-session')['session_id']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if fault=='capture_hash':
            c.execute('UPDATE voice_capture_binding SET wav_sha256=? WHERE voice_input_id=?',('0'*64,voice))
        elif fault=='capture_missing':
            c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?',(voice,))
        elif fault=='turn_owner':
            c.execute("UPDATE agent_turn SET child_id='child-primary-4' WHERE agent_turn_id=?",(turn['agent_turn_id'],))
        elif fault=='turn_cancelled':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(turn['agent_turn_id'],))
        elif fault=='slot_hash':
            c.execute('UPDATE specialized_exam_item SET answer_hash=? WHERE run_id=? AND position=?',
                ('0'*64,binding['run_id'],binding['position']))
        elif fault=='slot_session':
            c.execute('UPDATE specialized_exam_run SET session_id=? WHERE run_id=?',(foreign_session,run['run_id']))
        elif fault=='slot_prompt':
            forged = {**binding,'position':2}
            c.execute('UPDATE diagnostic_prompt SET request_json=? WHERE attempt_id=?',
                (json.dumps({'exam_slot':forged},sort_keys=True),attempt['attempt_id']))
        elif fault=='criterion_version':
            rubric = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                (binding['item_id'],)).fetchone()[0])
            rubric['task_points'].append('改版后新增要求')
            c.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?',
                (json.dumps(rubric,ensure_ascii=False),binding['item_id']))
        else:
            from home_study_agent.learning_evidence_withdrawal import ensure_schema,record_withdrawal
            ensure_schema(c)
            record_withdrawal(c,child_id=CHILD,session_id=sid,evidence_id=attempt['evidence_id'],
                client_action_id='actual-answer-withdrawal',request={'reason':'original revoked'})
        c.commit()
    current = reopen_service(db).status(run['run_id'])['result']
    assert current['verified_items']==0 and current['score'] is None
    rejected = review(client,headers,snapshot,revision=1,key='after-fault')
    assert rejected.status_code==409,rejected.text
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone())==attempt
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',(snapshot['assessment_id'],)).fetchone()[0]==1


@pytest.mark.parametrize('school_case',['written'],indirect=True)
def test_written_exam_asr_cannot_be_relabeled_as_an_original_editor(school_case,monkeypatch):
    db,client,headers = school_case[:3]
    service,run = exam(school_case,monkeypatch)
    _,attempt,_,_ = answer(school_case,service,monkeypatch,'written-asr')
    answer(school_case,service,monkeypatch,'other-written-asr')
    snapshot = assessments(school_case)[attempt['evidence_id']]
    detail = client.get(f"/v1/parent/children/{CHILD}/learning-rubrics/{snapshot['assessment_id']}",headers=headers)
    assert detail.status_code==200,detail.text
    assert detail.json()['writing_review']['required'] and detail.json()['writing_review']['blockers']
    rejected = review(client,headers,snapshot,key='forged-writing-review')
    assert rejected.status_code==409,rejected.text
    assert service.status(run['run_id'])['result']['verified_items']==0
    foreign = client.get(f"/v1/parent/children/child-primary-4/learning-rubrics/{snapshot['assessment_id']}",headers=headers)
    assert foreign.status_code==409 and foreign.json()['detail']['code']=='RUBRIC_ASSESSMENT_NOT_OWNED'
