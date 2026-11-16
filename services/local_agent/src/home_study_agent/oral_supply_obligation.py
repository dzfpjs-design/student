"""An explicit, unavailable new-question request belongs to its original goal.

This is a delivery obligation, not a score, generation permission or extra
study time. It shares the existing durable commitment store and deletion path.
"""
import json
import hashlib
from datetime import datetime, timezone
from datetime import timedelta
from uuid import uuid4

from .local_supply import digest

ORIGIN = 'oral_question_supply_v1'
LOCAL = dict(local_only=True, cloud_transmission_allowed=False, mastery_claim_allowed=False)


def require(value):
    if not value:
        from .agent_runtime import InvalidAgentPlanError
        raise InvalidAgentPlanError('ANSWER_ORAL_SUPPLY_SOURCE_CHANGED')


def identity(action):
    return {key: action[key] for key in ('child_id', 'session_id', 'parent_prompt_id',
        'parent_item_version', 'parent_source_version', 'knowledge_node_id')}


def record(c, turn_id, request_key):
    from .agent_runtime import AgentRuntimeStore
    from .voice_control_intent import VoiceControlIntent
    require(c.in_transaction)
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    require(turn)
    require(VoiceControlIntent.scope_valid(c, turn['session_id'], turn['child_id'], turn['client_action_id'], require_cloud=False))
    action = json.loads(turn['action_json'])
    require(action.get('type') == 'none' and action.get('source') == 'oral_followup_clarification')
    request = c.execute('''SELECT * FROM voice_control_intent_request WHERE request_key=?
        AND child_id=? AND session_id=? AND status='completed' ''',
        (request_key, turn['child_id'], turn['session_id'])).fetchone()
    fingerprint = hashlib.sha256(json.dumps([turn['child_id'], turn['session_id'], turn['transcript']], ensure_ascii=False).encode()).hexdigest()
    require(request and request['fingerprint'] == fingerprint)
    decision = json.loads(request['decision_json'])
    require(decision.get('status') == 'control' and decision.get('control') == 'oral.independent_check')
    packets = json.loads(request['binding_json']).get('oral_followups') or {}
    require(packets.get('independent_check', {}).get('reason') == 'oral_no_unexposed_source'
        and packets['independent_check']['status'] == 'blocked')
    arrival = c.execute('''SELECT * FROM learning_input_arrival WHERE child_id=? AND session_id=?
        AND client_action_id=?''', (turn['child_id'], turn['session_id'], turn['client_action_id'])).fetchone()
    require(arrival and arrival['utterance'] == turn['transcript'])
    snapshots = [entry.get('record') for entry in json.loads(arrival['snapshot_json'])
        if entry.get('kind') == 'oral_followup_arrival']
    require(len(snapshots) == 1 and snapshots[0] and snapshots[0]['packets'] == packets)
    snapshot = snapshots[0]
    goal = c.execute('''SELECT * FROM agent_objective WHERE objective_id=? AND child_id=?
        AND session_id=? AND objective_type='learning' AND status='active' ''',
        (snapshot['objective_id'], turn['child_id'], turn['session_id'])).fetchone()
    require(goal)
    parent = snapshot['parent']
    scope = dict(child_id=turn['child_id'], session_id=turn['session_id'], **parent)
    checked = AgentRuntimeStore.resolve_oral_followup(c, **scope, kind='independent_check')
    require(checked['status'] == 'blocked' and checked['reason'] == 'oral_no_unexposed_source')
    retry = AgentRuntimeStore.resolve_oral_followup(c, **scope, kind='retry')
    require(retry.get('status') == 'ready' and retry['binding_version'] == packets['retry']['binding_version'])
    source = identity(retry['action'])
    source_hash = digest(source)
    existing = c.execute('''SELECT commitment_id FROM agent_commitment WHERE child_id=? AND objective_id=?
        AND status IN ('pending','due') AND json_valid(payload_json)
        AND json_extract(payload_json,'$.origin')=? AND json_extract(payload_json,'$.source_hash')=?''',
        (turn['child_id'], goal['objective_id'], ORIGIN, source_hash)).fetchone()
    commitment_id = existing[0] if existing else str(uuid4())
    if not existing:
        now = datetime.now(timezone.utc).isoformat()
        payload = dict(origin=ORIGIN, source=source, source_hash=source_hash, request_turn_id=turn_id,
            request_key=request_key, summary='补齐同能力、未展示过的口述练习题',
            phase='awaiting_qualified_question', resolution=None, **LOCAL)
        c.execute('''INSERT INTO agent_commitment(commitment_id,objective_id,child_id,commitment_type,
            due_at,status,payload_json,created_at,client_action_id)
            VALUES(?,?,?,'follow_up',?,'pending',?,?,?)''',
            (commitment_id, goal['objective_id'], turn['child_id'], now,
                json.dumps(payload, ensure_ascii=False), now, 'oral-supply:' + turn_id))
    c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.oral_supply_obligation',json(?)) WHERE agent_turn_id=?",
        (json.dumps(dict(commitment_id=commitment_id, source_hash=source_hash, **LOCAL)), turn_id))
    return commitment_id


