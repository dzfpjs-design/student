import json
from uuid import uuid4

import pytest

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_curriculum_supply import CHILD, prepared, publish


@pytest.mark.parametrize('scope', ['learning', 'knowledge', 'all_history'])
def test_supply_evidence_export_delete_and_retire_published_question(tmp_path, scope):
    db, service, payload = prepared(tmp_path)
    published = publish(service, payload)
    privacy = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b's' * 32)
    privacy.initialize()
    artifact = privacy.export_child(CHILD)
    with open(artifact['path'], encoding='utf-8') as stream:
        exported = json.load(stream)
    assert len(exported['curriculum_supply_selections']) == 1
    assert len(exported['curriculum_supply_packages']) == 1
    assert len(exported['curriculum_supply_events']) == 4
    assert privacy.deletion_preview(CHILD)['curriculum_supply'] == 6
    result = privacy.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))
    assert result['status'] == 'completed'
    assert privacy.deletion_preview(CHILD)['curriculum_supply'] == 0
    with db.connect() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute('SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?',
                         (published['diagnostic_item_id'],)).fetchone()[0] == 'retired'
