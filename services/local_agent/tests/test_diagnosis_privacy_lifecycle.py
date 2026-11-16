import json
from uuid import uuid4

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_mistake_gate_reliability import CHILD, flow, prepare


def test_diagnosis_events_are_exported_counted_and_deleted(flow, tmp_path):
    db, _, _, _ = flow
    book, case = prepare(flow)
    book.propose_diagnosis_hypotheses(case, CHILD, [
        {'error_type': 'concept_gap', 'stance': 'candidate', 'explanation': 'Possible explanation'},
    ], 0, str(uuid4()))
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    exported = privacy.export_child(CHILD)
    with open(exported['path'], encoding='utf-8') as stream:
        payload = json.load(stream)
    assert len(payload['mistake_diagnosis_events']) == 1
    assert privacy.deletion_preview(CHILD)['mistake_diagnosis_events'] == 1
    deleted = privacy.delete_child_scope(CHILD, 'learning', '确认删除', str(uuid4()))
    assert deleted['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_diagnosis_event').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    assert privacy.deletion_preview(CHILD)['mistake_diagnosis_events'] == 0
