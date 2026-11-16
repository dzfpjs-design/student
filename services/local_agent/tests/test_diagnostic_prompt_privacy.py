import json
from uuid import uuid4

from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_diagnostic_prompt import CHILD, flow, prepare


def test_pending_question_is_exported_erased_and_no_longer_captures_answers(flow, tmp_path):
    db, prompts, sid = flow
    prepare(prompts, sid)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    artifact = privacy.export_child(CHILD)
    with open(artifact['path'], encoding='utf-8') as stream:
        exported = json.load(stream)
    assert len(exported['diagnostic_prompts']) == 1
    assert privacy.deletion_preview(CHILD)['diagnostic_prompts'] == 1
    result = privacy.delete_child_scope(CHILD, 'learning', '确认删除', str(uuid4()))
    assert result['status'] == 'completed'
    assert prompts.read(sid)['status'] == 'no_item'
    assert privacy.deletion_preview(CHILD)['diagnostic_prompts'] == 0
    with db.connect() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