def prepared(c, turn_id, target, objective_id, prompt):
    """Called only after the Runtime binds a real new prompt in this transaction."""
    require(c.in_transaction)
    if target['kind'] != 'independent_check' or prompt['status'] != 'waiting_answer':
        return
    source = identity(target)
    rows = c.execute('''SELECT * FROM agent_commitment WHERE child_id=? AND objective_id=?
        AND status IN ('pending','due') AND json_valid(payload_json)
        AND json_extract(payload_json,'$.origin')=? AND json_extract(payload_json,'$.source_hash')=?''',
        (target['child_id'], objective_id, ORIGIN, digest(source))).fetchall()
    for row in rows:
        payload = json.loads(row['payload_json'])
        require(payload['source'] == source)
        payload.update(phase='question_prepared', resolution=dict(turn_id=turn_id,
            prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
            diagnostic_item_id=prompt['diagnostic_item_id'], learning_completed=False))
        c.execute("UPDATE agent_commitment SET status='completed',completed_at=?,payload_json=? WHERE commitment_id=?",
            (datetime.now(timezone.utc).isoformat(), json.dumps(payload, ensure_ascii=False), row['commitment_id']))


def reconcile(c, child_id):
    """A cancelled/terminal goal cannot leave an actionable delivery promise."""
    # This single statement also runs in context assembly before its first write;
    # the caller owns commit, as with the adjacent learning reconciliation hooks.
    c.execute('''UPDATE agent_commitment SET status='cancelled' WHERE child_id=?
        AND status IN ('pending','due') AND json_valid(payload_json)
        AND json_extract(payload_json,'$.origin')=? AND NOT EXISTS (
            SELECT 1 FROM agent_objective o WHERE o.objective_id=agent_commitment.objective_id
            AND o.child_id=agent_commitment.child_id AND o.objective_type='learning'
            AND o.status IN ('active','paused'))''', (child_id, ORIGIN))


def ready_offer(c, child_id, session_id, now, commitment_id=None):
    """Offer availability, not a prepared question or permission to teach."""
    from .agent_runtime import AgentRuntimeStore
    require(c.in_transaction)
    session = c.execute('''SELECT * FROM daily_session WHERE session_id=? AND child_id=?
        AND status='active' AND state='study' ''', (session_id, child_id)).fetchone()
    if not session or (session['snooze_until'] and datetime.fromisoformat(session['snooze_until']) > now):
        return None
    rows = c.execute('''SELECT ac.* FROM agent_commitment ac JOIN agent_objective ao
        ON ao.objective_id=ac.objective_id AND ao.child_id=ac.child_id
        WHERE ac.child_id=? AND ao.session_id=? AND ao.status='active' AND ao.objective_type='learning'
        AND ac.status IN ('pending','due') AND ac.due_at<=? AND (? IS NULL OR ac.commitment_id=?)
        AND CASE WHEN json_valid(ac.payload_json) THEN json_extract(ac.payload_json,'$.origin') END=?
        AND NOT EXISTS (SELECT 1 FROM agent_objective other WHERE other.child_id=ac.child_id
            AND other.session_id=ao.session_id AND other.status='active' AND other.objective_id!=ao.objective_id)
        ORDER BY ac.created_at,ac.commitment_id''',
        (child_id, session_id, now.isoformat(), commitment_id, commitment_id, ORIGIN)).fetchall()
    for row in rows:
        if c.execute("SELECT 1 FROM agent_plan WHERE objective_id=? AND status='waiting_child'", (row['objective_id'],)).fetchone():
            continue
        active = {item[0] for item in c.execute('''SELECT pi.plan_item_id FROM plan_item pi
            JOIN daily_plan dp USING(plan_id) WHERE dp.session_id=? AND pi.status='active' ''', (session_id,))}
        owned = {item[0] for item in c.execute('''SELECT json_extract(context_manifest_json,'$.scheduled_writing_goal.plan_item_id')
            FROM agent_plan WHERE objective_id=? AND child_id=? AND session_id=?
            AND json_valid(context_manifest_json)''', (row['objective_id'], child_id, session_id))}
        if active - owned:
            continue
        payload = json.loads(row['payload_json'])
        source = payload.get('source')
        if (not isinstance(source, dict) or payload.get('phase') != 'awaiting_qualified_question'
                or payload.get('source_hash') != digest(source) or source.get('child_id') != child_id
                or source.get('session_id') != session_id):
            continue
        packet = AgentRuntimeStore.resolve_oral_followup(c, child_id=child_id, session_id=session_id,
            prompt_id=source.get('parent_prompt_id'), item_version=source.get('parent_item_version'), kind='independent_check')
        if packet.get('status') == 'ready' and identity(packet['action']) == source:
            return row['commitment_id']
    return None


def acknowledged_offer(c, child_id, session_id, now):
    """Only a recently heard offer, with no intervening turn, explains 'yes'."""
    require(c.in_transaction)
    row = c.execute('''SELECT * FROM agent_proactive_prompt WHERE child_id=? AND session_id=?
        AND source_type='oral_supply_ready' AND status='delivered'
        AND acknowledged_at BETWEEN ? AND ? AND expires_at>?
        AND NOT EXISTS (SELECT 1 FROM agent_turn t WHERE t.session_id=agent_proactive_prompt.session_id
            AND t.created_at>agent_proactive_prompt.acknowledged_at)
        ORDER BY acknowledged_at DESC LIMIT 1''',
        (child_id, session_id, (now - timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat())).fetchone()
    if row and ready_offer(c, child_id, session_id, now, row['source_id']):
        return row['prompt_id']
    return None
