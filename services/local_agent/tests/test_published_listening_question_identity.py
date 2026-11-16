"""Published original recordings retain their frozen exact question identity.

Synthetic PCM validates the local contract only, not recording content or learning.
"""
import json
import hashlib

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_obligations import (
    _source_question, project_child_mastery_evidence, project_item_exposure, question_identity,
)
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.repository import StudyRepository
from test_pet_reviewed_source_publication import prepared, publish, CHILD
from test_pet_listening_resource_admission import bind_audio
from test_pet_reviewed_source_contract import packet


def answer_published(client, db, root, monkeypatch, sid, published, answer, key, *, expected_status='verified_local'):
    result = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
        'track': 'pet', 'preferred_item_id': published['diagnostic_item_id'], 'client_action_id': key + ':prepare'})
    assert result.status_code == 200, result.text
    prompt = result.json()['prompt']
    audio, _ = bind_audio(client, db, root, sid, prompt, key=key + ':audio')
    mono = [1000]
    monkeypatch.setattr('home_study_agent.pet_media_evidence._clock_ms', lambda: mono[0])
    base = f'/v1/sessions/{sid}/pet-media/listening'
    for index in range(audio['qualification']['required_play_count']):
        result = client.post(base + '/control', json={'media_id': audio['media_id'],
            'generation': audio['generation'], 'expected_revision': audio['revision'],
            'command': 'resume', 'client_action_id': f'{key}:play:{index}'})
        assert result.status_code == 200, result.text
        audio = result.json()['media']
        for event, frame in [('started', 0), ('ended', audio['audio']['frames'])]:
            if event == 'ended':
                mono[0] += 1000
            result = client.post(base + '/playback', json={'media_id': audio['media_id'],
                'generation': audio['generation'], 'expected_revision': audio['revision'], 'event': event,
                'audio_sha256': audio['audio']['sha256'], 'frame_position': frame, 'monotonic_ms': mono[0],
                'client_action_id': f'{key}:play:{index}:{event}'})
            assert result.status_code == 200, result.text
            audio = result.json()['media']
    assert audio['qualification']['task_delivery_qualified']
    body = {'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
            'answer': answer, 'client_action_id': key + ':answer'}
    result = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'answered', result.json()
    assert result.json()['attempt']['score_status'] == expected_status, result.json()
    return result.json()['attempt'], body


def test_published_listening_answer_uses_frozen_complete_identity(tmp_path, monkeypatch):
    db, supply, spec, source = prepared(tmp_path)
    published = publish(supply, spec)
    sid = StudyRepository(db).start_session(CHILD, 'identity-session')['session_id']
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
        attempt, body = answer_published(client, db, tmp_path, monkeypatch, sid, published,
                                        source['expected_answer']['exact_answers'][0], 'original')
        with client.app.state.repository.database.connect() as c:
            item = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                (published['diagnostic_item_id'],)).fetchone())
            projection = project_child_mastery_evidence(c, CHILD)[item['knowledge_node_id']]
            receipt = next(row for row in projection['effective_evidence'] if row['evidence_id'] == attempt['evidence_id'])
            assert receipt['question_identity'] == question_identity(item), 'Frozen listening stimulus was lost'
            assert receipt['independent'] == 1
    with TestClient(create_app(settings)) as client:
        replay = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=body)
        assert replay.status_code == 200 and replay.json()['attempt']['attempt_id'] == attempt['attempt_id']
        with client.app.state.repository.database.connect() as c:
            receipt = project_child_mastery_evidence(c, CHILD)[item['knowledge_node_id']]['effective_evidence'][0]
            assert receipt['question_identity'] == question_identity(item)
            assert not project_child_mastery_evidence(c, 'child-junior-3')
    supply.withdraw(CHILD, published['candidate_id'], actor='parent', note='Original withdrawn',
                    action_id='identity-withdraw', expected_version=4)
    with Database(db.path, media_root=tmp_path).connect() as c:
        projection = project_child_mastery_evidence(c, CHILD)[item['knowledge_node_id']]
        assert not projection['effective_evidence']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('same_material', [False, True])
