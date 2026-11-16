"""Replay disclosed synthetic signed corpus with final code, without relabeling.

Requires an explicit checkpoint from test_school_calibrated_authority_http.
Real executor/authority journal; synthetic transport, not model quality evidence.
"""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.school_spoken_score import current_score
from home_study_agent.school_open_reviews import reconcile_school_reviews
from home_study_agent.rubric_calibration import RubricCalibrationService
from calibration_execution_measurement import ExecutionMeasurement


def checkpoint_runtime(tmp_path,monkeypatch):
    selected = os.environ.get('HSC_SYNTHETIC_ORAL_CHECKPOINT')
    if not selected:
        pytest.skip('Explicit synthetic signed oral checkpoint required')
    root = Path(selected).resolve()
    assert root.is_relative_to(Path('/private/tmp'))
    meta = json.loads((root/'context.json').read_text())
    assert meta['synthetic_only'] is True and meta['real_model_quality'] is False
    assert meta['child_id'] == 'child-junior-3'
    media_root = Path(meta['source_database']).resolve().parent
    assert any(media_root.is_relative_to(base.resolve()) for base in
        (Path('/private/tmp'), Path(tempfile.gettempdir())/'pytest-of-dzfpjs'))
    path = tmp_path/'replay.db'
    # This is a completed sqlite backup, not a live WAL database.
    assert not (root/'checkpoint.db-wal').exists()
    with sqlite3.connect(f'file:{root / "checkpoint.db"}?mode=ro&immutable=1',uri=True) as source, sqlite3.connect(path) as target:
        source.backup(target)
    monkeypatch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE',str(root/'public-trust.json'))
    settings = Settings(data_dir=media_root,database_path=path,maintenance_enabled=False,
        model_provider='qwen',model_name='isolated-model',model_api_key='synthetic-69c92b8a1f26c7ac5e4763bd',
        model_base_url='https://isolated.invalid/v1',data_encryption_key=b'8'*32)
    db = Database(path,media_root=media_root,data_encryption_key=settings.data_encryption_key)
    return meta, settings, db


