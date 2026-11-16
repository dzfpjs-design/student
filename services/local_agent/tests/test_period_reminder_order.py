"""Latest reminder feedback is ordered by time, not ISO offset spelling."""
from datetime import datetime, timedelta, timezone

import pytest

from home_study_agent.family_clock import FAMILY_ZONE
from home_study_agent.repository import coaching_evidence_ref
from test_period_report_reliable_evidence import flow, learning_clock, CHILD


@pytest.mark.parametrize('microseconds', [False, True])
def test_newest_feedback_survives_five_item_limit_with_mixed_offsets(flow, microseconds):
    f = flow
    local = datetime.combine(f.reference + timedelta(days=3), datetime.min.time(), tzinfo=FAMILY_ZONE)
    older = [(f'older-{i}', local + timedelta(hours=9 + i)) for i in range(5)]
    newest = local + timedelta(hours=14)
    if microseconds:
        older = [(key, local + timedelta(hours=14, microseconds=i)) for i, (key, _) in enumerate(older)]
        newest += timedelta(microseconds=5)
    rows = [(key, at.isoformat()) for key, at in older]
    rows.append(('newest', newest.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')))
    f.clock[0] = (newest + timedelta(minutes=1)).astimezone(timezone.utc).isoformat()
    with f.db.connect() as c:
        for key, at in rows:
            c.execute('''INSERT INTO intervention_record(intervention_id,child_id,session_id,
                trigger_state,confidence,stage,message_text,policy_version,child_response,status,created_at,responded_at)
                VALUES (?,?,?,'synthetic',0.8,'offer','Synthetic reminder','test','accepted','responded',?,?)''',
                (key, CHILD, f.sid, at, at))
        c.commit()
        journal = [tuple(row) for row in c.execute('SELECT * FROM intervention_record ORDER BY intervention_id')]
    result = f.reports.generate(CHILD, 'weekly', f.reference + timedelta(days=6))
    refs = result['content']['parent_coaching']['evidence_refs']
    assert coaching_evidence_ref(CHILD, 'intervention', 'newest') in refs
    assert coaching_evidence_ref(CHILD, 'intervention', 'older-0') not in refs
    with f.db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM intervention_record ORDER BY intervention_id')] == journal
