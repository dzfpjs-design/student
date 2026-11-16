"""Explicit synthetic signal fixtures, never official audio or child outcomes.

The explicit-test-parent approval simulates a full-recording reference
attestation for protocol tests. It does not verify the signal's spoken content,
actual acoustic delivery, or a real parent/child approval. Existing media is not
upgraded by this fixture.
"""
from uuid import uuid4

import pytest

from home_study_agent.database import LocalMediaRuntime
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from test_pet_media_evidence import wav_file


def audio_artifact(db, root, prompt, *, child_id=None, key=None):
    key = key or str(uuid4())
    path = root / (key + '.wav')
    wav_file(path)
    with db.connect() as c:
        c.execute('''INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,
            details_json,status,created_at,client_action_id) VALUES(?,?,?,'audio',?,'{}','completed',?,?)''',
            (key, child_id or prompt['child_id'], prompt['origin_session_id'], str(path), prompt['created_at'], key))
        c.commit()
    return key, path


def reviewed_audio(db, root, prompt):
    db.local_media_runtime = LocalMediaRuntime(root)
    service = PetMediaEvidenceService(db, media_root=root)
    with db.connect() as c:
        if c.execute("SELECT 1 FROM pet_media_binding WHERE prompt_id=? AND kind='listening'", (prompt['prompt_id'],)).fetchone():
            return
    aid, _ = audio_artifact(db, root, prompt)
    media = service.begin_listening(prompt['origin_session_id'], prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], artifact_id=aid, client_action_id=aid + ':bind')
    service.review_listening_source(prompt['origin_session_id'], media_id=media['media_id'],
        audio_sha256=media['audio']['sha256'], operation='approve', reviewer_id='explicit-test-parent',
        reviewer_role='parent', client_action_id=aid + ':approve', full_spoken_reference=True)


@pytest.fixture
def listening_resources(monkeypatch, tmp_path):
    from home_study_agent import goal_task_prompt
    prepare = goal_task_prompt.prepare_goal_task_prompt
    def with_audio(database, *args, **kwargs):
        result = prepare(database, *args, **kwargs)
        prompt = result.get('prompt') or {}
        if (prompt.get('item') or {}).get('pet_audio_modality') == 'listening':
            reviewed_audio(database, tmp_path, prompt)
        return result
    monkeypatch.setattr(goal_task_prompt, 'prepare_goal_task_prompt', with_audio)
