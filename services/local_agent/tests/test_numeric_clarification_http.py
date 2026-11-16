"""Uncertain original -> bound clarification -> new original, without rewriting."""
from contextlib import contextmanager
import json
import pytest
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, start, endpoint, binding
from test_specialized_closed_http import generated_http
from test_spoken_numeric import numeric_generated
from test_numeric_tcp_audit30 import TCPClient
import test_specialized_closed_contract as closed


@contextmanager
def unclear(environment, monkeypatch, transport=TestClient):
    settings, db = environment
    monkeypatch.setattr(closed, 'generated', numeric_generated)
    gateway = closed.ClosedGateway()
    with transport(create_app(settings, model_gateway=gateway)) as client:
        headers, candidates = generated_http(client, db)
        candidate = next(row for row in candidates if row['stage'] == 'parallel')
        response = client.post('/v1/parent/specialized-supply/candidates/' + candidate['candidate_id'] + '/review',
            headers=headers, json={'decision':'approve','note':'Synthetic scalar original', 'client_action_id':'clarify-review'})
        assert response.status_code == 200, response.text
        sid = start(client)
        prompt = client.post(endpoint(sid, 'prepare'), json={'track':'competition_math',
            'preferred_item_id':response.json()['diagnostic_item_id'], 'client_action_id':'clarify-prepare'}).json()['prompt']
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript':'答案是两三',
            'client_action_id':'unclear-original', 'cloud_text_allowed':True}).json()
        assert turn['action']['type'] == 'submit_diagnostic_answer', turn
        response = client.post(endpoint(sid, 'answer'), json={**binding(prompt), 'answer':turn['action']['answer'],
            'authorized_turn_id':turn['agent_turn_id'], 'client_action_id':'unclear-answer'})
        assert response.status_code == 200, response.text
        first = response.json()
        assert first['attempt']['correctness'] is None and not first['attempt']['mastery_update_allowed']
        assert '只说一个数' in first['attempt']['feedback']
        assert first['numeric_clarification']['parent_attempt_id'] == first['attempt']['attempt_id']
        request = dict(track='competition_math', preferred_item_id=prompt['diagnostic_item_id'],
            client_action_id='numeric-clarification:' + first['attempt']['attempt_id'],
            numeric_clarification=first['numeric_clarification'])
        yield client, sid, first, request, gateway


@pytest.mark.parametrize('transport', [TestClient, TCPClient])
def test_new_clarified_original_survives_restart_and_keeps_first_original(environment, monkeypatch, transport):
    settings, db = environment
    with unclear(environment, monkeypatch, transport) as (client, sid, first, request, gateway):
        response = client.post(endpoint(sid,'prepare'), json=request)
        assert response.status_code == 200, response.text
        successor = response.json()['prompt']
        assert successor['prompt_id'] != first['prompt']['prompt_id']
        assert successor['item_version'] == first['prompt']['item_version']
        with db.connect() as c:
            original = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (first['attempt']['attempt_id'],)).fetchone())
    with transport(create_app(settings, model_gateway=gateway)) as client:
        replay = client.post(endpoint(sid,'prepare'), json=request)
        assert replay.status_code == 200 and replay.json()['prompt']['prompt_id'] == successor['prompt_id'], replay.text
        assert replay.json()['status'] == 'waiting_answer'
        turn = client.post(f'/v1/sessions/{sid}/agent-turn', json={'transcript':'答案是六',
            'client_action_id':'clear-original', 'cloud_text_allowed':True}).json()
        assert turn['action']['type'] == 'submit_diagnostic_answer', turn
        assert turn['action']['diagnostic_prompt_id'] == successor['prompt_id']
        response = client.post(endpoint(sid,'answer'), json={**binding(successor), 'answer':turn['action']['answer'],
            'authorized_turn_id':turn['agent_turn_id'], 'client_action_id':'clear-answer'})
        assert response.status_code == 200, response.text
        second = response.json()
        assert second['attempt']['correctness'] == 1, second
        # Clarifying the same question is practice, not an independent retest.
        assert second['attempt']['score_status'] == 'practice_only', second['attempt']
        assert not second['attempt']['mastery_update_allowed']
        assert not second.get('numeric_clarification')
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (original['attempt_id'],)).fetchone()) == original
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?',(sid,)).fetchone()[0] == 2
            chain = json.loads(c.execute('SELECT request_json FROM diagnostic_prompt WHERE prompt_id=?',(successor['prompt_id'],)).fetchone()[0])
            assert chain['numeric_clarification']['parent_attempt_id'] == original['attempt_id']
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('fault',['attempt','version','item','other_focus','withdrawn','answer_changed'])
def test_stale_or_wrong_clarification_never_changes_another_question(environment, monkeypatch, fault):
    settings, db = environment
    with unclear(environment, monkeypatch) as (client,sid,first,request,_):
        if fault == 'attempt': request['numeric_clarification']['parent_attempt_id']='wrong-attempt'
        elif fault == 'version': request['numeric_clarification']['parent_item_version']='wrong-version'
        elif fault == 'item': request['preferred_item_id']='diag-p4-arithmetic-closed-1'
        elif fault == 'other_focus':
            response=client.post(endpoint(sid,'prepare'),json={'track':'school','preferred_item_id':'diag-p4-arithmetic-closed-1','client_action_id':'another-question'})
            assert response.status_code == 200,response.text
        else:
            with db.connect() as c:
                if fault == 'withdrawn':
                    c.execute("UPDATE diagnostic_prompt SET status='invalidated' WHERE prompt_id=?",(first['prompt']['prompt_id'],))
                else:
                    c.execute("UPDATE diagnostic_attempt SET submitted_answer='different original' WHERE attempt_id=?",(first['attempt']['attempt_id'],))
                c.commit()
        with db.connect() as c:
            count=c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0]
        response=client.post(endpoint(sid,'prepare'),json=request)
        assert response.status_code == 409,response.text
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == count


def test_parent_withdrawn_after_preparation_blocks_pending_clarification(environment,monkeypatch):
    _,db=environment
    with unclear(environment,monkeypatch) as (client,sid,first,request,_):
        response=client.post(endpoint(sid,'prepare'),json=request)
        assert response.status_code == 200,response.text
        successor=response.json()['prompt']
        with db.connect() as c:
            c.execute("UPDATE diagnostic_prompt SET status='invalidated' WHERE prompt_id=?",(first['prompt']['prompt_id'],))
            c.commit()
        current=client.get(endpoint(sid),params={'prompt_id':successor['prompt_id']})
        assert current.status_code == 200,current.text
        assert current.json()['status']=='invalidated'
