"""Parent acknowledgement changes planning consent, never public rule authority."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent import exam_blueprints
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.family_clock import family_today
from home_study_agent.goal_program import GoalProgramService
from test_goal_program import goal_db
from test_goal_roadmap import configure

CHILD = 'child-primary-4'
TRACK = 'pet_path'
PATH = f'/v1/parent/children/{CHILD}/goal-programs/{TRACK}/rule-review'


def changed_rules(monkeypatch):
    rules = deepcopy(exam_blueprints.EXAM_BLUEPRINTS)
    rules['cambridge-b1-preliminary-for-schools-current-v2']['sections'][0]['item_target'] += 1
    monkeypatch.setattr(exam_blueprints, 'EXAM_BLUEPRINTS', rules)


def request_body(review, key='confirm-rule-request'):
    snapshot = review['rules']['current_snapshot']
    return dict(expected_program_version=review['program_version'],
        expected_rule_version=snapshot['version'], expected_rule_hash=snapshot['snapshot_hash'],
        client_action_id=key)


def authorize(client, setup=True):
    response = client.post('/v1/parent/setup' if setup else '/v1/parent/unlock', json={'pin':'7391'})
    assert response.status_code == 200, response.text
    client.headers['X-HSC-Parent'] = response.json()['parent_token']


@pytest.fixture
def host(goal_db):
    service = GoalProgramService(goal_db)
    configure(service, child=CHILD, track=TRACK,
        target_date=(family_today()+timedelta(days=120)).isoformat(), action='initial-target')
    settings = Settings(data_dir=goal_db.path.parent, database_path=goal_db.path, maintenance_enabled=False)
    return goal_db, service, settings


def test_parent_current_rule_confirmation_persists_without_authority_upgrade(host, monkeypatch):
    db, service, settings = host
    original = service.roadmap(CHILD, TRACK)['exam_rules']['bound_snapshot']
    changed_rules(monkeypatch)
    with TestClient(create_app(settings)) as client:
        assert client.get(PATH).status_code in (401,403)
        authorize(client)
        read = client.get(PATH)
        assert read.status_code == 200, read.text
        assert read.headers['cache-control'] == 'no-store'
        review = read.json()
        assert review['rules']['binding_status'] == 'source_changed'
        body = request_body(review)
        before = client.get(f'/v1/parent/children/{CHILD}/goal-programs/{TRACK}/roadmap').json()
        saved = client.post(PATH, json=body)
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert result['review']['rules']['binding_status'] == 'current'
        assert result['receipt']['actor_type'] == 'parent'
        assert result['receipt']['old_rule_hash'] == original['snapshot_hash']
        assert not result['review']['rules']['exam_readiness_claim_allowed']
        route = client.get(f'/v1/parent/children/{CHILD}/goal-programs/{TRACK}/roadmap').json()
        assert before['source_version'] != route['source_version']
        assert not any(r['code']=='exam_rules_changed' for r in route['risks'])
        assert client.post(PATH, json={**body,'actor_type':'official'}).status_code == 422
        assert client.post(PATH.replace(CHILD,'child-junior-3'), json=body).status_code == 404
        assert client.post(PATH, json=body).json()['receipt'] == result['receipt']
    with TestClient(create_app(settings)) as client:
        authorize(client, False)
        assert client.post(PATH, json=body).json()['receipt'] == result['receipt']
        changed_rules(monkeypatch)
        replay = client.post(PATH, json=body).json()
        assert replay['review']['rules']['binding_status'] == 'source_changed'
        assert replay['receipt'] == result['receipt']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM goal_program_revision WHERE client_action_id=?', (body['client_action_id'],)).fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('race', ['rule', 'budget', 'date', 'unknown_snapshot'])
def test_stale_parent_review_cannot_bind_a_new_current_state(host, monkeypatch, race):
    db, service, settings = host
    with TestClient(create_app(settings)) as client:
        authorize(client)
        read = client.get(PATH)
        assert read.status_code == 200, read.text
        body = request_body(read.json())
        if race == 'rule':
            changed_rules(monkeypatch)
        elif race == 'unknown_snapshot':
            monkeypatch.setattr('home_study_agent.official_goal_rules.rule_snapshot', lambda track: None)
        else:
            configure(service, child=CHILD, track=TRACK, weekly_minutes=80,
                target_date=(family_today()+timedelta(days=121)).isoformat() if race=='date' else None,
                action='other-parent-edit')
        with db.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM goal_program')]
        response = client.post(PATH, json=body)
        assert response.status_code == 409, response.text
        assert response.json()['code'].startswith('GOAL_RULE_')
        with db.connect() as c:
            assert before == [tuple(r) for r in c.execute('SELECT * FROM goal_program')]
            assert c.execute('SELECT COUNT(*) FROM goal_program_revision WHERE client_action_id=?', (body['client_action_id'],)).fetchone()[0] == 0


def test_unverified_year_remains_unverified_after_explicit_acknowledgement(host):
    _, service, settings = host
    child, track = 'child-junior-3', 'junior_exam_readiness'
    configure(service, child=child, track=track, target_date='2030-06-01', action='future-rule-target')
    path = PATH.replace(CHILD,child).replace(TRACK,track)
    with TestClient(create_app(settings)) as client:
        authorize(client)
        read = client.get(path)
        assert read.status_code == 200, read.text
        assert read.json()['rules']['public_rule_status'] == 'target_year_unverified'
        saved = client.post(path, json=request_body(read.json())).json()
        assert saved['review']['rules']['binding_status'] == 'current'
        assert saved['review']['rules']['public_rule_status'] == 'target_year_unverified'
        assert not saved['review']['rules']['exam_readiness_claim_allowed']
        assert saved['review']['rules']['coverage_status'] == 'not_audited_against_official_requirements'
