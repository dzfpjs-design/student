"""Actual CPU embedding in the voice/SQLite journey; ASR/OCR/cloud responses controlled."""
from dataclasses import replace
import json
from functools import partial
import socket

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_check_supply import deliver
from test_diagnostic_prompt_http import environment, start, CHILD, OTHER
from test_local_semantic import installed_models
import test_document_goal_http as original
import test_document_revisit as revisit
import test_voice_runtime_binding as voice_helpers
from test_voice_runtime_binding import capture, interpret, submit


@pytest.mark.parametrize('child,body,node',[
    (CHILD,'先求24与6的和，然后将所得结果乘3。','node-p4-math-arithmetic'),
    (OTHER,'不解这个二次方程，怎么判断它有几个不同的实数根？','node-j3-math-discriminant'),
])
def test_real_local_model_proposes_then_child_probes_without_cloud_source_upload(environment,installed_models,monkeypatch,child,body,node):
    settings, db = environment
    settings = replace(settings, local_model_dir=installed_models)
    monkeypatch.setattr(original,'BODY',body)
    monkeypatch.setattr(original,'start',partial(start,child=child))
    monkeypatch.setattr(revisit,'start',partial(start,child=child))
    monkeypatch.setattr(voice_helpers,'CHILD',child)
    monkeypatch.setattr(socket.socket,'connect',lambda *a: pytest.fail('Test inference attempted network access'))
    generator=original.journey.__wrapped__((settings,db),monkeypatch)
    j=next(generator)
    try:
        if child==OTHER:
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert deliver(c,child,'diag-j3-discriminant-1','semantic-junior-source-1')
                assert deliver(c,child,'diag-j3-discriminant-1','semantic-junior-source-2')
                c.commit()
        revisit.next_day(j,monkeypatch)
        revisit.offer(j)
        revisit.say(j,monkeypatch,'先看看原题','start','semantic-original-start')
        issued=revisit.say(j,monkeypatch,'换一道类似的题考考我','independent_check','semantic-original-proposal')
        assert issued['execution']['result']['status']=='target_choice',issued
        cid=issued['execution']['result']['commitment_id']
        with db.connect() as c:
            proposal=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(cid,)).fetchone()[0])['independent_target_hypothesis']
            assert proposal['retrieval']['status']=='ready'
            assert proposal['retrieval']['source_policy']=='all_qualified_variants_v1'
            assert proposal['retrieval']['source_count']>=len(proposal['options'])
            assert proposal['options'][0]['node_id']==node,proposal
            assert proposal['options'][0]['semantic_similarity']>=.60
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
        previous_client=j['client']
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            j['client']=restarted
            selected=revisit.say(j,monkeypatch,'选第一项','select_1','semantic-original-choice')
        j['client']=previous_client
        prompt=selected['execution']['result']['diagnostic_prompt']['prompt']
        with db.connect() as c:
            expected=json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                (prompt['diagnostic_item_id'],)).fetchone()[0])['exact_answers'][0]
        assert prompt['item']['knowledge_node_id']==node
        voice=capture(j['client'],j['settings'],monkeypatch,j['sid'],'答案是'+expected,prompt,'semantic-answer-voice')
        turn=interpret(j['client'],j['sid'],'答案是'+expected,'semantic-answer',voice)
        result=submit(j['client'],j['sid'],prompt,turn,expected)
        assert result.status_code==200,result.text
        assert result.json()['attempt']['correctness']==1
        with TestClient(create_app(j['settings'],model_gateway=j['gateway'])) as restarted:
            assert restarted.post('/v1/agent-turns/'+selected['turn']['agent_turn_id']+'/execute').status_code==200
        with db.connect() as c:
            rows=c.execute('SELECT child_id FROM learning_evidence').fetchall()
            assert [r[0] for r in rows]==[child]
            payload=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(cid,)).fetchone()[0])
            assert payload['practice']['phase']=='check_feedback'
            assert payload['independent_target_hypothesis']['alignment_verified'] is False
            assert not payload.get('independent_mapping')
        assert body not in json.dumps(j['calls'][-3:],ensure_ascii=False)
    finally:
        generator.close()
