"""Reuse an original-reader result, never a previous qualification decision."""
from collections import Counter

from home_study_agent import local_supply, writing_response_origin
from home_study_agent.rubric_calibration_evidence import source_snapshot
from test_school_open_reviews import school_case, answer_and_review, CHILD


def test_written_snapshot_reuses_original_reader_item(school_case, monkeypatch):
    db = school_case[0]
    _, answered, _, _, _ = answer_and_review(school_case, submit=False)
    attempt = answered['attempt']
    iid, eid = attempt['diagnostic_item_id'], attempt['evidence_id']
    with db.connect() as c:
        c.execute('BEGIN')
        expected = source_snapshot(c, CHILD, eid)
        calls = Counter()
        original = local_supply.validate_item

        def counted(connection, child, item, **kwargs):
            calls[item] += 1
            return original(connection, child, item, **kwargs)

        monkeypatch.setattr(local_supply, 'validate_item', counted)
        monkeypatch.setattr(writing_response_origin, 'validate_item', counted)
        before = c.total_changes
        assert source_snapshot(c, CHILD, eid) == expected
        # One writing-source check plus the independent current-prompt check.
        assert calls[iid] == 2
        assert c.total_changes == before and c.in_transaction
        c.rollback()
