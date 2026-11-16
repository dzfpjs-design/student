"""Borrow the real goal route to schedule prerequisite work within its budget."""
from datetime import datetime

from .family_clock import family_today
from .goal_roadmap import build_goal_roadmap, _catalog, _catalog_binding


TASK_TYPE = 'goal_prerequisite'


def selected_review_for_node(c, child_id, node_id, selected):
    """Only the selected, child-owned skill can represent this goal's work."""
    if not node_id:
        return None
    for item in selected:
        table = {'due_review': ('review_task', 'review_task_id'),
                 'mistake_review': ('mistake_case', 'mistake_id')}.get(item['source_type'])
        if table:
            row = c.execute(f'SELECT knowledge_node_id FROM {table[0]} WHERE {table[1]}=? AND child_id=?',
                (item.get('source_ref'), child_id)).fetchone()
            if row and row[0] == node_id:
                return item
    return None


def foundation_candidate(c, child_id, intended_node, selected, *, as_of):
    now = datetime.fromisoformat(as_of)
    today = family_today(now)
    program = c.execute("SELECT * FROM goal_program WHERE child_id=? AND track='competition_math_path' AND status='active'", (child_id,)).fetchone()
    if not program or not intended_node:
        return None
    roadmap = build_goal_roadmap(c, dict(program), today=today, feedback_now=now,
                                 action_target_node_id=intended_node)
    action = roadmap['next_daily_action']
    if not action or action['knowledge_node_id'] == intended_node:
        return None
    # Only replace this challenge with its ancestor, never another unrelated goal.
    child = c.execute('SELECT * FROM child_profile WHERE child_id=?', (child_id,)).fetchone()
    catalog = _catalog(c, child, today)
    target_basis = _catalog_binding(intended_node, catalog)
    from .goal_roadmap import _path
    relevant = _path(catalog[0], catalog[1], [intended_node], catalog[2], {})[-1]
    node = action['knowledge_node_id']
    if node not in relevant:
        return None
    cycle = c.execute('SELECT cycle_id FROM goal_cycle WHERE program_id=? AND child_id=? AND week_start<=? AND week_end>=?',
        (program['program_id'], child_id, today.isoformat(), today.isoformat())).fetchone()
    if not cycle:
        return None
    item = selected_review_for_node(c, child_id, node, selected)
    if item:
        return {'status': 'represented', 'source_type': item['source_type'], 'source_ref': item['source_ref'],
                'knowledge_node_id': node, 'item': item,
                'support': {'program_id': program['program_id'], 'cycle_id': cycle[0],
                    'knowledge_node_id': node, 'target_node_id': intended_node,
                    'target_catalog_binding': target_basis, 'mastery_claim_allowed': False,
                    'cloud_transmission_allowed': False}}
    evidence = {key: action[key] for key in ('knowledge_node_id', 'action', 'roadmap_source_version',
        'catalog_binding', 'mastery_claim_allowed') if key in action}
    evidence['goal_foundation'] = {'target_node_id': intended_node, 'target_catalog_binding': target_basis}
    return {'status': 'ready' if action['dispatchable'] else 'source_needed', 'item': {
        'homework_id': None, 'subject': action['subject'], 'title': '目标基础检查：' + action['title'],
        'minutes': action['minutes'], 'source_type': 'competition_math', 'source_ref': cycle[0],
        'task_type': TASK_TYPE, 'status': 'pending', 'cognitive_load': 'medium',
        'completion_standard': '完成当前基础的独立检查，根据答案继续教学和复测；单次答对不等于掌握。',
        'planning_evidence': evidence}}
