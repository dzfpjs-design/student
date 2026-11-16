"""Whole review -> real next speech remains a reference, never mastery."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.document_spoken_supplement import teaching_followup
from home_study_agent.document_spoken_review import SpokenReview
from test_document_open_check import opened
from test_document_open_followup import evaluated
from test_document_spoken_review import review
from test_document_spoken_conversation import supplement
from test_document_revisit import say


@pytest.mark.parametrize('fault',['none','teacher','timing'])
def test_teaching_response_source_restart_and_revocation_without_losing_answer(tmp_path,monkeypatch,fault):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'trace-first')
        result,_=review(j,monkeypatch,'trace-review')
        reviewed=result['execution']['result']['whole_review']
        answer=supplement(j,monkeypatch,parent,'我把每组数量乘以组数，检查是否等于总量。','trace-answer')
        with j['db'].connect() as c:
            row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(answer['prompt_id'],)).fetchone()
            trace=teaching_followup(c,row)
            assert trace and trace['status']=='linked_reference',trace
            assert trace['parent_prompt_id']==parent['prompt_id']
            assert trace['tutor_turn_id']==reviewed['tutor_turn_id']
            assert trace['composition_version']==reviewed['composition_version']
            assert trace['followup_question']==reviewed['review']['next_prompt']
            assert trace['delivery_verified'] is False and trace['learning_effect']=='not_established'
            assert trace['cloud_transmission_allowed'] is False and trace['mastery_update_allowed'] is False
            attempt=dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(row['attempt_id'],)).fetchone())
            assert trace['response_evidence_id']==attempt['evidence_id']
            turn_id=row['authorized_turn_id']
            stored=c.execute('''SELECT s.result_json FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE p.agent_turn_id=? AND s.step_index=0''',(turn_id,)).fetchone()
            assert json.loads(stored[0])['result']['teaching_followup']==trace
            # Later answer makes the review stale for current readback, but
            # does not erase the review's original historical composition.
            with pytest.raises(ValueError,match='DOCUMENT_WHOLE_SOURCE_CHANGED'):
                SpokenReview(None,None,None,None,None,None).saved(c,reviewed['request_turn_id'])
            assert SpokenReview(None,None,None,None,None,None).saved(c,reviewed['request_turn_id'],historical=True)==reviewed
            if fault=='teacher':
                c.execute("UPDATE tutor_turn SET safety_status='blocked' WHERE turn_id=?",(reviewed['tutor_turn_id'],))
            elif fault=='timing':
                c.execute("UPDATE tutor_turn SET created_at='2099-01-01T00:00:00+00:00' WHERE turn_id=?",(reviewed['tutor_turn_id'],))
            c.commit()
        calls=len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            replay=client.post('/v1/agent-turns/'+turn_id+'/execute')
            assert replay.status_code==200,replay.text
            returned=replay.json()['execution']['result']['teaching_followup']
            assert returned['status']==('linked_reference' if fault=='none' else 'attribution_unavailable')
            assert replay.json()['execution']['replay_audio_allowed'] is False
            assert len(j['calls'])==calls
            with j['db'].connect() as c:
                kept=dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(row['attempt_id'],)).fetchone())
                assert kept==attempt
        for call in j['calls']:
            wire=json.dumps(call,ensure_ascii=False)
            assert 'document_teaching_followup_v1' not in wire
            assert reviewed['tutor_turn_id'] not in wire


@pytest.mark.parametrize('fault',['teacher','marker'])
def test_assisted_response_to_new_check_persists_without_certifying_mastery(tmp_path,monkeypatch,fault):
    from test_document_open_check import answer_open,evaluation,snapshot
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'chain-first')
        reviewed,_=review(j,monkeypatch,'chain-review')
        reply=supplement(j,monkeypatch,parent,'我用每组数量乘以组数，核对总量。','chain-response')
        result=say(j,monkeypatch,'换一道新题看看我能不能自己做','independent_check','chain-check')
        check=result['execution']['result']['diagnostic_prompt']
        assert check['status']=='waiting_answer',result
        context=check['teaching_context']
        assert context['status']=='linked_reference'
        assert context['source_trace']['response_prompt_id']==reply['prompt_id']
        assert context['source_trace']['tutor_turn_id']==reviewed['execution']['result']['whole_review']['tutor_turn_id']
        assert check['prompt']['diagnostic_item_id']!=reply['diagnostic_item_id']
        saved,_=answer_open(j,monkeypatch,check['prompt'],'spoken_explanation','chain-independent')
        assert saved['teaching_context']==context
        assert saved['attempt']['correctness'] is None and saved['attempt']['score_status']=='needs_review'
        feedback_id=saved['local_learning_evaluation']['feedback_turn_id']
        j['responses'].append(evaluation(j,check['prompt']))
        feedback=j['client'].post('/v1/agent-turns/'+feedback_id+'/execute')
        assert feedback.status_code==200,feedback.text
        assert feedback.json()['execution']['result']['formative_only'] is True
        assert snapshot(j)['practice']['independent_results'][-1]['attempt_id']==saved['attempt']['attempt_id']
        calls=len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as client:
            found=client.get('/v1/sessions/'+j['sid']+'/diagnostic-prompt',params={'prompt_id':check['prompt']['prompt_id']})
            assert found.status_code==200,found.text
            assert found.json()['teaching_context']==context
            with j['db'].connect() as c:
                if fault=='teacher':
                    c.execute("UPDATE tutor_turn SET safety_status='blocked' WHERE turn_id=?",(context['source_trace']['tutor_turn_id'],))
                else:
                    c.execute("UPDATE diagnostic_prompt SET request_json=json_set(request_json,'$.document_teaching_context.source_trace.learning_effect','mastered') WHERE prompt_id=?",
                        (check['prompt']['prompt_id'],))
                c.commit()
            found=client.get('/v1/sessions/'+j['sid']+'/diagnostic-prompt',params={'prompt_id':check['prompt']['prompt_id']})
            assert found.status_code==200,found.text
            assert found.json()['teaching_context']['status']=='attribution_unavailable'
            assert found.json()['attempt']['attempt_id']==saved['attempt']['attempt_id']
            assert len(j['calls'])==calls
        for call in j['calls']:
            assert 'source_trace' not in json.dumps(call)
