"""Review changed assignment scope against its exact retained execution history."""
import json
import math

from .learning_input_revision import _version

REASONS = {'executed_scope_requires_review', 'remaining_scope_requires_review'}


def extend_origin(connection, origin):
    if origin['reason'] not in REASONS or origin['payload'].get('reclaim'):
        return origin
    source = origin['source']
    binding = origin['payload']['source_binding']
    items = [dict(row) for row in connection.execute('''SELECT i.* FROM plan_item i
        JOIN daily_plan p USING(plan_id) WHERE p.plan_id=? AND p.child_id=? AND p.session_id=?
        AND i.homework_id=? AND i.source_type='school_homework' AND i.status!='cancelled'
        ORDER BY i.plan_item_id''', (binding['plan_id'], origin['child_id'], origin['session_id'], source['homework_id']))]
    latest = connection.execute('''SELECT * FROM learning_input_carryover_event
        WHERE obligation_id=? AND operation IN ('confirm_scope','withdraw_scope')
        ORDER BY revision DESC LIMIT 1''', (origin['obligation_id'],)).fetchone()
    scope = {**dict(latest), 'result': json.loads(latest['result_json'])} if latest else None
    valid = origin['source_current'] and bool(items)
    if scope:
        valid = (valid and scope['operation'] == 'confirm_scope'
                 and scope['result'].get('revision_execution') == items)
    budget = sum(max(0, row['expected_minutes'] - math.ceil(max(
        row['active_seconds'] or 0, (row['actual_minutes'] or 0) * 60) / 60)) for row in items)
    review = {'requires_scope_review': True, 'execution': items,
              'execution_settled': bool(items) and all(row['status'] in {'completed', 'rescheduled'} for row in items),
              'maximum_remaining_minutes': budget,
              'plan_item_id': items[0]['plan_item_id'] if items else None}
    return {**origin, 'revision_scope': review, 'scope': scope,
            'scope_review_required': not scope, 'source_current': bool(valid),
            'source_version': _version([origin['source_version'], 'revision_remaining_scope_v1',
                                        items, scope['event_id'] if scope else None])}
