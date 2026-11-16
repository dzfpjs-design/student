"""Continue a verified foundation error through the existing local mistake flow."""
from datetime import datetime, timezone
import json

from .diagnostic_prompt import DiagnosticPromptService, _TransactionDatabase
from .goal_roadmap import goal_catalog_source_reason, _catalog_source_reason
from .family_clock import family_today
from .mistake_dialogue import MistakeDialogueService
from .learning_state import LearningStateService


def review_handoff(c, focus):
    """A completed foundation slot may yield to an explicit active review slot."""
    origin = focus['state'].get('foundation_origin')
    if not isinstance(origin, dict) or focus['state'].get('pending_operation'):
        return None
    from .mistake_plan_completion import active_binding
    review = active_binding(c, focus)
    if not review or review['plan_item_id'] == origin.get('plan_item_id'):
        return None
    previous = c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?
        AND pi.task_type='goal_prerequisite' AND pi.status='completed' AND pi.completed_at IS NOT NULL
        AND EXISTS(SELECT 1 FROM plan_item_event e WHERE e.plan_item_id=pi.plan_item_id AND e.event_type='completed')''',
        (origin.get('plan_item_id'), focus['child_id'], focus['session_id'])).fetchone()
    if not previous:
        return None
    return {'from_origin': origin, 'review_plan_item_id': review['plan_item_id'],
        'review_started_at': review['started_at'], 'reason': 'explicit_same_day_review'}


def continuation_hold(c, focus, *, allow_paused=False):
    """Recheck the original task without reselecting after learning has changed."""
    origin = focus['state'].get('foundation_origin')
    if not origin:
        return None
    changed = {'reason': 'source_changed', 'message': '原任务或题目依据已经变化，先保留刚才的回答，暂不继续出题或评分。'}
    if not isinstance(origin, dict):
        return changed
    task = c.execute('''SELECT pi.*,dp.plan_date FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN daily_session ds ON ds.session_id=dp.session_id AND ds.child_id=dp.child_id
        JOIN child_profile cp ON cp.child_id=dp.child_id
        WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=? AND dp.status='active'
        AND ds.status='active' AND ds.state='study' AND cp.status='active' ''',
        (origin.get('plan_item_id'), focus['child_id'], focus['session_id'])).fetchone()
    if not task or task['task_type'] != 'goal_prerequisite':
        return changed
    owners = c.execute('''SELECT context_manifest_json FROM agent_plan WHERE child_id=? AND session_id=?
        AND status!='cancelled' AND json_extract(context_manifest_json,'$.goal_prompt_binding.plan_item_id')=?
        AND json_extract(context_manifest_json,'$.goal_prompt_binding.prompt_id')=?''',
        (focus['child_id'], focus['session_id'], task['plan_item_id'], origin.get('prompt_id'))).fetchall()
    try:
        bindings = [json.loads(row[0])['goal_prompt_binding'] for row in owners]
        if not bindings or any(binding != bindings[0] for binding in bindings):
            return changed
        from .task_start_binding import TaskStartBinding
        owned = TaskStartBinding.foundation_resume_focus(c, focus['session_id'], focus['child_id'],
            {**dict(task), 'goal_prompt_binding': bindings[0]}, exclusive=False, _focus=focus)
        if not owned or owned['origin'] != origin or owned['mistake_id'] != focus['mistake_id']:
            return changed
        foundation = bindings[0]['binding']['goal_foundation']
        now = datetime.now(timezone.utc)
        if _catalog_source_reason(c, focus['child_id'], {'knowledge_node_id': foundation['target_node_id'],
                'catalog_binding': foundation['target_catalog_binding']}, family_today(now)):
            return changed
        program = c.execute('''SELECT gp.status FROM goal_program gp JOIN goal_cycle gc USING(program_id)
            WHERE gc.cycle_id=? AND gp.child_id=? AND gc.child_id=?''',
            (task['source_ref'], focus['child_id'], focus['child_id'])).fetchone()
        if not program or program[0] != 'active':
            return changed
        if task['status'] != 'active' and not (allow_paused and task['status'] == 'paused'):
            return {'reason': 'task_inactive', 'message': '原学习任务已暂停或结束。刚才的回答先保留，不继续评分；可以先恢复原任务，或按今天的计划再安排。'}
        elapsed = task['active_seconds']
        if task['active_segment_started_at']:
            delta = (now - datetime.fromisoformat(task['active_segment_started_at'])).total_seconds()
            if delta < 0:
                return changed
            elapsed += delta
        if task['plan_date'] != family_today(now).isoformat() or elapsed >= task['expected_minutes'] * 60:
            return {'reason': 'time_budget', 'message': '这段计划的学习时间已到，刚才的回答和订正进度已保留，不再追加题目或评分。我们先休息，后续按计划复习。'}
    except (ValueError, TypeError, KeyError, AttributeError):
        return changed
    return None


def continue_after_answer(database, session_id, prompt_id):
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        session = DiagnosticPromptService._session(c, session_id)
        if session['state'] != 'study' or session['status'] != 'active' or session['child_status'] != 'active':
            return None
        prompt = DiagnosticPromptService._owned(c, session, prompt_id)
        receipt = DiagnosticPromptService._receipt(c, prompt)
        if (prompt['status'] != 'answered' or not receipt
                or receipt['correctness'] is None or receipt['correctness'] >= 1):
            return None
        receipt = LearningStateService._attempt(c, receipt['attempt_id'])
        if receipt['score_status'] != 'verified_local':
            return None
        plans = c.execute('''SELECT p.context_manifest_json,pi.* FROM agent_plan p
            JOIN plan_item pi ON pi.plan_item_id=json_extract(p.context_manifest_json,'$.goal_prompt_binding.plan_item_id')
            JOIN daily_plan dp ON dp.plan_id=pi.plan_id
            WHERE p.child_id=? AND p.session_id=? AND dp.child_id=p.child_id AND dp.session_id=p.session_id
            AND dp.status='active' AND pi.status='active' AND pi.task_type='goal_prerequisite'
            AND p.status!='cancelled' AND json_extract(p.context_manifest_json,'$.goal_prompt_binding.prompt_id')=?''',
            (session['child_id'], session_id, prompt_id)).fetchall()
        if not plans or len({p['plan_item_id'] for p in plans}) != 1:
            return None
        task = plans[0]
        bindings = [json.loads(p['context_manifest_json'])['goal_prompt_binding'] for p in plans]
        if any(b != bindings[0] for b in bindings[1:]):
            return None
        binding = bindings[0]
        if binding.get('item_version') != prompt['item_version'] or goal_catalog_source_reason(c, session_id, task['plan_item_id']):
            return None
        foundation = binding.get('binding', {}).get('goal_foundation') or {}
        now = datetime.now(timezone.utc)
        if _catalog_source_reason(c, session['child_id'], {
                'knowledge_node_id': foundation.get('target_node_id'),
                'catalog_binding': foundation.get('target_catalog_binding')}, family_today(now)):
            return None
        program = c.execute('''SELECT gp.status FROM goal_program gp JOIN goal_cycle gc USING(program_id)
            WHERE gc.cycle_id=? AND gp.child_id=? AND gc.child_id=?''',
            (task['source_ref'], session['child_id'], session['child_id'])).fetchone()
        if not program or program[0] != 'active':
            return None
        elapsed = task['active_seconds']
        if task['active_segment_started_at']:
            try:
                started = datetime.fromisoformat(task['active_segment_started_at'])
                seconds = (now - started).total_seconds()
                if seconds < 0:
                    return None
                elapsed += seconds
            except (ValueError, TypeError):
                return None
        if task['expected_minutes'] * 60 - elapsed < 180:
            return None
        mistakes = c.execute('''SELECT DISTINCT mc.mistake_id FROM mistake_occurrence mo
            JOIN mistake_case mc USING(mistake_id)
            WHERE mo.evidence_id=? AND mo.child_id=? AND mc.child_id=mo.child_id
            AND mc.status NOT IN ('excluded','mastered') AND mc.diagnostic_item_id=?''',
            (receipt['evidence_id'], session['child_id'], prompt['diagnostic_item_id'])).fetchall()
        if len(mistakes) != 1:
            return None
        mid = mistakes[0][0]
        current = c.execute('SELECT * FROM mistake_dialogue WHERE session_id=? AND child_id=?',
            (session_id, session['child_id'])).fetchone()
        service = MistakeDialogueService(_TransactionDatabase(c))
        if current and current['phase'] != 'closed':
            # Replay only the initial handoff, never overwrite later child work.
            latest = c.execute('SELECT client_action_id FROM mistake_dialogue_event WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                (session_id, session['child_id'])).fetchone()
            if (current['mistake_id'] != mid or current['phase'] == 'paused' or not latest
                    or latest[0] != 'foundation-remediation:' + prompt_id):
                return None
            focus = service.state(session_id)
            origin = focus['state'].get('foundation_origin')
            if not origin or origin.get('prompt_id') != prompt_id:
                return None
            return _packet(focus, origin, service.prompt(focus))
        if c.execute('SELECT 1 FROM mistake_dialogue_event WHERE client_action_id=?', ('foundation-remediation:' + prompt_id,)).fetchone():
            return None
        if c.execute("SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=? AND status IN ('waiting_answer','answer_saved')", (session_id,)).fetchone():
            return None
        origin = {'prompt_id': prompt_id, 'item_version': prompt['item_version'], 'attempt_id': receipt['attempt_id'],
            'plan_item_id': task['plan_item_id'], 'evidence_id': receipt['evidence_id']}
        focus = service.open_review(session_id, 'foundation-remediation:' + prompt_id, mistake_id=mid, origin=origin)
        c.commit()
        return _packet(focus, origin, focus['response_text'])


def _packet(focus, origin, message):
    return {'origin': origin, 'mistake_id': focus['mistake_id'], 'phase': focus['phase'],
        'response_text': '这次回答还没有完全答对，我们先核对一个关键步骤，不急着判断错因。' + message,
        'local_only': True, 'cloud_transmission_allowed': False}
