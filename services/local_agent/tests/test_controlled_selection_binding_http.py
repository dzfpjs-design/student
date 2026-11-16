"""Real local host contract for a private exact selection; inert synthetic media."""
import json
from pathlib import Path

import pytest

from home_study_agent.database import Database
from test_app import client_for
from test_companion_direct_start_atomic_http import task
from test_controlled_start_atomic_http import CHILD, counts, setup
from test_controlled_query_selection_http import approve_second
from test_learning_runtime_http import NoCloud


def offered(client, sid, kind):
    result = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start',
                        json={'query':'Synthetic','client_action_id':'needs-private-selection'})
    assert result.status_code == 409, result.text
    packet = result.json()
    assert packet['code'] == ('CONTROLLED_CONTENT_SELECTION_REQUIRED' if kind=='content' else 'CONTROLLED_GAME_SELECTION_REQUIRED')
    value = packet['details']['selection']
    assert value['session_id'] == sid and value['child_id'] == CHILD
    assert value['local_only'] is True and value['cloud_transmission_allowed'] is False
    assert all(set(c)=={'resource_id','title','source_version'} for c in value['choices'])
    return packet, next(c for c in value['choices'] if c['title']=='Synthetic story')


@pytest.mark.parametrize('kind',['content','games'])
def test_private_offer_exact_selection_and_restart_readback(tmp_path,kind):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid=setup(client,db,tmp_path,kind)
        approve_second(client,db,tmp_path,kind)
        packet,choice=offered(client,sid,kind)
        assert counts(db,pid,kind)==(0,0,0)
        binding={k:choice[k] for k in ('resource_id','source_version')}
        body={'query':choice['title'],'client_action_id':'new-exact-local-selection','selection_binding':binding}
        result=client.post(f'/v1/sessions/{sid}/controlled-{kind}/start',json=body)
        assert result.status_code==200,result.text
        assert result.json()['selection_binding']==binding
        assert str(result.json()['curriculum_row_id' if kind=='content' else 'approval_id'])==binding['resource_id']
        first=result.json()
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        replay=client.post(f'/v1/sessions/{sid}/controlled-{kind}/start',json=body)
        assert replay.status_code==200,replay.text
        identity='content_session_id' if kind=='content' else 'game_session_id'
        assert replay.json()[identity]==first[identity]
        assert replay.json()['selection_binding']==binding
        assert replay.json()['replayed'] is True and replay.json()['launch_allowed'] is False
        assert counts(db,pid,kind)==(1,1,1)
        if output:=__import__('os').environ.get('HSC_SELECTION_PACKET_OUTPUT'):
            root=Path(output);root.mkdir(parents=True,exist_ok=True)
            (root/(kind+'.json')).write_text(json.dumps({'offer':packet,'selected':first,'replay':replay.json()},ensure_ascii=False),encoding='utf-8')


@pytest.mark.parametrize('kind',['content','games'])
@pytest.mark.parametrize('change',['file','binding'])
def test_changed_exact_choice_never_pauses_or_selects_replacement(tmp_path,kind,change):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid=setup(client,db,tmp_path,kind);approve_second(client,db,tmp_path,kind)
        _,choice=offered(client,sid,kind)
        binding={k:choice[k] for k in ('resource_id','source_version')}
        if change=='file':
            path=tmp_path/('videos/second.mp4' if kind=='content' else 'SecondSynthetic.exe')
            path.write_bytes(b'replaced inert approved file, never launched')
        else: binding['resource_id']='different-resource'
        before=task(db,pid)
        result=client.post(f'/v1/sessions/{sid}/controlled-{kind}/start',json={
            'query':choice['title'],'client_action_id':'must-not-replace-original-choice','selection_binding':binding})
        assert result.status_code==409,result.text
        assert task(db,pid)==before and counts(db,pid,kind)==(0,0,0)


@pytest.mark.parametrize('kind',['content','games'])
def test_one_unavailable_file_does_not_hide_valid_choices_or_auto_start(tmp_path,kind):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid=setup(client,db,tmp_path,kind)
        approve_second(client,db,tmp_path,kind)
        (tmp_path/('videos/second.mp4' if kind=='content' else 'SecondSynthetic.exe')).unlink()
        before=task(db,pid)
        response=client.post(f'/v1/sessions/{sid}/controlled-{kind}/start',json={
            'query':'Synthetic','client_action_id':'one-unavailable-selection'})
        assert response.status_code==409,response.text
        offer=response.json().get('details',{}).get('selection')
        assert offer is not None,response.text
        assert offer['unavailable_count']==1
        assert len(offer['choices'])==1
        assert offer['choices'][0]['title']!='Synthetic story'
        assert task(db,pid)==before and counts(db,pid,kind)==(0,0,0)


@pytest.mark.parametrize('kind',['content','games'])
def test_no_verifiable_file_returns_no_offer_and_never_pauses(tmp_path,kind):
    db=Database(tmp_path/'test.db')
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid=setup(client,db,tmp_path,kind)
        approve_second(client,db,tmp_path,kind)
        for path in tmp_path.glob('videos/*.mp4' if kind=='content' else '*.exe'):
            path.unlink()
        before=task(db,pid)
        response=client.post(f'/v1/sessions/{sid}/controlled-{kind}/start',json={
            'query':'Synthetic','client_action_id':'all-unavailable-selection'})
        assert response.status_code==409,response.text
        assert response.json()['code']=='CONTROLLED_SELECTION_SOURCE_UNAVAILABLE'
        assert 'selection' not in response.json().get('details',{})
        assert task(db,pid)==before and counts(db,pid,kind)==(0,0,0)


@pytest.mark.parametrize('length',[200,201])
def test_offered_video_title_matches_real_start_query_limit_without_truncation(tmp_path,length):
    db=Database(tmp_path/'test.db')
    title='Synthetic'+('甲'*(length-len('Synthetic')))
    with client_for(tmp_path,model_gateway=NoCloud(),agent_runtime_enabled=True) as client:
        sid,pid=setup(client,db,tmp_path,'content')
        approve_second(client,db,tmp_path,'content',title=title)
        path=f'/v1/sessions/{sid}/controlled-content/start'
        response=client.post(path,json={'query':'Synthetic','client_action_id':'title-limit-offer'})
        assert response.status_code==409,response.text
        offer=response.json()['details']['selection']
        assert all(len(c['title'])<=200 for c in offer['choices'])
        if length>200:
            assert offer['unselectable_title_count']==1
            assert '名称过长' in response.json()['message']
            assert all(c['title']!=title and c['title']!=title[:200] for c in offer['choices'])
            assert counts(db,pid,'content')==(0,0,0)
        else:
            choice=next(c for c in offer['choices'] if c['title']==title)
            result=client.post(path,json={'query':choice['title'],'client_action_id':'exact-200-character-title',
                'selection_binding':{k:choice[k] for k in ('resource_id','source_version')}})
            assert result.status_code==200,result.text
            assert result.json()['title']==title
            assert counts(db,pid,'content')==(1,1,1)
