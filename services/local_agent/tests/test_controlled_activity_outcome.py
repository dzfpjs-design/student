"""W24 controlled sources: atomic registration and no lifecycle-only recovery."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService, ActivityConflict
from home_study_agent.controlled_game import ControlledGameService, ControlledGameError
from home_study_agent.controlled_content import ControlledContentService, ControlledContentError
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_controlled_game import seed as game_seed
from test_controlled_content import seed as content_seed


@pytest.fixture(params=['game','content'])
def controlled(request, tmp_path):
    kind=request.param
    db=Database(tmp_path/'controlled.db'); db.initialize()
    if kind=='game':
        session,item=game_seed(db,tmp_path/'synthetic.exe'); child='child-game'
        driver=ControlledGameService(db)
        driver.update_rule(child,enabled=True,max_daily_minutes=10,max_item_minutes=5)
        driver.approve(child,'Synthetic',str(tmp_path/'synthetic.exe'),5)
        query='Synthetic'
    else:
        session,resource=content_seed(db,tmp_path/'videos'); child='child-media'; item='item-media'
        driver=ControlledContentService(db)
        driver.update_rule(child,enabled=True,max_daily_minutes=10,max_item_minutes=5)
        driver.approve(child,resource,5); query='分数'
        with db.connect() as c:
            c.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id=?",(item,)); c.commit()
    return db,driver,kind,child,session,item,query


def test_start_registers_once_and_concurrent_replay_is_scoped(controlled):
    db,driver,kind,child,session,item,query=controlled
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:driver.start(session,query,'same-start',item),range(2)))
    key=kind+'_session_id'
    assert results[0][key]==results[1][key]
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome_event').fetchone()[0]==1
    with pytest.raises((ControlledGameError,ControlledContentError,ActivityConflict)):
        driver.start('another-child-session',query,'same-start',item)
    with pytest.raises((ControlledGameError,ControlledContentError,ActivityConflict)):
        driver.start(session,query+' changed','same-start',item)
    with pytest.raises((ControlledGameError,ControlledContentError,ActivityConflict)):
        driver.start(session,query,'same-start',None)


def test_registration_failure_rolls_back_launch_and_audit_and_allows_start_compensation(controlled, monkeypatch):
    db,driver,kind,child,session,item,query=controlled
    original=CompanionActivityOutcomeService.open
    def fail_after_registration(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('isolated registration failure')
    monkeypatch.setattr(CompanionActivityOutcomeService,'open',fail_after_registration)
    with pytest.raises(RuntimeError,match='registration failure'):
        driver.start(session,query,'failing-start',item)
    with db.connect() as c:
        assert c.execute(f'SELECT COUNT(*) FROM controlled_{kind}_session').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action=?",('controlled_'+kind+'_started',)).fetchone()[0]==0
    # Existing host compensation remains possible, but is not an activity result.
    StudyRepository(db).resume_plan_item(item,'start-failure:rollback-resume')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM companion_activity_outcome_event').fetchone()[0]==0


@pytest.mark.parametrize('ending',['natural','timeout','failure','restart'])
def test_end_and_recovery_leave_original_paused_without_return_evidence(controlled,ending):
    db,driver,kind,child,session,item,query=controlled
    started = driver.start(session,query,'start',item)
    if ending=='restart':
        driver=type(driver)(Database(db.path))
        assert driver.recover_interrupted()==1
        assert driver.recover_interrupted()==0
    else:
        reason={'natural':'process_exited' if kind=='game' else 'media_ended','timeout':'time_limit','failure':'launch_failed'}[ending]
        identity = {kind + '_session_id': started[kind + '_session_id']}
        ended = driver.end(session,reason,10,**identity)
        replay = driver.end(session,reason,10,**identity)
        assert replay['replayed'] is True
        assert replay['ended_at'] == ended['ended_at']
        assert replay['actual_seconds'] == ended['actual_seconds']
    with db.connect() as c:
        timer=c.execute('SELECT status,active_segment_started_at FROM plan_item WHERE plan_item_id=?',(item,)).fetchone()
        assert tuple(timer)==('paused',None)
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0]==0
    state=CompanionActivityOutcomeService(Database(db.path)).list(child)['items'][0]
    assert state['execution_status']=='unknown'
    assert state['resume']['status']=='pending'
    assert not state['habit_evidence_eligible']
