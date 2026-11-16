"""Private content is not permission to discard requested remaining work."""
import asyncio
import json
import pytest
from home_study_agent.agent_continuation import ContinuationStore
from home_study_agent.database import Database
from test_agent_continuation import setup,complete,orchestrator_for,next_plan


@pytest.mark.parametrize('flags',[{'local_only':True},{'cloud_transmission_allowed':False},
    {'local_only':True,'cloud_transmission_allowed':False}])
def test_generic_private_replan_is_durable_blocked_not_model_authorization(tmp_path,flags):
    db,sid,runtime=setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id='root'",(json.dumps(flags),))
        c.commit()
    complete(db,runtime)
    complete(db,runtime)
    queue=ContinuationStore(Database(db.path))
    expected={'status':'blocked','reason':'LOCAL_CONTINUATION_AUTHORIZATION_REQUIRED'}
    assert queue.claim(sid,'root')==expected
    assert queue.snapshot(sid)=={**expected,'parent_turn_id':'root'}
    orchestrator,gateway=orchestrator_for(db,tmp_path,next_plan())
    assert asyncio.run(orchestrator.continue_execution(sid,'root',True))==expected
    assert gateway.requests==[]
    assert queue.claim('other-session','root')=={'status':'none'}
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_continuation').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM agent_turn').fetchone()[0]==1
        assert json.loads(c.execute("SELECT plan_json FROM agent_plan WHERE agent_turn_id='root'").fetchone()[0])['continuation_scope']


@pytest.mark.parametrize('change', ['remove_private_flags', 'cancel_objective', 'clear_scope'])
def test_saved_local_block_cannot_be_replayed_as_new_authorization(tmp_path, change):
    db, sid, runtime = setup(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE agent_turn SET action_json=? WHERE agent_turn_id='root'",
            (json.dumps({'local_only': True, 'cloud_transmission_allowed': False}),))
        c.commit()
    complete(db, runtime)
    with db.connect() as c:
        if change == 'remove_private_flags':
            c.execute("UPDATE agent_turn SET action_json='{}' WHERE agent_turn_id='root'")
        elif change == 'cancel_objective':
            c.execute("UPDATE agent_objective SET status='cancelled'")
        else:
            c.execute("UPDATE agent_plan SET plan_json=json_set(plan_json,'$.continuation_scope',json('[]'))")
        c.commit()
    complete(db, runtime)
    orchestrator, gateway = orchestrator_for(db, tmp_path, next_plan())
    for _ in range(2):
        assert asyncio.run(orchestrator.continue_execution(sid, 'root', True)) == {
            'status': 'blocked', 'reason': 'LOCAL_CONTINUATION_AUTHORIZATION_REQUIRED'}
    assert gateway.requests == []
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM agent_continuation').fetchone())
        assert row['next_turn_id'] is None and row['lease_token'] is None
        assert c.execute('SELECT count(*) FROM agent_turn').fetchone()[0] == 1
