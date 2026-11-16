from dataclasses import replace
import hashlib
import json
import pytest

from test_native_writing_shell_http import scheduled, create_app, _http_client, headers, draft, KEY
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('quote,control,action', [
    ('刚才作文交上了吗', 'native_writing.submission_status', 'check_native_writing_submission'),
    ('帮我查一下上一份原稿有没有存好', 'native_writing.submission_status', 'check_native_writing_submission'),
    ('继续提交本题', 'native_writing.submit_retained', 'submit_retained_native_writing'),
])
def test_real_asr_semantic_native_recovery_binds_local_original_not_control_utterance(tmp_path, monkeypatch, quote, control, action):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body=draft(case)
        settings=replace(case['settings'],native_launch_attestation_key=KEY)
        with _http_client(create_app(settings,model_gateway=case['gateway_factory']())) as client:
            saved=client.post(f'/v1/sessions/{case["sid"]}/native-writing-original',json=body,headers=headers(body))
            assert saved.status_code==200,saved.text
            case['transport'].control=control
            before=len(case['transport'].calls)
            voice=capture(client,settings,monkeypatch,case['sid'],quote,case['prompt'])
            heard=interpret(client,case['sid'],quote,'native-semantic-control',voice)
            assert heard['action']['type']==action,heard['action']
            expected={**{k:body[k] for k in ('child_id','session_id','prompt_id','item_version','diagnostic_item_id')},
                      'text_sha256':hashlib.sha256(body['text'].encode()).hexdigest()}
            assert heard['action']['native_writing_target']==expected
            assert heard['runtime_capability']==control
            assert heard['after_execution']=='stop'
            requests=case['transport'].calls[before:]
            assert len(requests)==1
            sent=json.loads(requests[0]['messages'][-1]['content'])
            assert set(sent)=={'transcript','state'}
            assert sent['transcript']==quote
            assert sent['state']['has_native_writing_submission'] is True
            serialized=json.dumps(requests,ensure_ascii=False)
            assert body['text'] not in serialized and body['prompt_id'] not in serialized
            from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
            with case['db'].connect() as c:
                # Interpretation/dispatch identity does not submit or evaluate.
                assert c.execute('SELECT answer_action_id FROM diagnostic_prompt WHERE prompt_id=?',(body['prompt_id'],)).fetchone()[0] is None
                AgentRuntimeStore.inspect_voice_control_execution(c,heard['agent_turn_id'],allow_planned=True)
                c.execute("UPDATE capture_artifact SET status='withdrawn' WHERE artifact_id=?",(saved.json()['artifact_id'],))
                with pytest.raises(InvalidAgentPlanError):
                    AgentRuntimeStore.inspect_voice_control_execution(c,heard['agent_turn_id'],allow_planned=True)
                c.rollback()


@pytest.mark.parametrize('cancelled',[False,True])
def test_semantic_retained_answer_rechecks_control_inside_answer_writer(tmp_path,monkeypatch,cancelled):
    with scheduled(tmp_path) as case:
        case['finish_client']()
        body=draft(case)
        settings=replace(case['settings'],native_launch_attestation_key=KEY)
        with _http_client(create_app(settings,model_gateway=case['gateway_factory']())) as client:
            base=f'/v1/sessions/{case["sid"]}'
            original=client.post(base+'/native-writing-original',json=body,headers=headers(body)).json()
            case['transport'].control='native_writing.submit_retained'
            quote='继续提交本题'
            voice=capture(client,settings,monkeypatch,case['sid'],quote,case['prompt'])
            heard=interpret(client,case['sid'],quote,'native-semantic-submit',voice)
            admission=client.post(base+f'/agent-turns/{heard["agent_turn_id"]}/control-admission')
            assert admission.status_code==200,admission.text
            assert admission.json()['status']=='admitted'
            if cancelled:
                with case['db'].connect() as c:
                    c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(heard['agent_turn_id'],))
                    c.execute("UPDATE agent_step SET status='cancelled' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",(heard['agent_turn_id'],))
                    c.commit()
            result=client.post(base+'/diagnostic-prompt/answer',json=dict(prompt_id=body['prompt_id'],item_version=body['item_version'],
                answer=body['text'],client_action_id='native-control-answer',authorized_turn_id=heard['agent_turn_id'],
                writing_original_artifact_id=original['artifact_id'],writing_original_revision=original['original_revision']))
            assert result.status_code==(409 if cancelled else 200),result.text
            with case['db'].connect() as c:
                row=c.execute('SELECT answer_action_id FROM diagnostic_prompt WHERE prompt_id=?',(body['prompt_id'],)).fetchone()
                assert (row[0] is None)==cancelled
            if not cancelled:
                assert result.json()['attempt']['submitted_answer']==body['text']
