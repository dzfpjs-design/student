"""Authored questions and synthetic audio; real voice binding/review/plan HTTP."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.repository import StudyRepository
from home_study_agent.school_open_reviews import _anchor, reconcile_school_reviews
from test_open_response_supply_http import CHILD
from test_pet_media_evidence import local_review
from test_school_open_reviews import school_case, next_day
from test_voice_runtime_binding import capture, interpret


def spoken_review(case, monkeypatch, *, prompt=None, key='spoken', score=0, approve=True):
    db, client, headers, sid, baseline = case[:5]
    settings = case[-1]
    assert client.put('/v1/parent/privacy', headers=headers, json={
        'raw_media_storage_allowed':True, 'cloud_audio_allowed':False}).status_code == 200
    if prompt is None:
        prompt = DiagnosticPromptService(db).prepare(sid, track='school',
            preferred_item_id=baseline['diagnostic_item_id'], client_action_id=key+'-prepare')['prompt']
    text = '我先检查已知条件，再选择合适的关系推理，最后核对结论适用的范围。'
    quote = '我的答案是' + text
    voice = capture(client, settings, monkeypatch, sid, quote, prompt, key+'-audio', child=CHILD)
    turn = interpret(client, sid, quote, key+'-turn', voice)
    assert turn['action']['type'] == 'submit_diagnostic_answer', turn
    text = quote
    response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
        'prompt_id':prompt['prompt_id'], 'item_version':prompt['item_version'],
        'answer':text, 'authorized_turn_id':turn['agent_turn_id'], 'client_action_id':key+'-answer'})
    assert response.status_code == 200, response.text
    scored = response.json()
    assert scored['attempt']['correctness'] is None
    with db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed',completed_at=? WHERE child_id=? AND status='active'",
            (datetime.now(timezone.utc).isoformat(), CHILD))
        c.commit()
    assessment = local_review((db,None,sid,prompt,None,None), prompt, text, approve=False, scored=scored)
    body = dict(operation='approve', expected_revision=0, client_action_id=key+'-review',
        reason='Synthetic reasoning transcript reviewed against the published criteria; no acoustic assessment.',
        independent=True, hint_level=0,
        dimensions=[dict(dimension_id=d['dimension_id'], score=score, response_quotes=[text],
            rationale='Check reasoning against the stated criterion') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=p,response_quote=text) for p in assessment['criteria']])
    path = f'/v1/parent/children/{CHILD}/learning-rubrics/{assessment["assessment_id"]}/review'
    if approve:
        reviewed = client.post(path, headers=headers, json=body)
        assert reviewed.status_code == 200, reviewed.text
    return scored, assessment, path, body, voice


@pytest.mark.parametrize('school_case', ['spoken','spoken_physics'], indirect=True)
def test_spoken_review_enters_next_day_plan_and_survives_restart(school_case, monkeypatch):
    db, client, headers, sid, baseline, near = school_case[:6]
    scored, assessment, path, body, _ = spoken_review(school_case, monkeypatch, approve=False)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert not reconcile_school_reviews(c, CHILD)['created']
        c.commit()
    reviewed = client.post(path, headers=headers, json=body)
    assert reviewed.status_code == 200, reviewed.text
    with db.connect() as c:
        anchor = _anchor(c, CHILD, scored['attempt']['evidence_id'], datetime.now(timezone.utc))
        assert anchor['receipt']['correctness'] == 0
        task = c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',
            (scored['attempt']['evidence_id'],)).fetchone()
        assert task and task['diagnostic_item_id'] == near['diagnostic_item_id']
        assert task['score_authority_id'] == anchor['score_decision']['authority_id']
    client = client.school_reopen()
    resumed = (db, client, headers, *school_case[3:])
    case, slot, prompt, _, _ = next_day(resumed, monkeypatch, key='oral-next-day')
    assert prompt['item']['response_mode'] == 'spoken_explanation'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 1
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',
            (scored['attempt']['attempt_id'],)).fetchone()[0] is None
    # Revocation cancels the uncompleted obligation, never the original answer.
    withdrawn = case[1].post(path, headers=case[2], json=dict(operation='retract',
        expected_revision=1, client_action_id='oral-retract', reason='Recheck this reasoning review'))
    assert withdrawn.status_code == 200, withdrawn.text
    with db.connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',
            (slot['source_ref'],)).fetchone()[0] == 'cancelled'
    from home_study_agent.repository import PlanInputError
    with pytest.raises(PlanInputError):
        StudyRepository(db).complete_plan_item(slot['plan_item_id'], 1, 'stale-review-complete')


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
@pytest.mark.parametrize('score', [0,3])
def test_reviewed_oral_transfer_changes_following_plan_not_original_score(school_case, monkeypatch, score):
    spoken_review(school_case, monkeypatch)
    case, slot, prompt, _, _ = next_day(school_case, monkeypatch, key='oral-transfer')
    db, client, headers, sid = case[:4]
    scored, assessment, path, body, _ = spoken_review(case, monkeypatch,
        prompt=prompt, key='oral-near-answer', score=score, approve=False)
    from home_study_agent.repository import PlanInputError
    with pytest.raises(PlanInputError):
        StudyRepository(db).complete_plan_item(slot['plan_item_id'], 1, 'unreviewed-oral-finish')
    reviewed = client.post(path, headers=headers, json=body)
    assert reviewed.status_code == 200, reviewed.text
    finished = StudyRepository(db).complete_plan_item(slot['plan_item_id'], 1, 'reviewed-oral-finish')
    assert finished['status'] == 'completed'
    with db.connect() as c:
        task = c.execute('SELECT * FROM review_task WHERE review_task_id=?', (slot['source_ref'],)).fetchone()
        assert task['status'] == 'completed' and task['result_evidence_id'] == scored['attempt']['evidence_id']
        far = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='school_open_far'",
            (scored['attempt']['evidence_id'],)).fetchone()
        assert bool(far) == (score == 3)
        if far:
            assert far['diagnostic_item_id'] == case[6]['diagnostic_item_id']
        assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE attempt_id=?',
            (scored['attempt']['attempt_id'],)).fetchone()[0] is None
    if score == 3:
        case, far_slot, far_prompt, _, _ = next_day(case, monkeypatch, key='oral-far-day')
        assert far_prompt['diagnostic_item_id'] == case[6]['diagnostic_item_id']


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
@pytest.mark.parametrize('fault', ['capture_missing','transcript_changed','future_review','corrupt_assessment','legacy_unbound_review'])
def test_changed_oral_authority_cancels_obligation_without_erasing_answer(school_case, monkeypatch, fault):
    db = school_case[0]
    scored, assessment, _, _, voice = spoken_review(school_case, monkeypatch)
    with db.connect() as c:
        task = dict(c.execute('SELECT * FROM review_task WHERE source_evidence_id=?',
            (scored['attempt']['evidence_id'],)).fetchone())
        original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',
            (scored['attempt']['attempt_id'],)).fetchone())
        if fault == 'capture_missing':
            c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?', (voice,))
        elif fault == 'transcript_changed':
            c.execute("UPDATE agent_turn SET transcript='different recording' WHERE agent_turn_id=(SELECT authorized_turn_id FROM diagnostic_prompt WHERE attempt_id=?)",
                (scored['attempt']['attempt_id'],))
        elif fault == 'future_review':
            with pytest.raises(sqlite3.IntegrityError, match='RUBRIC_HISTORY_IMMUTABLE'):
                c.execute("UPDATE rubric_review_event SET created_at='2099-01-01T00:00:00+00:00' WHERE assessment_id=?",
                    (assessment['assessment_id'],))
            # Deliberately corrupt only the disposable DB to exercise recovery.
            c.execute('DROP TRIGGER rubric_review_event_no_update')
            c.execute("UPDATE rubric_review_event SET created_at='2099-01-01T00:00:00+00:00' WHERE assessment_id=?",
                (assessment['assessment_id'],))
        elif fault == 'corrupt_assessment':
            with pytest.raises(sqlite3.IntegrityError, match='RUBRIC_HISTORY_IMMUTABLE'):
                c.execute("UPDATE rubric_assessment SET payload_json='{}' WHERE assessment_id=?", (assessment['assessment_id'],))
            c.execute('DROP TRIGGER rubric_assessment_no_update')
            c.execute("UPDATE rubric_assessment SET payload_json='{}' WHERE assessment_id=?", (assessment['assessment_id'],))
        else:
            payload = json.loads(c.execute('SELECT payload_json FROM rubric_review_event WHERE assessment_id=?',
                (assessment['assessment_id'],)).fetchone()[0])
            assert payload.pop('school_spoken_original_version')
            c.execute('DROP TRIGGER rubric_review_event_no_update')
            c.execute('UPDATE rubric_review_event SET payload_json=? WHERE assessment_id=?',
                (json.dumps(payload),assessment['assessment_id']))
        result = reconcile_school_reviews(c, CHILD)
        assert result['cancelled'] == [task['review_task_id']]
        assert not result['created']
        assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',
            (scored['attempt']['attempt_id'],)).fetchone()) == original
        assert not reconcile_school_reviews(c, 'child-primary-4')['created']
        c.commit()


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_assisted_oral_review_does_not_complete_independent_obligation(school_case, monkeypatch):
    spoken_review(school_case, monkeypatch)
    case, slot, prompt, _, _ = next_day(school_case, monkeypatch, key='assisted-oral')
    db, client, headers = case[:3]
    _, _, path, body, _ = spoken_review(case, monkeypatch, prompt=prompt, key='assisted-answer', score=3, approve=False)
    body.update(independent=False, hint_level=1)
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    from home_study_agent.repository import PlanInputError
    with pytest.raises(PlanInputError):
        StudyRepository(db).complete_plan_item(slot['plan_item_id'], 1, 'assisted-finish')
    with db.connect() as c:
        assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (slot['source_ref'],)).fetchone()[0] == 'pending'
        assert c.execute("SELECT COUNT(*) FROM review_task WHERE interval_label='school_open_far'").fetchone()[0] == 0


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_parent_cannot_approve_a_missing_original_voice(school_case, monkeypatch):
    db, client, headers = school_case[:3]
    _, assessment, path, body, voice = spoken_review(school_case, monkeypatch, approve=False)
    with db.connect() as c:
        c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?', (voice,))
        c.commit()
    result = client.post(path, headers=headers, json=body)
    assert result.status_code in (400,409,422), result.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?', (assessment['assessment_id'],)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM review_task').fetchone()[0] == 0
