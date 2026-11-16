from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.goal_program import GoalProgramService
from test_agent_continuation import Gateway
from test_goal_roadmap import configure
from test_pet_oral_supply_http import setup, post, CHILD


def test_parent_cycles_and_explicit_evidence_share_the_same_unchanged_route(tmp_path):
    db, settings = setup(tmp_path)
    configure(GoalProgramService(db), child=CHILD, track='pet_path', target_date='2026-12-01')
    gateway = Gateway({})
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway),
            headers={'X-HSC-Session': settings.session_token}, client=('127.0.0.1', 41952)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        root = f'/v1/parent/children/{CHILD}/goal-programs'
        response = client.get(root)
        assert response.status_code == 200, response.text[:300]
        embedded = next(item['roadmap'] for item in response.json()['items'] if item['track'] == 'pet_path')
        assert 'section_evidence' not in embedded
        # Independent HTTP reads use real current timestamps, not a frozen
        # shared response or renderer transport stub.
        for _ in range(2):
            response = client.get(root + '/pet_path/roadmap')
            assert response.status_code == 200, response.text[:300]
            current = response.json()
            for key in ('child_id', 'program_id', 'track', 'source_version'):
                assert current[key] == embedded[key]
            evidence = current['section_evidence']
            assert evidence['rule_snapshot_hash'] == embedded['exam_rules']['current_snapshot']['snapshot_hash']
            assert evidence['rule_binding_status'] == embedded['exam_rules']['binding_status']
            assert evidence['public_rule_status'] == embedded['exam_rules']['public_rule_status']
            assert evidence['cloud_transmission_allowed'] is False
