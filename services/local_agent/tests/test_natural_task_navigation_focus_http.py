"""Existing blackboard/listening/mistake owners are not implicit task resumes."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_content_free_voice_control_http import task_fixture
from test_natural_task_navigation_http import Offline, hear, execute
from test_tutor_playback import flow
from test_tutor_playback_http import begin as begin_tutor
from test_pet_media_evidence import media, begin, review
from test_mistake_voice_journey import fixture as mistake_case, begin as begin_mistake, say, focus


def paused_schedule(db,sid):
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.commit()
    repo=task_fixture(db,sid)
    repo.pause_plan_item('control-task-1','paused-under-current-interaction')
    return repo


def unchanged(db,repo,heard,client):
    assert heard.get('runtime_capability') not in {'task.start','task.resume'},heard
    if heard.get('runtime_capability')=='conversation.respond':
        execute(client,heard)
    assert repo._get_plan_item_by_id('control-task-1')['status']=='paused'
    assert repo._get_plan_item_by_id('control-task-2')['status']=='pending'
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0


@pytest.mark.parametrize('quote',['那我们继续吧','开始下一项'])
def test_blackboard_owner_does_not_become_a_timer_resume(flow,monkeypatch,quote):
    db,_,sid,turn,_=flow
    settings=Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        repo=paused_schedule(db,sid)
        original=begin_tutor(client,sid,turn)
        heard,_=hear(client,settings,monkeypatch,sid,quote)
        unchanged(db,repo,heard,client)
        current=client.get(f'/v1/sessions/{sid}/tutor-playback').json()['playback']
        assert current['playback_id']==original['playback_id']
        assert current['segment_index']==original['segment_index']


@pytest.mark.parametrize('quote',['那我们继续吧','开始下一项'])
def test_listening_owner_does_not_become_a_timer_resume(media,monkeypatch,quote):
    db,_,sid,prompt,_,_=media
    review(media,begin(media))
    settings=Settings(data_dir=db.path.parent,database_path=db.path,maintenance_enabled=False)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        repo=paused_schedule(db,sid)
        heard,_=hear(client,settings,monkeypatch,sid,quote,prompt=prompt)
        unchanged(db,repo,heard,client)
        current=client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()['prompt']
        assert current['prompt_id']==prompt['prompt_id']


@pytest.mark.parametrize('quote',['那我们继续吧','开始下一项'])
def test_mistake_pending_owner_does_not_become_a_timer_resume(tmp_path,monkeypatch,quote):
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'mistake.db',maintenance_enabled=False)
    with TestClient(create_app(settings,model_gateway=Offline())) as client:
        sid,db=mistake_case(client)
        begin_mistake(client,sid)
        say(client,sid,'第四题','choose-original')
        say(client,sid,'对','confirm-original')
        say(client,sid,'我记不清了','forgot-original')
        original=focus(client,sid)
        assert original['phase']=='answer'
        repo=paused_schedule(db,sid)
        ref={'kind':'mistake','binding':original['answer_binding']} if original.get('answer_binding') else None
        heard,_=hear(client,settings,monkeypatch,sid,quote,prompt=ref)
        unchanged(db,repo,heard,client)
        current=focus(client,sid)
        assert current['candidate_id']==original['candidate_id']
        assert current['phase']==original['phase']
