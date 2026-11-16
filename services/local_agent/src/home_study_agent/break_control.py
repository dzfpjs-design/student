"""Local break selection and actual transition receipts, never learning evidence."""
from __future__ import annotations

import json


class BreakControl:
    def __init__(self, database):
        self.database = database

    def initialize(self):
        with self.database.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS break_control_receipt (
                client_action_id TEXT PRIMARY KEY,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
                operation TEXT NOT NULL CHECK(operation IN ('begin','resume')),
                binding_json TEXT NOT NULL,
                transition_id TEXT NOT NULL REFERENCES session_transition(transition_id) ON DELETE CASCADE,
                result_json TEXT NOT NULL, created_at TEXT NOT NULL)''')
            c.commit()

    @staticmethod
    def snapshot(c, session_id, child_id=None):
        from .repository import PlanInputError, UnknownSessionError
        session = c.execute('''SELECT s.child_id,s.state,s.status,r.break_minutes
            FROM daily_session s JOIN family_rule r ON r.child_id=s.child_id WHERE s.session_id=?''', (session_id,)).fetchone()
        if not session:
            raise UnknownSessionError(session_id)
        if child_id is not None and child_id != session['child_id']:
            raise PlanInputError('休息操作与当前孩子不匹配。')
        plans = c.execute("SELECT plan_id,version FROM daily_plan WHERE session_id=? AND child_id=? AND status='active'",
                          (session_id, session['child_id'])).fetchall()
        if len(plans) > 1:
            raise PlanInputError('存在多个当前计划，需要先核对休息对应的计划。')
        plan = dict(plans[0]) if plans else None
        items = c.execute('''SELECT plan_item_id,source_type,source_ref,homework_id,task_type,status,expected_minutes FROM plan_item
            WHERE plan_id=? AND status IN ('active','paused','pending') ORDER BY position,plan_item_id''',
            ((plan or {}).get('plan_id'),)).fetchall()
        active = [dict(item) for item in items if item['status'] == 'active']
        if len(active) > 1:
            raise PlanInputError('存在多个进行中的任务，不能猜测要暂停哪一项。')
        current = active[0] if active else None
        pending = next((dict(item) for item in items if item['status'] == 'pending'), None)
        planned = current if current and current['source_type'] == 'planned_break' else (
            pending if session['state'] == 'study' and not current and pending
            and pending['source_type'] == 'planned_break' else None)
        result = {'child_id': session['child_id'], 'session_id': session_id, 'state': session['state'],
                  'session_status': session['status'], 'plan': plan, 'active_item': current,
                  'planned_break_item': planned, 'resume_item_id': None,
                  'origin_action_id': None, 'break_minutes': session['break_minutes'], 'status': 'ready'}
        if session['state'] == 'study':
            result['resume_item_id'] = current['plan_item_id'] if current and not planned else None
        elif session['state'] == 'break':
            transition = c.execute('''SELECT transition_id FROM session_transition WHERE session_id=?
                ORDER BY rowid DESC LIMIT 1''', (session_id,)).fetchone()
            table = c.execute("SELECT 1 FROM sqlite_master WHERE name='break_control_receipt'").fetchone()
            origin = c.execute('''SELECT * FROM break_control_receipt WHERE session_id=? AND child_id=?
                AND operation='begin' AND transition_id=?''', (session_id, session['child_id'], transition[0])).fetchone() if table and transition else None
            if not origin:
                result['status'] = 'original_break_unbound'
            else:
                original = json.loads(origin['binding_json'])
                result['origin_action_id'] = origin['client_action_id']
                result['resume_item_id'] = original['resume_item_id']
                original_break = original.get('planned_break_item')
                if original['plan'] != plan or (original_break and (
                        not planned or any(original_break.get(key) != planned.get(key)
                            for key in ('plan_item_id', 'source_type', 'source_ref', 'homework_id', 'task_type', 'expected_minutes')))):
                    result['status'] = 'original_plan_changed'
                if result['resume_item_id'] and not any(item['plan_item_id'] == result['resume_item_id']
                        and item['status'] == 'paused'
                        and all(item[key] == original['active_item'].get(key)
                                for key in ('source_type', 'source_ref', 'homework_id', 'task_type', 'expected_minutes'))
                        for item in items):
                    result['status'] = 'original_task_changed'
                if current and (not original_break or current['plan_item_id'] != original_break['plan_item_id']):
                    result['status'] = 'another_task_active'
        return result

    @staticmethod
    def recorded(c, session_id, operation, key):
        from .repository import PlanInputError
        row = c.execute('SELECT * FROM break_control_receipt WHERE client_action_id=?', (key,)).fetchone()
        if not row:
            old = c.execute('SELECT * FROM session_transition WHERE client_action_id=?', (key,)).fetchone()
            if not old:
                return None
            expected = ('study', 'break') if operation == 'begin' else ('break', 'study')
            if old['session_id'] != session_id or (old['from_state'], old['to_state']) != expected:
                raise PlanInputError('休息操作号与原会话或原操作不匹配。')
            return {'operation': operation, 'session_id': session_id, 'transition_id': old['transition_id'],
                    'from_state': old['from_state'], 'to_state': old['to_state'],
                    'status': 'legacy_recorded', 'recorded_at': old['occurred_at']}
        owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if not owner or row['session_id'] != session_id or row['child_id'] != owner[0] or row['operation'] != operation:
            raise PlanInputError('休息操作号与原会话或原操作不匹配。')
        return json.loads(row['result_json'])

    @staticmethod
    def save(c, binding, operation, key, now):
        transition = c.execute('SELECT transition_id,from_state,to_state FROM session_transition WHERE client_action_id=?', (key,)).fetchone()
        result = {'operation': operation, 'session_id': binding['session_id'], 'child_id': binding['child_id'],
                  'transition_id': transition['transition_id'], 'from_state': transition['from_state'],
                  'to_state': transition['to_state'], 'recorded_at': now}
        c.execute('INSERT INTO break_control_receipt VALUES (?,?,?,?,?,?,?,?)',
                  (key, binding['child_id'], binding['session_id'], operation, json.dumps(binding),
                   transition['transition_id'], json.dumps(result), now))
        return result
