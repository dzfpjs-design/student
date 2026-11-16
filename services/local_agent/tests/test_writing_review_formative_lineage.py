"""An earlier full read is an ancestor, not the corrected original's identity."""
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.formative_evaluation_projection import build_formative_projection
from test_paper_writing_runtime_http import PaperGateway
from test_writing_ocr_warning_voice import warning_read, finish_read
from test_writing_voice_runtime_http import environment, capture, interpret, TEXT


@pytest.mark.parametrize('fault',[None,'ancestor_retracted','ancestor_read_changed','correction_text_changed'])
def test_corrected_warning_formative_checks_complete_review_lineage(environment,monkeypatch,fault):
    settings,db=environment
    gateway=PaperGateway()
    app=create_app(settings,model_gateway=gateway)
    with TestClient(app) as client:
        sid,prompt,reading,_=warning_read(client,db,settings,monkeypatch,gateway)
        finish_read(client,sid,reading,'lineage-first-read')
        quote='识别更正，把“Saturday”改成“Sunday”'
        voice=capture(client,settings,monkeypatch,sid,quote,prompt,'lineage-correction-voice')
        corrected=interpret(client,sid,quote,'lineage-correction',voice)
        finish_read(client,sid,corrected,'lineage-corrected-read')
        app.state.privacy_lifecycle.update_preferences({'cloud_model_text_allowed':True})
        gateway.quote,gateway.control='对，刚读出的就是我的作答，文字没有错','writing.confirm_original'
        voice=capture(client,settings,monkeypatch,sid,gateway.quote,prompt,'lineage-confirm-voice')
        confirmed=interpret(client,sid,gateway.quote,'lineage-confirm',voice)
        submitted=client.post(f"/v1/agent-turns/{confirmed['agent_turn_id']}/execute")
        assert submitted.status_code==200,submitted.text
        follow=submitted.json()['turn']['local_evaluation']
        assert follow['status']=='feedback_required', {k:follow.get(k) for k in ('status','reason')}
        with db.connect() as c:
            original=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
            plan=c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?',(follow['feedback_turn_id'],)).fetchone()
            ancestor=reading['action']['writing_ocr_confirmation']['confirmation_id']
            if fault=='ancestor_retracted':
                from home_study_agent.repository import utc_now
                c.execute("INSERT INTO writing_ocr_event VALUES('retracted-ancestor',?,?,'retract','{}','{}',?,'retracted-ancestor')",
                    (ancestor,original['child_id'],utc_now()))
            elif fault=='ancestor_read_changed':
                c.execute("UPDATE writing_ocr_event SET payload_json='{}' WHERE confirmation_id=? AND kind='ended'",(ancestor,))
            elif fault=='correction_text_changed':
                c.execute("UPDATE writing_ocr_review SET review_json=json_set(review_json,'$.text','invented prose') WHERE confirmation_id!=?",(ancestor,))
            c.commit()
            before=list(c.iterdump())
            projection=build_formative_projection(c,child_id=original['child_id'],session_id=sid,
                prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],objective_id=plan[0])
            assert projection['status']==('local_required' if fault else 'ready_for_runtime_checks'),projection.get('reason')
            assert list(c.iterdump())==before
        if fault:return
        result=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert result.status_code==200,result.text
        assessment=result.json()['execution']['result']
        assert assessment['formative_only'] and not assessment['automatic_certification']
        assert len(gateway.evaluations)==1
        assert json.loads(gateway.evaluations[0].messages[1]['content'])['response']==TEXT.replace('Saturday','Sunday')
        with db.connect() as c:
            assert c.execute("SELECT status FROM capture_artifact WHERE artifact_id='page'").fetchone()[0]=='quality_warning'
            assert c.execute('SELECT review_required FROM document_page_revision').fetchone()[0]==1
    with TestClient(create_app(settings,model_gateway=gateway)) as client:
        replay=client.post(f"/v1/agent-turns/{follow['feedback_turn_id']}/execute")
        assert replay.status_code==200,replay.text
        assert len(gateway.evaluations)==1
