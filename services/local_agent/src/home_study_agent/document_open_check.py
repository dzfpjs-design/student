"""Bind reviewed open-task criteria before an original-review answer exists."""
from datetime import datetime, timezone
import json
from uuid import uuid4

from . import document_goal_handoff as goal
from .diagnostic_prompt import _version
from .document_independent_check import _qualified

KEY = 'document_open_check'


def bind_before_answer(c, turn, prompt):
    source = _qualified(c, turn['child_id'], prompt['diagnostic_item_id'])
    goal.require(source and source['answer_type'] == 'open_response'
        and _version(source) == prompt['item_version'], 'DOCUMENT_OPEN_SOURCE_CHANGED')
    plan = c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()
    goal.require(c.in_transaction and plan and plan['child_id'] == turn['child_id'] == prompt['child_id']
        and plan['session_id'] == turn['session_id'] == prompt['origin_session_id'], 'DOCUMENT_OPEN_OWNER_CHANGED')
    objective = c.execute("SELECT * FROM agent_objective WHERE objective_id=? AND child_id=?",
        (plan['objective_id'], turn['child_id'])).fetchone() if plan else None
    goal.require(objective and objective['status'] == 'active' and objective['objective_type'] == 'learning'
        and objective['last_agent_turn_id'] == turn['agent_turn_id'], 'DOCUMENT_OPEN_GOAL_CHANGED')
    points = json.loads(source['expected_answer_json'])['task_points']
    now = datetime.now(timezone.utc).isoformat()
    # The review goal may originate yesterday. Keep its criteria and history;
    # this specific new task needs its own pre-answer goal in today's session.
    c.execute("UPDATE agent_objective SET status='paused',updated_at=? WHERE objective_id=?", (now, objective['objective_id']))
    oid = str(uuid4())
    c.execute('''INSERT INTO agent_objective(objective_id,child_id,session_id,objective_type,summary,
        success_criteria_json,phase,status,created_at,updated_at,last_agent_turn_id)
        VALUES(?,?,?,'learning',?,?,'check','active',?,?,?)''',
        (oid, turn['child_id'], turn['session_id'], '按新题要求说明思路并根据反馈改进',
         json.dumps(points, ensure_ascii=False), now, now, turn['agent_turn_id']))
    bound = dict(objective_id=oid, child_id=turn['child_id'], session_id=turn['session_id'],
        prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], diagnostic_item_id=source['diagnostic_item_id'],
        phase='check', success_criteria=points, created_at=now, **goal.LOCAL)
    c.execute("""UPDATE agent_plan SET objective_id=?,plan_json=json_set(plan_json,'$.local_learning_binding',json(?)),
        context_manifest_json=json_set(context_manifest_json,'$.document_open_check',json(?)) WHERE plan_id=?""",
        (oid, json.dumps(bound, ensure_ascii=False), json.dumps(bound, ensure_ascii=False), plan['plan_id']))
    c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime.objective_id',?) WHERE agent_turn_id=?",
        (oid, turn['agent_turn_id']))


def validate_origin(c, plan, prompt):
    """Recheck the exact goal and original-review binding, never recover by guessing."""
    from .document_independent_check import source_reason
    source = _qualified(c, prompt['child_id'], prompt['diagnostic_item_id'])
    marker = json.loads(plan['context_manifest_json']).get(KEY)
    binding = json.loads(plan['plan_json']).get('local_learning_binding')
    ref = json.loads(prompt['request_json']).get('document_independent', {})
    goal.require(isinstance(marker, dict) and marker == binding and source
        and source['answer_type'] == 'open_response' and source_reason(c, prompt) is None
        and ref.get('turn_id') == plan['agent_turn_id']
        and marker.get('objective_id') == plan['objective_id']
        and marker.get('child_id') == prompt['child_id'] == plan['child_id']
        and marker.get('session_id') == prompt['origin_session_id'] == plan['session_id']
        and marker.get('prompt_id') == prompt['prompt_id']
        and marker.get('item_version') == prompt['item_version'] == _version(source)
        and marker.get('diagnostic_item_id') == prompt['diagnostic_item_id']
        and marker.get('phase') == 'check'
        and marker.get('success_criteria') == json.loads(source['expected_answer_json'])['task_points'],
        'DOCUMENT_OPEN_BINDING_CHANGED')


def validate_projection(c, row, prompt):
    """Admit local lineage for filtering, never include it in the cloud payload."""
    owned = c.execute('SELECT * FROM agent_plan WHERE plan_id=?', (row['plan_id'],)).fetchone()
    validate_origin(c, owned, prompt)
    action = json.loads(row['action_json'])
    extra = set()
    if action.get('document_spoken_supplement'):
        from .document_spoken_supplement import original
        goal.require(original(c,prompt) is not None,'DOCUMENT_SUPPLEMENT_ORIGIN_CHANGED')
        extra={'document_spoken_supplement','source_voice_input_id'}
    manifest = json.loads(row['context_manifest_json'])
    journal = c.execute('SELECT binding_json FROM voice_control_intent_request WHERE request_key=?',
        (action.get('document_revisit_request'),)).fetchone()
    goal.require(journal and row['step_index'] == 0 and json.loads(row['arguments_json']) == {}
        and set(action) <= {'type','capability','execution_location','local_only','cloud_transmission_allowed',
            'mastery_update_allowed','document_revisit_commitment','document_revisit_request','document_independent_check',
            'document_target_selection','voice_input_id','voice_input_binding','agent_runtime'} | extra
        and set(manifest) == {'document_revisit','voice_input_binding',KEY}
        and manifest['document_revisit'] == json.loads(journal[0])['document_revisit']
        and manifest['voice_input_binding'] == action.get('voice_input_binding'), 'DOCUMENT_OPEN_TRANSPORT_CHANGED')
