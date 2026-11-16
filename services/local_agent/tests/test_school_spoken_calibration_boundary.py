"""A typed-text certificate cannot stand in for captured oral explanations."""
import pytest

from home_study_agent.rubric_calibration import RubricCalibrationService, SCORER_FIELDS
from home_study_agent.rubric_calibration_evidence import source_snapshot
from test_school_open_reviews import school_case, CHILD
from test_school_spoken_reviews import spoken_review


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_calibration_source_keeps_original_spoken_modality_and_voice_proof(school_case, monkeypatch):
    db = school_case[0]
    saved, _, _, _, voice = spoken_review(school_case, monkeypatch, approve=False)
    with db.connect() as c:
        source = source_snapshot(c, CHILD, saved['attempt']['evidence_id'])
        assert source['modality'] == 'original_spoken_text'
        c.execute('DELETE FROM voice_capture_binding WHERE voice_input_id=?', (voice,))
        with pytest.raises(ValueError):
            source_snapshot(c, CHILD, saved['attempt']['evidence_id'])
        c.rollback()
    # Supporting an oral protocol must not invent a deployed certificate.
    scorer = {field:'synthetic' for field in SCORER_FIELDS}
    scorer.update({field:'0'*64 for field in ('prompt_hash','rubric_hash','parameters_hash')})
    scorer['modality'] = source['modality']
    service = RubricCalibrationService(db)
    scope = dict(subject=source['subject'], grade=source['grade'], task_family='synthetic', strata=['ordinary'])
    frozen = service.freeze(CHILD, scorer=scorer, scope=scope,
        holdout_receipt_id='missing-holdout', client_action_id='oral-calibration-not-validated')
    assert not frozen['current']['score_qualified']
    assert frozen['current']['status'] == 'insufficient'
    reopened = RubricCalibrationService(db)
    with db.connect() as c:
        assert not reopened.qualified_score_receipt(c, CHILD, saved.get('assessment_id','missing'),
            protocol_id=frozen['current']['protocol_id'], as_of=service._now())['score_qualified']


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
def test_signed_oral_holdout_cannot_be_frozen_as_typed_text(school_case, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.rubric_calibration_evidence import digest
    from test_calibration_evaluator_host import signer
    from test_voice_runtime_binding import capture, interpret
    db, client, headers, sid, baseline = school_case[:5]
    monkeypatch.setattr('test_voice_runtime_binding.CHILD', CHILD)
    assert client.put('/v1/parent/privacy',headers=headers,json={
        'raw_media_storage_allowed':True,'cloud_audio_allowed':False}).status_code == 200
    p = DiagnosticPromptService(db).prepare(sid,track='school',
        preferred_item_id=baseline['diagnostic_item_id'],client_action_id='holdout-oral')['prompt']
    text = '我的答案是先核对已知条件，再说明选择关系的理由。'
    voice = capture(client,school_case[-1],monkeypatch,sid,text,p,'holdout-voice',child=CHILD)
    turn = interpret(client,sid,text,'holdout-turn',voice)
    response = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer',json=dict(
        prompt_id=p['prompt_id'],item_version=p['item_version'],answer=text,
        authorized_turn_id=turn['agent_turn_id'],client_action_id='holdout-answer'))
    assert response.status_code == 200, response.text
    eid = response.json()['attempt']['evidence_id']
    with db.connect() as c:
        source = source_snapshot(c,CHILD,eid)
    sign, now = signer(tmp_path,monkeypatch,children=[CHILD])
    scope = dict(subject=source['subject'],grade=source['grade'],task_family='synthetic',strata=['ordinary'])
    holdout = dict(child_id=CHILD,receipt_id='oral-holdout',status='current',corpus_kind='observed_original',
        blind_protocol_version='blind_labels_v1',provenance_receipt_hash='b'*64,scope_hash=digest(scope),
        frozen_at=now.isoformat(),cases=[dict(evidence_id=eid,group_hash='c'*64,stratum='ordinary',source_ids=[])])
    root = f'/v1/parent/children/{CHILD}/rubric-calibration'
    r = client.post(root+'/authority/import',headers=headers,json=sign('holdout',holdout))
    assert r.status_code == 200, r.text
    scorer = {field:'synthetic' for field in SCORER_FIELDS}
    scorer.update({field:'0'*64 for field in ('prompt_hash','rubric_hash','parameters_hash')})
    scorer.update(model_revision=None,modality='typed_text')
    service = RubricCalibrationService(db)
    with pytest.raises(ValueError,match='CASE_SCOPE_MISMATCH'):
        service.freeze(CHILD,scorer=scorer,scope=scope,holdout_receipt_id='oral-holdout',client_action_id='wrong-mode')
    scorer['modality'] = 'original_spoken_text'
    result = service.freeze(CHILD,scorer=scorer,scope=scope,holdout_receipt_id='oral-holdout',client_action_id='correct-mode')
    assert not result['current']['score_qualified']
    with db.connect() as c:
        import json
        manifest = json.loads(c.execute('SELECT manifest_json FROM rubric_calibration_protocol WHERE protocol_id=?',
            (result['current']['protocol_id'],)).fetchone()[0])
        assert manifest['cases'][0]['source_version'] == source['version']
