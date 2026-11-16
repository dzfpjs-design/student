"""Voice undo keeps evidence and later independent supplements intact."""
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_document_open_check import opened
from test_document_open_followup import evaluated
from test_document_revisit import say
from test_document_spoken_revision import corrected
from test_document_spoken_conversation import supplement
from test_document_spoken_location import pending,locate


def undo(j,monkeypatch,key,execute=True):
    return say(j,monkeypatch,'撤回我刚才最后一次更正','undo_revision',key,execute=execute)


def test_repeated_public_correction_prefix_does_not_expose_old_answer_core():
    from home_study_agent.voice_control_intent import VoiceControlIntent as V
    old='第一段改成：先算差。'
    new='第一段改成：先算和，再分组。'
    assert not V.text_eligible(new,[],[old])
    assert V.text_eligible(new,[],[old],revision_command=True)
    assert not V.text_eligible(old,[],[old],revision_command=True)
    assert not V.text_eligible(old.rstrip('。'),[],[old],revision_command=True)
    private='这段私有解释不得再次外发给云端'
    assert not V.text_eligible('第一段改成：'+private,[],[private],revision_command=True)
    assert not V.text_eligible('第一段改成：小林的答案。',['小林'],[],revision_command=True)


def test_explicit_undo_target_cannot_be_dropped_by_model():
    from home_study_agent.document_revisit import parse,parse_journal
    text='撤回第二段的更正'
    raw=dict(meaning='undo_revision',quote=text,time=None,confidence=.99)
    anchor='2026-09-13T00:00:00+00:00'
    with pytest.raises(ValueError,match='DOCUMENT_UNDO_TARGET_REQUIRED'):
        parse(json.dumps(raw),text,anchor)
    parsed=parse(json.dumps(dict(raw,undo=dict(target=2,target_quote='第二段'))),text,anchor)
    assert parse_journal(parsed,text,anchor)==parsed


def test_targeted_undo_does_not_cancel_a_later_change_to_another_segment(tmp_path,monkeypatch):
    from test_voice_runtime_binding import capture,interpret
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'selected-first')
        first=j['answer']
        second=supplement(j,monkeypatch,parent,'接着核对数量单位。','selected-second')
        a=corrected(j,monkeypatch,second,'第一段改成：先找出未知量。',
            dict(target=1,target_quote='第一段',replacement_quote='先找出未知量。'),'selected-correct-first')
        b=corrected(j,monkeypatch,a['execution']['result']['diagnostic_prompt']['prompt'],'第二段改成：再列出等式。',
            dict(target=2,target_quote='第二段',replacement_quote='再列出等式。'),'selected-correct-second')
        text='撤回第一段的更正'
        j['responses'].append(json.dumps(dict(meaning='undo_revision',quote=text,time=None,confidence=.99,
            undo=dict(target=1,target_quote='第一段'))))
        voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],text,key='selected-undo-voice')
        turn=interpret(j['client'],j['sid'],text,'selected-undo',voice)
        response=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert response.status_code==200,response.text
        assert [s['text'] for s in response.json()['execution']['result']['spoken_conversation']['effective_segments']]==[first,'再列出等式。']
        last=undo(j,monkeypatch,'selected-undo-last')
        assert [s['text'] for s in last['execution']['result']['spoken_conversation']['effective_segments']]==[first,'接着核对数量单位。']
        assert len(b['execution']['result']['supplement_conversation']['segments'])==4


def test_undo_explicit_revision_preserves_later_supplement_and_replays_old_versions(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'undo-first')
        old=j['answer']
        changed=corrected(j,monkeypatch,parent,'第一段改成：先算差。',
            dict(target=1,target_quote='第一段',replacement_quote='先算差。'),'undo-change')
        prompt=changed['execution']['result']['diagnostic_prompt']['prompt']
        latest=supplement(j,monkeypatch,prompt,'还需要核对题目单位。','undo-supplement')
        prior=say(j,monkeypatch,'当前解释读回来','status','undo-prior')
        calls=len(j['calls'])
        result=undo(j,monkeypatch,'undo-control')
        chain=result['execution']['result']['spoken_conversation']
        assert [s['text'] for s in chain['effective_segments']]==[old,'还需要核对题目单位。']
        assert len(chain['segments'])==3 and len(chain['undo_events'])==1
        assert '先算差' not in result['execution']['response_text']
        assert chain['mastery_update_allowed'] is False and chain['combined_assessment_status']=='not_evaluated'
        assert len(j['calls'])==calls+1
        assert json.loads(j['calls'][-1]['messages'][1]['content'])['transcript']=='撤回我刚才最后一次更正'
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            j['client'] = restarted
            for original in (prior,result):
                replay=restarted.post('/v1/agent-turns/'+original['turn']['agent_turn_id']+'/execute')
                assert replay.status_code==200,replay.text
                assert replay.json()['execution']['result']['spoken_conversation']==original['execution']['result']['spoken_conversation']
                assert replay.json()['execution']['replay_audio_allowed'] is False
            amended=corrected(j,monkeypatch,latest,'第一段改成：先算和，再分组。',
                dict(target=1,target_quote='第一段',replacement_quote='先算和，再分组。'),'undo-new-change')
            assert amended['execution']['result']['supplement_conversation']['effective_segments'][0]['text']=='先算和，再分组。'
            again=undo(j,monkeypatch,'undo-again')
            assert again['execution']['result']['spoken_conversation']['effective_segments'][0]['text']==old
            empty=undo(j,monkeypatch,'undo-no-more')
            assert '没有尚可撤回' in empty['execution']['response_text']
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0]==4
                assert not c.execute('SELECT 1 FROM diagnostic_attempt WHERE correctness IS NOT NULL').fetchone()


