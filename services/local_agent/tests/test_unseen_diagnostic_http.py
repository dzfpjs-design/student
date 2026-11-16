"""Independent requests use licensed reviewed original sources, no model grader."""
import pytest
from test_curriculum_first_source_http import first_source_fixture, app_client, auth, publish, BASE, CHILD


def ready(tmp_path,*,with_spoken_tuple=False):
    db,source,spec,_=first_source_fixture(tmp_path,with_parallel_original=True,with_spoken_tuple=with_spoken_tuple)
    client=app_client(tmp_path,db);client.__enter__();auth(client)
    first=publish(client,spec)
    with db.connect() as c:
        second=c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? AND excerpt_id!=?',(source['source_id'],spec['excerpt_id'])).fetchone()[0]
    seeded={k:v for k,v in spec.items() if k!='knowledge_node_id'}
    seeded.update(seed_item_id=first['diagnostic_item_id'],excerpt_id=second,stage='parallel',validator='curriculum_objective_exact_source_v1',explanation='核对第二份合成原题：根为3和4。')
    r=client.post(BASE+'/generate',json=dict(action_id='fresh-second-d',expected_version=0,expected_scope_version=1,source=seeded));assert r.status_code==200,r.text
    cid=r.json()['candidate_id']
    for version,kind in enumerate(('validate','review','publish'),1):
        body=dict(action_id='fresh-second-'+kind,expected_version=version)
        if kind=='review':body.update(decision='approve',note='核对第二份来源原题与完整答案，只审核平行练习。')
        r=client.post(BASE+f'/{cid}/{kind}',json=body);assert r.status_code==200,r.text
    second=r.json();assert second['eligible']
    sid=client.post('/v1/sessions',json=dict(child_id=CHILD,client_action_id='fresh-session')).json()['session_id']
    return client,db,sid,first,second


def test_source_backed_unseen_question_avoids_original_and_replays_across_restart(tmp_path):
    client,db,sid,first,second=ready(tmp_path);path=f'/v1/sessions/{sid}/diagnostic-prompt'
    try:
        shown=client.post(path+'/prepare',json=dict(track='school',preferred_item_id=first['diagnostic_item_id'],client_action_id='show-first')).json()['prompt']
        cancelled=client.post(path+'/cancel',json=dict(prompt_id=shown['prompt_id'],item_version=shown['item_version']));assert cancelled.status_code==200,cancelled.text
        body=dict(track='school',require_unexposed=True,client_action_id='fresh-independent')
        r=client.post(path+'/prepare',json=body);assert r.status_code==200,r.text
        value=r.json();assert value['status']=='waiting_answer',value
        p=value['prompt'];assert p['diagnostic_item_id']==second['diagnostic_item_id']
        assert p['item']['prompt']!=shown['item']['prompt']
        assert client.post(path+'/prepare',json=body).json()['prompt']['prompt_id']==p['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM mastery_update_event').fetchone()[0]==0
        client.__exit__(None,None,None);client=None
        with app_client(tmp_path,db) as restart:
            assert restart.post(path+'/prepare',json=body).json()['prompt']['prompt_id']==p['prompt_id']
            r=restart.post(path+'/answer',json=dict(prompt_id=p['prompt_id'],item_version=p['item_version'],answer='3,4',hint_level=0,independent=True,client_action_id='new-exact-answer'))
            assert r.status_code==200,r.text
            assert r.json()['attempt']['score_status']=='verified_local'
            assert r.json()['attempt']['correctness']==1
    finally:
        if client:client.__exit__(None,None,None)


@pytest.mark.parametrize('scope',[{'knowledge_node_id':'other-learning-node'}, {'subject':'物理'}])
def test_unseen_preferred_source_cannot_escape_requested_learning_scope(tmp_path,scope):
    client,db,sid,first,second=ready(tmp_path)
    try:
        r=client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json=dict(track='school',require_unexposed=True,
            preferred_item_id=second['diagnostic_item_id'],client_action_id='wrong-unseen-scope',**scope))
        assert r.status_code==200,r.text
        assert r.json()['status']=='no_item' and r.json()['prompt'] is None,r.json()
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)


def test_reviewed_numeric_tuple_ambiguous_speech_is_durable_ungraded_evidence(tmp_path):
    client,db,sid,_,second=ready(tmp_path,with_spoken_tuple=True)
    try:
        base=f'/v1/sessions/{sid}/diagnostic-prompt'
        r=client.post(base+'/prepare',json=dict(track='school',require_unexposed=True,preferred_item_id=second['diagnostic_item_id'],client_action_id='ambiguous-tuple-prompt'))
        assert r.status_code==200,r.text
        prompt=r.json()['prompt']
        body=dict(prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer='三四',independent=True,hint_level=0,client_action_id='ambiguous-tuple-answer')
        r=client.post(base+'/answer',json=body)
        assert r.status_code==200,r.text
        attempt=r.json()['attempt']
        assert attempt['correctness'] is None and attempt['score_status']=='needs_review'
        assert attempt['mastery_update_allowed'] is False
        assert client.post(base+'/answer',json=body).json()['attempt']['attempt_id']==attempt['attempt_id']
        with db.connect() as c:
            stored=c.execute('SELECT submitted_answer,correctness FROM diagnostic_attempt WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()
            assert tuple(stored)==('三四',None)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
    finally:client.__exit__(None,None,None)


@pytest.mark.parametrize('mutation',['seen_alias','damaged_history','withdrawn_source'])
def test_unseen_request_fails_closed_without_new_prompt_or_attempt(tmp_path,mutation):
    client,db,sid,first,second=ready(tmp_path);path=f'/v1/sessions/{sid}/diagnostic-prompt'
    try:
        for index,item in enumerate((first,second)):
            shown=client.post(path+'/prepare',json=dict(track='school',preferred_item_id=item['diagnostic_item_id'],client_action_id=f'shown-source-{index}')).json()['prompt']
            assert client.post(path+'/cancel',json=dict(prompt_id=shown['prompt_id'],item_version=shown['item_version'])).status_code==200
        if mutation=='damaged_history':
            with db.connect() as c:c.execute("UPDATE diagnostic_prompt SET item_version='damaged' WHERE prompt_id=?",(shown['prompt_id'],));c.commit()
        if mutation=='withdrawn_source':
            r=client.post(BASE+f"/{first['candidate_id']}/withdraw",json=dict(action_id='fresh-withdraw',expected_version=4,note='来源撤回'));assert r.status_code==200,r.text
        r=client.post(path+'/prepare',json=dict(track='school',require_unexposed=True,client_action_id='should-not-reuse'))
        assert r.status_code==200,r.text
        assert r.json()['status']=='no_item' and r.json()['prompt'] is None,r.json()
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]==2
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
    finally:client.__exit__(None,None,None)
