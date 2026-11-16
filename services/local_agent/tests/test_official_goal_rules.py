"""Public rule snapshots bind plans, never certify personal exam registration."""
from copy import deepcopy
from datetime import date, timedelta
import json

from home_study_agent import exam_blueprints
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.official_goal_rules import target_rule_context
from test_goal_roadmap import flow, configure, CHILD, OTHER


def test_pet_config_preserves_rule_snapshot_across_restart_and_rule_change(flow, monkeypatch):
    db, programs, maps = flow
    target = (date.today() + timedelta(days=200)).isoformat()
    configure(programs, track='pet_path', target_date=target, action='pet-rules')
    first = maps.read(CHILD, 'pet_path')
    rules = first['exam_rules']
    assert rules['binding_status'] == 'current'
    assert rules['public_rule_status'] == 'verified_reference_snapshot'
    assert rules['registration_status'] == 'not_verified'
    assert rules['target_date_basis'] == 'family_target_not_registration'
    assert not rules['exam_readiness_claim_allowed']
    assert {r['section_id'] for r in rules['required_sections']} == {'reading','writing','listening','speaking'}
    assert rules['coverage_status'] == 'not_audited_against_official_requirements'
    assert GoalRoadmapService(db).read(CHILD, 'pet_path')['exam_rules'] == rules
    with db.connect() as c:
        frozen = json.loads(c.execute("SELECT target_horizon_json FROM goal_program WHERE child_id=? AND track='pet_path'", (CHILD,)).fetchone()[0])['rule_binding']
    changed = deepcopy(exam_blueprints.EXAM_BLUEPRINTS)
    key = 'cambridge-b1-preliminary-for-schools-current-v2'
    changed[key]['sections'][0]['item_target'] = 33
    monkeypatch.setattr(exam_blueprints, 'EXAM_BLUEPRINTS', changed)
    revised = maps.read(CHILD, 'pet_path')
    assert revised['exam_rules']['binding_status'] == 'source_changed'
    assert revised['source_version'] != first['source_version']
    assert revised['exam_rules']['bound_snapshot'] == frozen
    assert 'exam_rules_changed' in {r['code'] for r in revised['risks']}
    configure(programs, track='pet_path', weekly_minutes=70, action='budget-not-rule-consent')
    assert maps.read(CHILD, 'pet_path')['exam_rules']['binding_status'] == 'source_changed'


def test_future_nanjing_year_and_competition_url_are_not_verified_rules(flow):
    _, programs, maps = flow
    future = date(max(date.today().year + 1, 2027), 6, 20).isoformat()
    configure(programs, child=OTHER, track='junior_exam_readiness', target_date=future, action='future-exam')
    result = maps.read(OTHER, 'junior_exam_readiness')
    rules = result['exam_rules']
    assert rules['public_rule_status'] == 'target_year_unverified'
    assert rules['reference_year'] == 2026 and rules['target_year'] != 2026
    assert rules['required_sections'] == []
    assert rules['registration_status'] == 'not_verified'
    assert 'exam_rules_unverified' in {r['code'] for r in result['risks']}
    configure(programs, target_date=future, event_name='Synthetic contest',
        event_rules_url='https://example.invalid/rules', event_version='caller-says-official', action='contest-rules')
    competition = maps.read(CHILD, 'competition_math_path')['exam_rules']
    assert competition['public_rule_status'] == 'event_rules_unverified'
    assert not competition['exam_readiness_claim_allowed']
    assert competition['required_sections'] == []


def test_historical_projection_does_not_backdate_later_verification():
    before = target_rule_context('pet_path', {'target_date': '2026-10-01'}, as_of_date=date(2026, 9, 1))
    assert before['public_rule_status'] == 'not_verified_at_reference_date'
    assert before['required_sections'] == []


def test_local_policy_is_not_reused_for_another_curriculum_region():
    known = target_rule_context('junior_exam_readiness', {'target_date': '2026-06-20'},
                               as_of_date=date(2026,9,8), curriculum_region='江苏南京')
    assert known['public_rule_status'] == 'verified_reference_snapshot'
    other = target_rule_context('junior_exam_readiness', {'target_date': '2026-06-20'},
                               as_of_date=date(2026,9,8), curriculum_region='上海')
    assert other['public_rule_status'] == 'target_region_unverified'
    assert other['required_sections'] == []
