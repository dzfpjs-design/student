"""Recorded paid synthetic responses through HTTP/SQLite, not live ASR/TTS.

The archive is not a scorer calibration certificate. Network replay keeps its
fixture provider identity so a paid external response cannot forge app authority.
"""
import hashlib
import json
from synthetic_oral_app_lifecycle import fresh_oral_case
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_outcome_evaluator import FORMATIVE_SYSTEM_PROMPT,LearningOutcomeEvaluator,parse_learning_evaluation
from home_study_agent.formative_criterion_review import parse as parse_review
from home_study_agent.learning_rubric import RubricEvidenceService
from test_open_response_spoken_runtime_http import test_natural_spoken_process_asr_bound_answer_formative_and_restart as run_journey

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'eval'))
from school_formative_cases import cases,compare_review as compare


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def recorded(case_id):
    suite='heldout' if case_id in {c['case_id'] for c in cases('heldout')} else 'regression'
    name='heldout' if suite=='heldout' else 'pro'
    archive=json.loads((Path(__file__).parent/'fixtures'/f'school-criterion-review-{name}-20260914.json').read_text())
    assert archive['synthetic_only'] and not archive['family_data_used'] and not archive['calibration_authority']
    assert archive['prompt_hash']==fingerprint(FORMATIVE_SYSTEM_PROMPT)
    case=next(c for c in cases(suite) if c['case_id']==case_id)
    row=next(c for c in archive['cases'] if c['case_id']==case_id)
    assert row['payload_hash']==fingerprint(case['payload'])
    assert row['finish_reason']=='stop' and row['protocol_valid']
    assert row['model']=='deepseek-v4-pro' and row['max_tokens']==2400
    return case,row


@pytest.mark.parametrize('case_id',[c['case_id'] for suite in ('regression','heldout') for c in cases(suite)])
def test_recorded_synthetic_protocol_and_dimension_thresholds(case_id):
    case,row=recorded(case_id)
    result=parse_review(case['payload'],row['candidate'])
    LearningOutcomeEvaluator._validate_formative_receipt(case['payload'],result)
    assert compare(case,result)['passed']


@pytest.mark.parametrize('case_id',['grouping-correct','grouping-misread','roots-contradiction','price-cancelled-errors','english-correct'])
def test_prepaid_model_response_survives_actual_submission_and_app_restart(tmp_path,monkeypatch,case_id):
    case,row=recorded(case_id)
    candidate=parse_review(case['payload'],row['candidate'])
    assert compare(case,candidate)['passed']

    def inspect(j):
        client=j['client']
        assert j['follow']['status']=='feedback_required'
        tid=j['follow']['feedback_turn_id']
        response=client.post(f'/v1/agent-turns/{tid}/execute')
        assert response.status_code==200,response.text
        result=response.json()['execution']['result']
        assert result['formative_only'] and not result['automatic_certification']
        assert result['spoken_feedback']==candidate['spoken_feedback']
        # The delivery filter spaces sentence boundaries; it must not add a
        # new task, drop feedback, or alter any non-whitespace content.
        assert ''.join(response.json()['turn']['response_text'].split())==''.join(candidate['spoken_feedback'].split())
        assert result['criterion_evidence']==candidate['criterion_evidence']
        assert result['criterion_review']==candidate['criterion_review']
        assert response.json()['execution']['replay_audio_allowed'] is True
        assert len(j['evaluations'])==1

        with j['db'].connect() as c:
            assessment_id=c.execute('SELECT assessment_id FROM rubric_assessment').fetchone()[0]
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert not c.execute('SELECT 1 FROM mastery_state WHERE child_id=? AND evidence_count>0',(j['child'],)).fetchone()
            usage=c.execute('''SELECT m.status,m.prompt_tokens,m.completion_tokens FROM model_call m
                JOIN rubric_assessment a ON a.model_call_id=m.call_id WHERE a.assessment_id=?''',(assessment_id,)).fetchone()
            assert tuple(usage)==('completed',row['prompt_tokens'],row['completion_tokens'])
        saved=RubricEvidenceService(j['db']).get(j['child'],assessment_id)
        assert saved['score_status']=='candidate'
        assert {d['dimension_id']:d['score'] for d in saved['dimensions']}==row['scores']

        before=len(j['posts'])
        with fresh_oral_case({**j,'client_options':{}},TestClient,client=('testclient',50000)) as restarted_case:
            restarted=restarted_case['client']
            restored=restarted.get(f"/v1/sessions/{j['sid']}/diagnostic-prompt",params={'prompt_id':j['prompt']['prompt_id']})
            assert restored.status_code==200,restored.text
            assert restored.json()['attempt']['submitted_answer']==case['payload']['response']
            replay=restarted.post(f'/v1/agent-turns/{tid}/execute')
            assert replay.status_code==200,replay.text
            assert replay.json()['execution']['result']['spoken_feedback']==candidate['spoken_feedback']
            assert replay.json()['execution']['replay_audio_allowed'] is False
            status=restarted.get(f'/v1/agent-turns/{tid}/execution-status')
            assert status.status_code==200,status.text
            assert status.json()['execution']['execution']['replay_audio_allowed'] is False
            duplicate=restarted.post(f"/v1/sessions/{j['sid']}/diagnostic-prompt/answer",json=j['payload'])
            assert duplicate.status_code==200,duplicate.text
            assert duplicate.json()['attempt']['attempt_id']==j['attempt']['attempt_id']
            recovered=RubricEvidenceService(restarted.app.state.repository.database).get(j['child'],assessment_id)
            assert recovered==saved
            assert len(j['posts'])==before

    prefix='authored-p4:' if case['grade']=='四年级' else 'authored:'
    run_journey(tmp_path,monkeypatch,prefix+case['subject'],None,False,None,
        after_saved=inspect,probe_case=case,grader_reply=row)
