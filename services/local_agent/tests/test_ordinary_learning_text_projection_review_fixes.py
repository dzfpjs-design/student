"""Only new reviewer findings; source/voice/Tutor/HTTP remain real and synthetic."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_ordinary_learning_text_projection_http import (env, PrivacyGateway, source_pipeline, interpret_voice,
    pending, execute, evaluation_requests, ANSWER, CHECK, QUESTION)


class ReviewGateway(PrivacyGateway):
    mode = 'normal'
    before_eval = None
    async def complete(self, request):
        if request.request_type == 'learning_outcome_evaluation' and self.before_eval:
            hook=self.before_eval;self.before_eval=None;hook()
        return await super().complete(request)
    def transport(self, request):
        kind=self.kind.get()
        if kind and kind.startswith('tutor_hint_level_') and self.mode != 'normal':
            self.transports.append(kind)
            if self.mode=='keyword_board':
                segments=[{'narration':'为什么要先选角？因为边的关系依赖这个角。','blocks':[{'kind':'text','text':'先选定角'}]},
                    {'narration':CHECK,'blocks':[{'kind':'question','text':'对边/斜边；邻边/斜边'}]}]
            elif self.mode=='no_question':
                segments=[{'narration':'两个比都要先选定角，再确认对边和邻边。','blocks':[{'kind':'text','text':'先选定角'}]}]
            else:
                segments=[{'narration':'正弦比较哪条边？余弦比较哪条边？','blocks':[{'kind':'question','text':'比较两种边'}]}]
            content=json.dumps({'blackboard':{'title':'合成概念','segments':segments}},ensure_ascii=False)
            return httpx.Response(200,request=request,json={'choices':[{'finish_reason':'stop','message':{'content':content}}],
                'usage':{'prompt_tokens':20,'completion_tokens':25}})
        return super().transport(request)


def fixture(env,tmp_path,monkeypatch,mode='normal'):
    db,_=env;monkeypatch.setenv('HSC_DATA_DIR',str(tmp_path));monkeypatch.setenv('HSC_DATABASE_PATH',str(db.path))
    settings=Settings(data_dir=tmp_path,database_path=db.path,maintenance_enabled=False,
        model_api_key='SYNTHETIC_NO_REAL_ACCOUNT',model_base_url='https://synthetic.invalid',fallback_model_api_key=None)
    g=ReviewGateway(settings);g.mode=mode
    return db,g,TestClient(create_app(settings,model_gateway=g))


def rename_other(client,headers,db,name):
    with db.connect() as c:region=c.execute("SELECT curriculum_region FROM child_profile WHERE child_id='child-junior-3'").fetchone()[0]
    r=client.put('/v1/parent/children/child-junior-3/profile',headers=headers,json={'nickname':name,'curriculum_region':region})
    assert r.status_code==200,r.text


@pytest.mark.parametrize('rename',[False,True])
def test_known_sibling_name_in_original_candidate_stays_local_even_after_parent_renames(env,tmp_path,monkeypatch,rename):
    db,g,client=fixture(env,tmp_path,monkeypatch)
    with client:
        sid,headers,_,_=source_pipeline(client,g,db);ref=pending(client,sid)
        with db.connect() as c:name=c.execute("SELECT nickname FROM child_profile WHERE child_id='child-junior-3'").fetchone()[0]
        raw=ANSWER+'我和'+name+'的说法一样。'
        candidate,original=interpret_voice(client,g,sid,raw,'review-private-sibling-candidate',ref)
        assert candidate['action']['pending_answer_candidate']['answer']==raw
        kept=execute(client,candidate);assert kept.status_code==200,kept.text
        if rename:rename_other(client,headers,db,'新的合成昵称')
        confirm,_=interpret_voice(client,g,sid,'刚才那句就是我的答案','review-private-sibling-confirm',ref)
        assert confirm['action']['answer_source_turn_id']==candidate['agent_turn_id']
        result=execute(client,confirm)
        assert result.status_code==409,result.text
        assert not evaluation_requests(g)
        with db.connect() as c:
            row=c.execute('SELECT transcript,action_json FROM agent_turn WHERE agent_turn_id=?',(candidate['agent_turn_id'],)).fetchone()
            assert row['transcript']==raw and json.loads(row['action_json'])['pending_answer_candidate']['answer']==raw
            assert '_ordinary_learning_text_input_policy_v1' not in json.loads(row['action_json'])
            assert c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0]==0


def test_fresh_family_name_version_rejects_inflight_dispatch_before_provider(env,tmp_path,monkeypatch):
    db,g,client=fixture(env,tmp_path,monkeypatch)
    with client:
        sid,headers,_,_=source_pipeline(client,g,db)
        turn,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'review-names-version-answer',pending(client,sid))
        from home_study_agent.repository import StudyRepository
        with db.connect() as c:region=c.execute("SELECT curriculum_region FROM child_profile WHERE child_id='child-junior-3'").fetchone()[0]
        g.before_eval=lambda:StudyRepository(db).update_child_profile('child-junior-3','在途新的合成昵称',region)
        result=execute(client,turn)
        with db.connect() as c:assert c.execute("SELECT nickname FROM child_profile WHERE child_id='child-junior-3'").fetchone()[0]=='在途新的合成昵称'
        assert result.status_code==409,result.text
        assert 'learning_outcome_evaluation' not in g.transports
        with db.connect() as c:assert c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0]==0


def test_frozen_last_narration_question_survives_keyword_board_and_earlier_rhetorical_question(env,tmp_path,monkeypatch):
    db,g,client=fixture(env,tmp_path,monkeypatch,'keyword_board')
    with client:
        sid,_,_,tutor=source_pipeline(client,g,db)
        assert tutor['output_envelope']['result']['blackboard']['segments'][-1]['narration']==CHECK
        turn,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'review-keyword-board-answer',pending(client,sid))
        result=execute(client,turn);assert result.status_code==200,result.text
        assert json.loads(evaluation_requests(g)[0].messages[-1]['content'])=={
            'question_asked':CHECK,'child_response':'我的答案是'+ANSWER,'subject':'数学'}


@pytest.mark.parametrize('mode',['no_question','two_questions'])
def test_only_trusted_lesson_without_unique_last_question_asks_existing_fixed_prompt_without_scoring(env,tmp_path,monkeypatch,mode):
    from home_study_agent.ordinary_learning_text_projection import APP_PROMPT
    db,g,client=fixture(env,tmp_path,monkeypatch,mode)
    with client:
        sid,_,_,_=source_pipeline(client,g,db)
        text='我的答案是'+ANSWER
        turn,_=interpret_voice(client,g,sid,text,'review-no-unique-question-answer',pending(client,sid))
        assert turn['runtime_capability']=='learning.request_response',turn
        result=execute(client,turn);assert result.status_code==200,result.text
        assert result.json()['execution']['response_text']==APP_PROMPT
        assert not evaluation_requests(g)
        with db.connect() as c:
            row=c.execute('SELECT transcript,action_json FROM agent_turn WHERE agent_turn_id=?',(turn['agent_turn_id'],)).fetchone()
            assert row['transcript']==text and 'pending_answer_candidate' not in json.loads(row['action_json'])
            assert c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0]==0


@pytest.mark.parametrize('fault',['privacy_transform','model_purpose','material_with_no_question'])
def test_provenance_or_material_block_cannot_borrow_trusted_no_question_route(env,tmp_path,monkeypatch,fault):
    db,g,client=fixture(env,tmp_path,monkeypatch,'no_question' if fault=='material_with_no_question'else'normal')
    with client:
        sid,_,source,tutor=source_pipeline(client,g,db)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if fault=='material_with_no_question':
                row=c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',(source['agent_turn_id'],)).fetchone();a=json.loads(row[0]);a['document_context']={'local_only':True,'cloud_transmission_allowed':False,'source_ref':'private_synthetic_book'}
                c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?',(json.dumps(a,ensure_ascii=False),source['agent_turn_id']))
            elif fault=='model_purpose':c.execute("UPDATE model_call SET purpose='synthetic_unrelated_purpose' WHERE call_id=?",(tutor['output_envelope']['request_id'],))
            else:
                row=c.execute('SELECT context_manifest_json FROM model_call WHERE call_id=?',(tutor['output_envelope']['request_id'],)).fetchone();m=json.loads(row[0]);m['privacy_transform']['transmitted']=False
                c.execute('UPDATE model_call SET context_manifest_json=? WHERE call_id=?',(json.dumps(m,ensure_ascii=False),tutor['output_envelope']['request_id']))
            c.commit()
        turn,_=interpret_voice(client,g,sid,'我的答案是'+ANSWER,'review-source-integrity-answer',pending(client,sid))
        assert turn['runtime_capability']!='learning.request_response',turn
        result=execute(client,turn);assert result.status_code in {409,422},result.text
        assert not evaluation_requests(g)
        with db.connect() as c:assert c.execute('SELECT count(*) FROM agent_outcome').fetchone()[0]==0
