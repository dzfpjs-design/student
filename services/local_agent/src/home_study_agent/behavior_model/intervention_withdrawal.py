"""Durable, nonblocking withdrawal admission for stale optional support."""

PENDING_REASON = 'habit_support_source_changed_pending'
FINAL_REASON = 'habit_support_source_changed'


def pending_for_state(connection, child_id, state):
    return bool(connection.execute('''SELECT 1 FROM intervention WHERE child_id=? AND candidate_state=?
        AND status='active' AND withdrawal_reason=? LIMIT 1''',
        (child_id, state, PENDING_REASON)).fetchone())
