"""Read-only compatibility for the old planner's appended habit instruction."""
from __future__ import annotations

import json


LEGACY_REASON = '附着到第一项真实学习任务，不额外增加孩子负担。'


def academic_completion_item(connection, item: dict) -> dict:
    standard = str(item.get('completion_standard') or '')
    if '；本轮习惯实验：' not in standard:
        return item
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='habit_experiment'"
    ).fetchone():
        return item
    owner = connection.execute('''SELECT p.plan_id,p.child_id,p.generator FROM daily_plan p
        JOIN plan_item i USING(plan_id) WHERE i.plan_item_id=?''', (item['plan_item_id'],)).fetchone()
    if not owner or owner['generator'] != 'constraint_rules_v4':
        return item
    support = connection.execute('''SELECT d.metadata_json,h.child_action
        FROM plan_constraint_decision d JOIN habit_experiment h
          ON h.experiment_id=d.source_ref AND h.child_id=?
        WHERE d.plan_id=? AND d.obligation_type='habit_experiment'
          AND d.decision='applied' AND d.reason=? ORDER BY d.rowid''',
        (owner['child_id'], owner['plan_id'], LEGACY_REASON)).fetchall()
    if len(support) != 1:
        return item
    try:
        metadata = json.loads(support[0]['metadata_json'])
    except (TypeError, ValueError):
        return item
    if not isinstance(metadata, dict) or 'support_role' in metadata:
        return item
    suffix = '；本轮习惯实验：' + str(support[0]['child_action'] or '')
    if not support[0]['child_action'] or not standard.endswith(suffix) or standard == suffix:
        return item
    # Original placement receipts survive task reordering. A matching phrase by
    # itself is not permission to remove a teacher's or child's requirement.
    receipts = connection.execute('''SELECT metadata_json,obligation_type,source_ref FROM plan_constraint_decision
        WHERE plan_id=? AND decision='scheduled' AND obligation_type!='planned_break'
        ORDER BY rowid''', (owner['plan_id'],)).fetchall()
    if not receipts:
        return item
    if (receipts[0]['obligation_type'] != item.get('source_type')
            or receipts[0]['source_ref'] != item.get('source_ref')):
        return item
    try:
        first = json.loads(receipts[0]['metadata_json'])
    except (TypeError, ValueError):
        return item
    if not isinstance(first, dict) or first.get('plan_item_id') != item['plan_item_id']:
        return item
    return {**item, 'completion_standard': standard[:-len(suffix)]}
