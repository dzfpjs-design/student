import json
from uuid import uuid4

import pytest

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.tutor_playback import TutorPlaybackError
from test_tutor_playback import CHILD, begin, control, flow


@pytest.mark.parametrize('scope', ['tutor', 'all_history'])
def test_playback_export_and_delete_include_pending_controls(flow, tmp_path, scope):
    db, service, sid, _, _ = flow
    view = control(flow, begin(flow), 'resume')
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    exported = privacy.export_child(CHILD)
    with open(exported['path'], encoding='utf-8') as stream:
        data = json.load(stream)
    assert data['tutor_playback_checkpoints'][0]['playback_id'] == view['playback_id']
    assert len(data['tutor_playback_events']) == 2
    assert privacy.delete_child_scope(CHILD, scope, '确认删除', str(uuid4()))['status'] == 'completed'
    if scope == 'all_history':
        with pytest.raises(TutorPlaybackError, match='SESSION_INACTIVE'):
            service.read(sid)
    else:
        assert service.read(sid) is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM tutor_playback_event').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
