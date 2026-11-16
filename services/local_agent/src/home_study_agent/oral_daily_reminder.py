"""Local, version-bound offers for supplied daily checks; never permission to teach."""
import json
from datetime import datetime, timedelta

from .local_supply import digest
from .oral_daily_supply import SOURCE, candidates
from .oral_supply_execution import require

KIND = 'oral_daily_supply_ready'
KEY = 'daily_supply_offer'


def ready(c, child, session_id, now, version=None):
    from .supply_gap_resume import _plan
    require(c.in_transaction)
    session = c.execute('''SELECT * FROM daily_session WHERE session_id=? AND child_id=?
        AND status='active' AND state='study' ''', (session_id, child)).fetchone()
    if not session or (session['snooze_until'] and datetime.fromisoformat(session['snooze_until']) > now):
        return None
    plan = _plan(c, session_id, child, as_of=now)
    if not plan:
        return None
    for item in candidates(c, session_id, child, plan):
        identity = digest(item)
        if version is None or version == identity:
            return {'source_id': identity, 'item': item}
    return None


def acknowledged(c, child, session_id, now, voice_input_id=None):
    require(c.in_transaction)
    row = c.execute('''SELECT * FROM agent_proactive_prompt WHERE child_id=? AND session_id=?
        AND source_type=? AND status='delivered' AND acknowledged_at BETWEEN ? AND ? AND expires_at>?
        AND NOT EXISTS (SELECT 1 FROM agent_turn t WHERE t.session_id=agent_proactive_prompt.session_id
            AND t.created_at>=agent_proactive_prompt.acknowledged_at)
        ORDER BY acknowledged_at DESC LIMIT 1''',
        (child, session_id, KIND, (now-timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat())).fetchone()
    current = ready(c, child, session_id, now, row['source_id']) if row else None
    if current:
        if voice_input_id and not voice_after_offer(c, child, session_id, voice_input_id, row['acknowledged_at']):
            return None
        return {'prompt_id': row['prompt_id'], 'source_id': row['source_id'],
            **({'voice_input_id': voice_input_id} if voice_input_id else {})}
    return None


def voice_after_offer(c, child, session_id, voice_input_id, acknowledged_at):
    from .voice_capture_binding import capture_started_at
    row = c.execute('''SELECT created_at FROM voice_capture_binding WHERE voice_input_id=?
        AND child_id=? AND session_id=? AND status='completed' ''', (voice_input_id, child, session_id)).fetchone()
    onset = capture_started_at(c, voice_input_id) if row else None
    return bool(onset and datetime.fromisoformat(onset) >= datetime.fromisoformat(acknowledged_at))


def validate(c, child, session_id, target, now, turn_id=None):
    """The own turn may exist already; no intervening dialogue may explain 'yes'."""
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return validate(c, child, session_id, target, now, turn_id)
        finally:
            c.rollback()
    proof = (target or {}).get(KEY)
    require(isinstance(proof, dict) and set(proof) == {'prompt_id', 'source_id', 'voice_input_id'})
    item = target.get('item')
    require(item and item.get('source_type') == SOURCE and digest(item) == proof['source_id'])
    row = c.execute('''SELECT * FROM agent_proactive_prompt WHERE prompt_id=? AND child_id=?
        AND session_id=? AND source_type=? AND source_id=? AND status='delivered' ''',
        (proof['prompt_id'], child, session_id, KIND, proof['source_id'])).fetchone()
    require(row and row['acknowledged_at'] and datetime.fromisoformat(row['expires_at']) > now)
    at = datetime.fromisoformat(row['acknowledged_at'])
    require(now-timedelta(minutes=5) <= at <= now)
    require(voice_after_offer(c, child, session_id, proof['voice_input_id'], row['acknowledged_at']))
    require(not c.execute('''SELECT 1 FROM agent_turn WHERE session_id=? AND created_at>=?
        AND (? IS NULL OR agent_turn_id!=?)''', (session_id, row['acknowledged_at'], turn_id, turn_id)).fetchone())
    if turn_id:
        own = c.execute('SELECT created_at,action_json FROM agent_turn WHERE agent_turn_id=? AND child_id=? AND session_id=?',
            (turn_id, child, session_id)).fetchone()
        require(own and at <= datetime.fromisoformat(own[0]) <= now
            and json.loads(own['action_json']).get('voice_input_id') == proof['voice_input_id'])
    # Materialization changes the plan version and candidate set. Its existing
    # immutable supply receipt validates the same item after placement.
    placed = c.execute('SELECT 1 FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()
    if not placed:
        current = ready(c, child, session_id, now, proof['source_id'])
        require(current and current['item'] == item)
    return True


def due_current(c, child, session_id, commitment_id, now):
    """A missing daily source must not leak through the generic due reminder."""
    row = c.execute('''SELECT * FROM agent_commitment WHERE commitment_id=? AND child_id=?
        AND status IN ('pending','due') AND due_at<=?''', (commitment_id, child, now.isoformat())).fetchone()
    if not row:
        return False
    try:
        payload = json.loads(row['payload_json'])
        origin = payload.get('origin') if isinstance(payload, dict) else None
        if origin in {'oral_question_supply_v1', 'document_revisit_v1'}:
            return False
        if origin != 'daily_learning_closure':
            return True
        from .oral_daily_check import original, resolve_execution
        oral = original(c, child, commitment_id)
        from .supply_gap_resume import _plan
        plan = _plan(c, session_id, child, as_of=now)
        if not plan:
            return False
        if oral is None:
            from .learning_outcome_projection import LearningOutcomeProjection
            return bool(LearningOutcomeProjection(None).commitment_available(c, row)
                and c.execute('''SELECT 1 FROM plan_item WHERE plan_id=? AND source_type='agent_retention'
                    AND source_ref=? AND status IN ('pending','paused')''', (plan['plan_id'], commitment_id)).fetchone())
        tasks = c.execute('''SELECT plan_item_id FROM plan_item WHERE plan_id=? AND source_type=?
            AND source_ref=? AND status IN ('pending','paused')''', (plan['plan_id'], SOURCE, commitment_id)).fetchall()
        return len(tasks) == 1 and resolve_execution(c, session_id, tasks[0][0])['status'] == 'ready'
    except (ValueError, KeyError, TypeError):
        return False


def task_current(c, child, session_id, task_id):
    task = c.execute('''SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=? AND dp.status='active'
        AND pi.status='pending' ''', (task_id, child, session_id)).fetchone()
    if not task:
        return False
    if task['source_type'] != SOURCE:
        return True
    from .oral_daily_check import resolve_execution
    try:
        return resolve_execution(c, session_id, task_id)['status'] == 'ready'
    except (ValueError, KeyError, TypeError):
        return False
