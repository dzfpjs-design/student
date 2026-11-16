import json

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, build_planner_messages
from test_tutor_playback import begin, flow


def test_planner_has_playback_position_without_local_explanation_or_binding(flow):
    db, _, sid, _, board = flow
    view = begin(flow)
    context = AgentRuntimeStore(db).assemble_context(sid, ())
    assert context['tutor_playback'] == dict(status='paused', segment_index=0, segment_started=0, segment_count=2)
    messages = build_planner_messages(context, '继续刚才那一段', CapabilityRegistry())
    sent = json.loads(messages[1]['content'])['context']['tutor_playback']
    assert sent == context['tutor_playback']
    assert view['playback_id'] not in json.dumps(sent)
    assert board['segments'][0]['narration'] not in json.dumps(sent)