def test_explicit_source_revision_does_not_merge_different_recordings_or_erase_real_exposure(tmp_path, monkeypatch, same_material):
    first = packet()
    second = packet(instruction='Listen once, then answer.') if same_material else packet(
        stimulus_text='Mira, please wait at the blue gate.',
        expected_answer={'local_rule': 'local_exact_v1', 'exact_answers': ['at the blue gate']})
    actual_import = KnowledgeBaseService.import_source
    def import_packet_pair(service, child_id, payload):
        # Real NDJSON import into a single selected source. No source body SQL edits.
        return actual_import(service, child_id, {**payload, 'content_text': '\n'.join(
            json.dumps(value, ensure_ascii=False) for value in (first, second))})
    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeBaseService, 'import_source', import_packet_pair)
        db, supply, spec, _ = prepared(tmp_path)
    original = publish(supply, spec)
    with db.connect() as c:
        excerpts = c.execute('SELECT excerpt_id FROM knowledge_excerpt ORDER BY sequence').fetchall()
        assert len(excerpts) == 2
    sid = StudyRepository(db).start_session(CHILD, 'revision-session')['session_id']
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings)) as client:
        assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
        old_attempt, _ = answer_published(client, db, tmp_path, monkeypatch, sid, original, 'at the red bridge', 'first')
        # The publisher correctly rejects unrelated duplicate prompts. Use its
        # explicit revision path, which withdraws the original but keeps exposure.
        revised = supply.generate_draft(CHILD, {**spec, 'excerpt_id': excerpts[1][0]},
            author='local:reviewed_source_extractor_v1', action_id='revised-draft', expected_scope_version=1,
            supersedes=original['candidate_id'], supersedes_version=4)
        cid = revised['candidate_id']
        assert revised['state'] == 'draft', revised
        supply.validate(CHILD, cid, actor='parent', action_id='revised-validate', expected_version=1)
        supply.review(CHILD, cid, actor='parent', decision='approve', note='Checked the actual revised recording text',
                      action_id='revised-review', expected_version=2)
        revised = supply.publish(CHILD, cid, actor='parent', action_id='revised-publish', expected_version=3)
        assert revised['eligible']
        attempt, body = answer_published(client, db, tmp_path, monkeypatch, sid, revised, 'at the red bridge',
            'second', expected_status='practice_only' if same_material else 'verified_local')
        assert attempt['correctness'] == (1 if same_material else 0)
        with client.app.state.repository.database.connect() as c:
            new_item = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                (revised['diagnostic_item_id'],)).fetchone())
            old_item = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
                (original['diagnostic_item_id'],)).fetchone())
            assert (question_identity(_source_question(c, old_item)) == question_identity(_source_question(c, new_item))) == same_material
            projected = project_child_mastery_evidence(c, CHILD)[new_item['knowledge_node_id']]
            assert old_attempt['evidence_id'] not in {r['evidence_id'] for r in projected['effective_evidence']}
            if not same_material:
                receipt = next(r for r in projected['effective_evidence'] if r['evidence_id'] == attempt['evidence_id'])
                assert receipt['independent'] == 1 and receipt['correctness'] == 0
                reviews = [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (attempt['evidence_id'],))]
                assert {r['interval_label'] for r in reviews} == {'immediate', '48h', '7d'}
            else:
                assert any(r['evidence_id'] == attempt['evidence_id'] and r['reason'] == 'same_item_within_24h'
                           for r in projected['excluded_evidence'])
    with TestClient(create_app(settings)) as client:
        replay = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=body)
        assert replay.status_code == 200 and replay.json()['attempt']['attempt_id'] == attempt['attempt_id']
        with client.app.state.repository.database.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 2
            if not same_material:
                assert [dict(r) for r in c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (attempt['evidence_id'],))] == reviews


@pytest.mark.parametrize('fault', ['missing_source', 'missing_content', 'missing_hash', 'bad_hash',
    'payload_changed', 'source_changed', 'candidate_prompt', 'unknown_schema', 'missing_payload', 'wrong_excerpt'])
def test_invalid_frozen_listening_proof_cannot_fall_back_to_mutable_item(tmp_path, fault):
    db, supply, spec, _ = prepared(tmp_path)
    published = publish(supply, spec)
    with db.connect() as c:
        row = c.execute('SELECT payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',
            (published['candidate_id'],)).fetchone()
        payload, proof = json.loads(row[0]), json.loads(row[1])
        item = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
            (published['diagnostic_item_id'],)).fetchone())
        if fault == 'missing_source':
            proof.pop('source')
        elif fault == 'missing_content':
            proof['source'].pop('content_text')
        elif fault == 'missing_hash':
            proof['source'].pop('excerpt_hash')
        elif fault == 'bad_hash':
            proof['source']['excerpt_hash'] = '0' * 64
        elif fault == 'payload_changed':
            payload['pet_listening']['stimulus_text'] = 'Another recording.'
        elif fault == 'source_changed':
            changed = json.loads(proof['source']['content_text'])
            changed['stimulus_text'] = 'Another recording.'
            text = json.dumps(changed)
            proof['source'].update(content_text=text, excerpt_hash=hashlib.sha256(text.encode()).hexdigest())
        elif fault == 'candidate_prompt':
            c.execute('UPDATE specialized_supply_candidate SET prompt=? WHERE candidate_id=?',
                ('Another question.', published['candidate_id']))
        elif fault == 'missing_payload':
            payload.pop('pet_listening')
        elif fault == 'wrong_excerpt':
            proof['source']['excerpt_id'] = 'another-excerpt'
        else:
            payload['pet_listening']['schema'] = 'unknown-listening'
        c.execute('UPDATE curriculum_supply_package SET payload_json=?,source_snapshot_json=? WHERE candidate_id=?',
            (json.dumps(payload), json.dumps(proof), published['candidate_id']))
        with pytest.raises(ValueError):
            _source_question(c, item)
        assert project_item_exposure(c, CHILD, item['diagnostic_item_id'], as_of=item['created_at']
            if 'created_at' in item else '2026-09-09T00:00:00+00:00')['question_identity'] is None


def test_mutable_item_cannot_rewrite_frozen_original_identity(tmp_path):
    from home_study_agent.local_supply import LocalSupplyError, validate_item
    db, supply, spec, _ = prepared(tmp_path)
    published = publish(supply, spec)
    with db.connect() as c:
        item = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
            (published['diagnostic_item_id'],)).fetchone())
        original = _source_question(c, item)
        assert question_identity(original) == question_identity(item)
        c.execute('UPDATE diagnostic_item SET stimulus_text=? WHERE diagnostic_item_id=?',
            ('An unauthorized changed recording.', item['diagnostic_item_id']))
        c.commit()
    with Database(db.path).connect() as c:
        changed = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?',
            (item['diagnostic_item_id'],)).fetchone())
        assert _source_question(c, changed) == original
        assert question_identity(changed) != question_identity(original)
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, item['diagnostic_item_id'])
