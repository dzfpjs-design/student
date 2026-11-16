"""PET delivery history follows existing child export and erasure scope."""
import json
from uuid import uuid4

import pytest

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_pet_media_evidence import media, begin, review, CHILD


@pytest.mark.parametrize('scope', ['learning', 'media', 'tutor', 'knowledge', 'all_history'])
def test_pet_media_history_is_exported_and_erased_without_cross_child_loss(media, tmp_path, scope):
    db = media[0]
    row = review(media, begin(media))
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    with open(privacy.export_child(CHILD)['path'], encoding='utf-8') as stream:
        exported = json.load(stream)
    assert exported['pet_media_bindings'][0]['media_id'] == row['media_id']
    assert exported['pet_media_events']
    assert privacy.deletion_preview(CHILD)['pet_media_bindings'] == 1
    privacy.delete_child_scope('child-junior-3', scope, '确认删除', str(uuid4()))
    assert media[1].read(media[2], media_id=row['media_id'])['source_reviewed']
    privacy.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM pet_media_binding').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM pet_media_event').fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
