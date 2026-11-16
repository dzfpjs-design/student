import json
from uuid import uuid4

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_parent_action_evidence import submit
from test_parent_coaching_feedback_semantics import CHILD, report, repository


def test_frozen_parent_action_export_preview_and_report_erasure(repository, tmp_path):
    saved = report(repository)
    submit(repository, saved)
    lifecycle = PrivacyLifecycleService(repository.database, tmp_path)
    lifecycle.initialize()
    with open(lifecycle.export_child(CHILD)['path'], encoding='utf-8') as stream:
        exported = json.load(stream)
    assert len(exported['parent_action_snapshots']) == 1
    assert len(exported['parent_action_feedback_events']) == 1
    assert lifecycle.deletion_preview(CHILD)['parent_actions'] == 2
    result = lifecycle.delete_child_scope(CHILD, 'reports', '确认删除', str(uuid4()))
    assert result['status'] == 'completed'
    assert lifecycle.deletion_preview(CHILD)['parent_actions'] == 0
    with repository.database.connect() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