@pytest.mark.parametrize('candidate_score', [3, 1.5])
def test_signed_oral_checkpoint_final_executor_restart_and_recording_loss(tmp_path,monkeypatch,candidate_score,fault=None):
    meta, settings, db = checkpoint_runtime(tmp_path,monkeypatch)
    media_root = settings.data_dir
    posts = []
    async def transport(request):
        assert request.url.host == 'isolated.invalid'
        body = json.loads(request.content)
        assert not any(key in str(body) for key in ('wav_base64','local_path','voice_input_id'))
        posts.append(body)
        context = json.loads(body['messages'][1]['content'])
        dims = context.get('rubric',{}).get('dimensions',context.get('rubric_dimensions',[]))
        answer = context.get('response',context.get('child_response'))
        result = dict(understanding='independent',confidence=.92,evidence_summary='Synthetic transport only',
            spoken_feedback='请再核对所用条件。',next_check='能说明判别式符号吗？',criterion_evidence=[],
            rubric_dimensions=[dict(dimension_id=d['dimension_id'],score=candidate_score,response_quotes=[answer],
                rationale='Synthetic transport; not real quality evidence') for d in dims])
        from home_study_agent.learning_outcome_evaluator import FORMATIVE_SYSTEM_PROMPT
        if body['messages'][0]['content'] == FORMATIVE_SYSTEM_PROMPT:
            from formative_response_fixture import criterion_reply
            result = criterion_reply(context,result)
        if fault == 'revoked':
            service = RubricCalibrationService(db)
            current = service.read(meta['child_id'],meta['protocol_id'])
            service.revoke(meta['child_id'],meta['protocol_id'],expected_revision=current['revision'],
                client_action_id='abstention-late-revoke')
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]})
    def app():
        return create_app(settings,model_gateway=ModelGateway(settings,transport=httpx.MockTransport(transport)))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 201
        count_before = c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0]
    qualified = RubricCalibrationService(db).read(meta['child_id'],meta['protocol_id'])
    assert qualified['score_qualified'], qualified.get('blockers')
    if fault == 'write_failure':
        with db.connect() as c:
            c.execute("""CREATE TRIGGER reject_abstention_outcome BEFORE INSERT ON agent_outcome
                BEGIN SELECT RAISE(ABORT,'synthetic-abstention-write-failure'); END""")
            c.commit()
    application = app()
    measure = ExecutionMeasurement(monkeypatch,db,RubricCalibrationService,
        additional_databases=(application.state.repository.database,),
        additional_services=(application.state.rubric_calibration,))
    with TestClient(application,client=('127.0.0.1',41000),raise_server_exceptions=False) as client:
        response, metrics = measure.run(lambda:client.post('/v1/agent-turns/'+meta['turn_id']+'/execute'),lock_probe=True)
        if fault in {'write_failure','revoked'}:
            assert response.status_code == 409,response.text
            assert len(posts) == 1
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 200
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 201
                assert not c.execute('SELECT 1 FROM review_task').fetchone()
                if fault == 'write_failure':
                    assert c.execute("SELECT COUNT(*) FROM learning_evaluation_execution WHERE state='response_ready'").fetchone()[0] == 1
                    c.execute('DROP TRIGGER reject_abstention_outcome')
                    c.commit()
            if fault == 'revoked':
                return
            # Restart recovers the saved response, not a second provider call.
            with TestClient(app(),client=('127.0.0.1',41000),raise_server_exceptions=False) as reopened:
                response = reopened.post('/v1/agent-turns/'+meta['turn_id']+'/execute')
        assert response.status_code == 200,response.text
        assert len(posts) == 1
    with db.connect() as c:
        row = c.execute('''SELECT a.assessment_id,a.payload_json FROM rubric_assessment a
            JOIN agent_step s ON s.step_id=a.step_id JOIN agent_plan p USING(plan_id)
            WHERE p.agent_turn_id=?''',(meta['turn_id'],)).fetchone()
        assert row
        evidence = json.loads(row['payload_json'])['source_evidence_id']
        attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE evidence_id=?',(evidence,)).fetchone()
        prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()
        if candidate_score == 3:
            score = current_score(c,meta['child_id'],row['assessment_id'],prompt,attempt,now=datetime.now(timezone.utc))
            assert score['decision']['status'] == 'calibrated_model'
            assert score['receipt']['calibration_receipt']['modality'] == 'original_spoken_text'
            task = c.execute("SELECT review_task_id FROM review_task WHERE source_evidence_id=? AND status='pending'",(evidence,)).fetchone()[0]
        else:
            assert all(d['score'] is None for d in json.loads(row['payload_json'])['dimensions'])
            receipt = RubricCalibrationService(db).qualified_score_receipt(c,meta['child_id'],row['assessment_id'],
                protocol_id=meta['protocol_id'],as_of=datetime.now(timezone.utc))
            assert not receipt['score_qualified'] and receipt['blockers'] == ['assessment_abstained']
            binding = json.loads(row['payload_json'])['calibration_execution']['score_binding']
            with pytest.raises(ValueError,match='SCORE_BINDING_CHANGED'):
                RubricCalibrationService(db).revalidate_score_binding(c,meta['child_id'],binding,
                    phase='persist',as_of=datetime.now(timezone.utc),assessment_id=row['assessment_id'])
            assert not c.execute('SELECT 1 FROM review_task WHERE source_evidence_id=?',(evidence,)).fetchone()
            assert attempt['correctness'] is None
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?',(row['assessment_id'],)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == count_before+1
    from home_study_agent.child_model import ChildModelService
    learning = ChildModelService(db).snapshot(meta['child_id'])['payload']['learning']
    assert learning['qualified_original_count'] == (1 if candidate_score == 3 else 0)
    assert not learning['reliable_feedback']['mastery_claim_allowed']
    if candidate_score != 3:
        assert learning['reliable_feedback']['nodes'] == []
    with TestClient(app(),client=('127.0.0.1',41000),raise_server_exceptions=False) as client:
        response = client.post('/v1/agent-turns/'+meta['turn_id']+'/execute')
        assert response.status_code == 200,response.text
        assert len(posts) == 1
    assert ChildModelService(db).snapshot(meta['child_id'])['payload']['learning']['reliable_feedback'] == learning['reliable_feedback']
    if candidate_score != 3:
        print('SYNTHETIC_ORAL_ABSTENTION_METRICS',json.dumps(metrics),flush=True)
        return
    with db.connect() as c:
        voice = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
            (prompt['authorized_turn_id'],)).fetchone()[0])['source_voice_input_id']
        audio = Path(c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=?',(voice,)).fetchone()[0])
    assert audio.resolve().is_relative_to(media_root)
    retained = audio.read_bytes()
    try:
        audio.unlink()
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            assert reconcile_school_reviews(c,meta['child_id'])['cancelled'] == [task]
            assert c.execute('SELECT correctness FROM diagnostic_attempt WHERE evidence_id=?',(evidence,)).fetchone()[0] is None
            c.commit()
    finally:
        audio.write_bytes(retained)
    print('SYNTHETIC_ORAL_EXECUTOR_METRICS',json.dumps(metrics),flush=True)


@pytest.mark.parametrize('fault',['write_failure','revoked'])
def test_abstention_preserves_recovery_without_bypassing_late_revocation(tmp_path,monkeypatch,fault):
    test_signed_oral_checkpoint_final_executor_restart_and_recording_loss(tmp_path,monkeypatch,1.5,fault)
