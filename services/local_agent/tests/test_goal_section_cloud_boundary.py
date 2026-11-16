"""Exercise the existing planner sanitizer with a real local section projection."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.family_clock import family_today
from home_study_agent.goal_section_coverage import build_section_evidence
from home_study_agent.official_goal_rules import target_rule_context
from test_agent_reliable_feedback_context import runtime, CHILD, PlannerGateway, planner_payload
from voice_dispatch_helpers import capture_dispatch_guard


def test_nested_actual_section_projection_is_not_sent_to_general_planner(runtime, monkeypatch):
    db, repository, _, sid, _ = runtime
    now = datetime.now(timezone.utc)
    rules = target_rule_context('pet_path', {'target_date':'2026-12-01'},
        as_of_date=family_today(now), curriculum_region='南京')
    with db.connect() as c:
        c.execute('BEGIN')
        projection = build_section_evidence(c, CHILD, rules, as_of=now)
    assert any(section['sources'] for section in projection['sections'])
    gateway = PlannerGateway(planner_payload('none', 'execution', 'execute', readonly=True))
    orchestrator = AgentOrchestrator(db, repository, gateway,
        SimpleNamespace(model_provider='test', model_name='test'))
    assemble = orchestrator.agent_runtime.assemble_context
    def with_parent_projection(*args, **kwargs):
        context = assemble(*args, **kwargs)
        context['nested_parent_view'] = {'section_evidence': projection}
        return context
    monkeypatch.setattr(orchestrator.agent_runtime, 'assemble_context', with_parent_projection)
    result = asyncio.run(orchestrator._resolve_agent_plan(session_id=sid, child_id=CHILD,
        transcript='接下来呢', known_child_names=[], recent_dialogue=(),
        pre_dispatch=capture_dispatch_guard(orchestrator, sid, CHILD, '接下来呢')))
    assert result is not None and len(gateway.requests) == 1
    # The gateway is controlled, no physical model/network call. We exercise
    # actual message assembly, not a hand-written copy of its sanitizer.
    transmitted = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
    assert projection['context_version'] not in transmitted
    assert projection['version'] not in transmitted
    assert 'qualified_evidence_count' not in transmitted
    for section in projection['sections']:
        for source in section['sources']:
            assert source['source_version'] not in transmitted
    # build_planner_messages applies a second local-only filter: even the
    # redacted placeholder is omitted, not just its source payload.
    assert json.loads(gateway.requests[0].messages[-1]['content'])['context']['nested_parent_view'] == {}