@pytest.mark.parametrize('resolved',[False,True])
def test_undo_pending_or_located_correction_does_not_reopen_clarification(tmp_path,monkeypatch,resolved):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        pending(j,monkeypatch,'undo-pending')
        if resolved:
            locate(j,monkeypatch,'第一段','undo-located')
        result=undo(j,monkeypatch,'undo-pending-control')
        chain=result['execution']['result']['spoken_conversation']
        assert not chain['pending_revisions']
        assert chain['effective_segments'][0]['text']==chain['segments'][0]['transcript']
        assert '你要改第几段' not in result['execution']['response_text']
        status=say(j,monkeypatch,'现在采用什么说法','status','undo-pending-status')
        assert not status['execution']['result']['spoken_conversation']['pending_revisions']
        assert '旧评价仅留在历史' in status['execution']['response_text']


def test_readback_cannot_present_a_retracted_fragment_score_as_current_feedback():
    from home_study_agent.document_spoken_conversation import response
    chain=dict(segments=[dict(prompt_id='original',transcript='old'),dict(prompt_id='correction',transcript='new')],
        active_prompt_ids=['original','correction'],undo_events=[dict(turn_id='undo')],pending_revisions=[],
        effective_segments=[dict(source_prompt_id='original',text='old',original_text=True)])
    text=response(chain,'STALE-REFERENCE-SCORE')
    assert 'STALE-REFERENCE-SCORE' not in text and '尚未对当前完整说法重新评价' in text


def test_successive_undo_restores_versions_in_reverse_order(tmp_path,monkeypatch):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        parent,_,_=evaluated(j,monkeypatch,'versions-first')
        original=j['answer']
        for i,new in enumerate(('先求和。','先分组。')):
            change=corrected(j,monkeypatch,parent,'第一段改成：'+new,
                dict(target=1,target_quote='第一段',replacement_quote=new),'versions-'+str(i))
            parent=change['execution']['result']['diagnostic_prompt']['prompt']
        first=undo(j,monkeypatch,'versions-back-one')
        assert first['execution']['result']['spoken_conversation']['effective_segments'][0]['text']=='先求和。'
        second=undo(j,monkeypatch,'versions-back-two')
        assert second['execution']['result']['spoken_conversation']['effective_segments'][0]['text']==original


@pytest.mark.parametrize('fault',['voice','event','journal','cancel'])
def test_undo_replay_rechecks_immutable_control_authority(tmp_path,monkeypatch,fault):
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        pending(j,monkeypatch,'undo-invalid')
        result=undo(j,monkeypatch,'undo-invalid-control')
        turn=result['turn']
        with j['db'].connect() as c:
            if fault=='voice':
                c.execute("UPDATE voice_input SET transcript='not the same control' WHERE voice_input_id=?",(turn['action']['voice_input_id'],))
            elif fault=='event':
                c.execute("UPDATE agent_commitment SET payload_json=json_remove(payload_json,'$.practice.spoken_undo_events') WHERE commitment_id=?",
                    (turn['action']['document_revisit_commitment'],))
            elif fault=='journal':
                c.execute("UPDATE voice_control_intent_request SET decision_json=json_set(decision_json,'$.meaning','status') WHERE request_key=?",
                    (turn['action']['document_revisit_request'],))
            else:
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?",(turn['agent_turn_id'],))
            c.commit()
        calls=len(j['calls'])
        replay=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert replay.status_code in {409,422},replay.text
        assert len(j['calls'])==calls


def test_undo_restores_committed_event_after_dispatch_interruption(tmp_path,monkeypatch):
    from home_study_agent import document_revisit
    with opened(tmp_path,monkeypatch,'spoken_explanation') as j:
        pending(j,monkeypatch,'undo-recovery')
        turn=undo(j,monkeypatch,'undo-recovery-control',execute=False)
        original=document_revisit.execute
        def interrupted(db,turn_id):
            result=original(db,turn_id)
            assert result['result']['spoken_undo_event']['status']=='resolved'
            raise ValueError('synthetic-after-undo-commit')
        with monkeypatch.context() as scoped:
            scoped.setattr(document_revisit,'execute',interrupted)
            failed=j['client'].post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
        assert failed.status_code in {409,422}
        calls=len(j['calls'])
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            result=restarted.post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute')
            assert result.status_code==200,result.text
            assert result.json()['execution']['replay_audio_allowed'] is False
            assert len(result.json()['execution']['result']['spoken_conversation']['undo_events'])==1
        assert len(j['calls'])==calls
