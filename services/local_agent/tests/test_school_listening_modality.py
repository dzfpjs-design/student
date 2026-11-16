"""Isolated school listening contracts; synthetic PCM is not real listening content proof."""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import LocalSupplyError, validate_item
from home_study_agent.pet_reviewed_source import VALIDATOR, parse_listening_source
from home_study_agent.pet_skill_gate import pet_skill_gate
from home_study_agent.repository import StudyRepository
from test_pet_listening_resource_admission import bind_audio
from test_pet_reviewed_source_contract import packet


CHILD = 'child-junior-3'
NODE = 'node-j3-english-listening'


def school_packet(**changes):
    return packet(schema='school_listening_source_v1', subject='英语', grade='初三',
                  knowledge_node_id=NODE, **changes)


def prepared_school(root, source=None):
    source = source or school_packet()
    db = Database(root / 'school-listening.db', media_root=root)
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    imported = KnowledgeBaseService(db).import_source(CHILD, {
        'source_type': 'school_material', 'title': 'Isolated original school listening',
        'subject': '英语', 'grade_band': '初三', 'version_label': 'isolated-2026',
        'source_ref': 'isolated-fixture:school-listening', 'license_status': 'public',
        'parent_reviewed': True, 'content_text': json.dumps(source, ensure_ascii=False)})
    quote = '理解简短英语对话并提取明确地点信息。'
    with db.connect() as c:
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (imported['source_id'],)).fetchone()[0]
        c.execute('''INSERT INTO curriculum_standard_source(source_id,source_key,jurisdiction,source_type,
            subject_title,title,version_label,official_url,local_root_path,status,imported_at,updated_at)
            VALUES('school-listening-std','isolated-school-listening','CN','national_standard','英语',
                   'Isolated school scope','2022','isolated-fixture:school-standard','','structured','2026-09-01','2026-09-01')''')
        c.execute("INSERT INTO curriculum_standard_page VALUES('school-listening-std',12,?,'fixture-hash',20)", (quote,))
        c.execute('''INSERT INTO curriculum_standard_requirement(requirement_id,source_id,subject_title,stage_scope,
            requirement_type,title,requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('school-listening-req','school-listening-std','英语','第四学段','content_requirement',
                   'Listening detail',?,'Exact place',12,12,.9,'verified','2026-09-01','2026-09-01')''', (quote,))
        c.commit()
    supply = CurriculumSupplyService(db)
    supply.select_curriculum(CHILD, {'grade': '初三', 'subject': '英语', 'edition': 'isolated-2026',
        'semester': '上学期', 'standard_scope': '第四学段', 'textbook_source_id': imported['source_id']},
        actor='parent', action_id='school-scope', expected_version=0)
    spec = {'seed_item_id': 'diag-j3-english-listening-baseline', 'stage': 'parallel', 'excerpt_id': excerpt,
        'requirement_id': 'school-listening-req', 'standard_page': 12, 'standard_quote': quote,
        'validator': VALIDATOR, 'explanation': 'The recording gives the complete place.', 'difficulty': 1}
    return db, supply, spec, source, imported['source_id']


def publish_school(supply, spec):
    result = supply.generate_draft(CHILD, spec, author='local:reviewed_source_extractor_v1',
        action_id='school-draft', expected_scope_version=1)
    assert result['state'] == 'draft', result
    cid = result['candidate_id']
    supply.validate(CHILD, cid, actor='parent', action_id='school-check', expected_version=1)
    supply.review(CHILD, cid, actor='parent', decision='approve', note='Checked complete question, original script and playback instruction',
        action_id='school-review', expected_version=2)
    return supply.publish(CHILD, cid, actor='parent', action_id='school-publish', expected_version=3)


def play(client, sid, audio, mono, key):
    base = f'/v1/sessions/{sid}/pet-media/listening'
    for index in range(audio['qualification']['required_play_count']):
        result = client.post(base + '/control', json={'media_id': audio['media_id'],
            'generation': audio['generation'], 'expected_revision': audio['revision'], 'command': 'resume',
            'client_action_id': f'{key}:{index}:resume'})
        assert result.status_code == 200, result.text
        audio = result.json()['media']
        for event, frame in [('started', 0), ('ended', audio['audio']['frames'])]:
            if event == 'ended':
                mono[0] += 1000
            result = client.post(base + '/playback', json={'media_id': audio['media_id'],
                'generation': audio['generation'], 'expected_revision': audio['revision'], 'event': event,
                'audio_sha256': audio['audio']['sha256'], 'frame_position': frame, 'monotonic_ms': mono[0],
                'client_action_id': f'{key}:{index}:{event}'})
            assert result.status_code == 200, result.text
            audio = result.json()['media']
    assert audio['qualification']['task_delivery_qualified']
    return audio


@pytest.mark.parametrize('review_interval', ['immediate', '48h'])
def test_school_source_options_publish_audio_gate_recovery_review_and_withdraw(tmp_path, monkeypatch, review_interval):
    db, supply, spec, source, source_id = prepared_school(tmp_path)
    published = publish_school(supply, spec)
    item_id = published['diagnostic_item_id']
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    mono = [1000]
    monkeypatch.setattr('home_study_agent.pet_media_evidence._clock_ms', lambda: mono[0])
    with TestClient(create_app(settings), client=('127.0.0.1', 50000)) as client:
        assert client.post('/v1/parent/setup', json={'pin': '2468'}).status_code == 200
        token = client.post('/v1/parent/unlock', json={'pin': '2468'}).json()['parent_token']
        options = client.get(f'/api/parent/children/{CHILD}/curriculum-supply/sources/{source_id}/options',
                             headers={'X-HSC-Parent': token})
        assert options.status_code == 200, options.text
        assert any(row['seed_item_id'] == spec['seed_item_id'] and row['validator'] == VALIDATOR for row in options.json()['seeds'])
        package = client.get(f"/api/parent/children/{CHILD}/curriculum-supply/{published['candidate_id']}",
                             headers={'X-HSC-Parent': token})
        assert package.status_code == 200, package.text
        assert package.json()['payload']['pet_listening'] == source
        sid = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'school-session'}).json()['session_id']
        result = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'school', 'preferred_item_id': item_id, 'client_action_id': 'school-prepare'})
        assert result.status_code == 200, result.text
        prompt = result.json()['prompt']
        assert prompt['item']['answer_type'] == 'listening_text' and prompt['item']['pet_audio_modality'] == 'listening'
        assert prompt['item']['stimulus_text'] is None and source['stimulus_text'] not in result.text
        parent_prompts = client.get(f'/v1/parent/children/{CHILD}/pet-prompts', headers={'X-HSC-Parent': token})
        assert parent_prompts.status_code == 200, parent_prompts.text
        original, = parent_prompts.json()['prompts']
        assert original['item']['track'] == 'school' and original['item']['stimulus_text'] == source['stimulus_text']
        with db.connect() as c:
            assert not pet_skill_gate(c, CHILD, item_id)['skill_scoring_allowed']
        audio, _ = bind_audio(client, db, tmp_path, sid, prompt, approved=False, key='school-original')
        refused = client.get(f"/v1/sessions/{sid}/pet-media/{audio['media_id']}/audio",
                             params={'audio_sha256': audio['audio']['sha256']})
        assert refused.status_code == 409, refused.text
        review = client.post(f"/v1/sessions/{sid}/pet-media/{audio['media_id']}/source-review", headers={'X-HSC-Parent': token},
            json={'audio_sha256': audio['audio']['sha256'], 'operation': 'approve', 'client_action_id': 'school-audio-review'})
        assert review.status_code == 200, review.text
        audio = play(client, sid, review.json()['media'], mono, 'school-play')
        body = dict(prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                    answer=source['expected_answer']['exact_answers'][0], client_action_id='school-answer')
        result = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=body)
        assert result.status_code == 200, result.text
        attempt = result.json()['attempt']
        assert attempt['correctness'] == 1 and attempt['score_status'] == 'verified_local', attempt
        with db.connect() as c:
            reviews = list(c.execute('SELECT * FROM review_task WHERE source_evidence_id=?', (attempt['evidence_id'],)))
            assert {row['interval_label'] for row in reviews} == {'immediate', '48h', '7d'}
            from home_study_agent.pet_task_execution import freeze_listening_review
            frozen = freeze_listening_review(c, CHILD, reviews[0]['review_task_id'])
            assert frozen['skill'] == 'listening' and frozen['diagnostic_item_id'] == item_id
            assert c.execute("SELECT count(*) FROM diagnostic_attempt WHERE child_id='child-primary-4'").fetchone()[0] == 0
    with TestClient(create_app(settings), client=('127.0.0.1', 50000)) as client:
        restored = client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()
        assert restored['attempt']['attempt_id'] == attempt['attempt_id'] and source['stimulus_text'] not in str(restored['prompt'])
        replay = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json=body)
        assert replay.status_code == 200 and replay.json()['attempt']['attempt_id'] == attempt['attempt_id']
        # A scheduled review is a new exact prompt: borrow only the still-reviewed
        # recording, never the earlier playback or its scored answer receipt.
        review_task = next(row for row in reviews if row['interval_label'] == review_interval)
        review_id = review_task['review_task_id']
        if review_interval == '48h':
            from test_writing_stage_review_consumers import advance_clock
            advance_clock(monkeypatch, datetime.fromisoformat(review_task['due_at']) + timedelta(seconds=1))
        reviewed = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'school', 'preferred_item_id': item_id, 'review_task_id': review_id,
            'client_action_id': 'school-immediate-review'})
        assert reviewed.status_code == 200, reviewed.text
        review_prompt = reviewed.json()['prompt']
        assert review_prompt['prompt_id'] != prompt['prompt_id'] and review_prompt['item']['stimulus_text'] is None
        from home_study_agent.pet_listening_reuse import bind_reviewed_listening_resource
        from home_study_agent.pet_media_evidence import project_pet_media_evidence
        with client.app.state.repository.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            reused = bind_reviewed_listening_resource(c, sid, CHILD, review_prompt['prompt_id'], review_prompt['item_version'])
            c.commit()
            assert reused['status'] == 'ready'
            fresh = project_pet_media_evidence(c, sid, media_id=reused['media_id'], media_root=tmp_path)
            assert fresh['source_reviewed'] and not fresh['qualification']['task_delivery_qualified']
            assert not pet_skill_gate(c, CHILD, item_id, session_id=sid,
                prompt_id=review_prompt['prompt_id'], item_version=review_prompt['item_version'],
                media_root=tmp_path)['skill_scoring_allowed']
        play(client, sid, fresh, mono, 'school-review-play')
        reviewed_answer = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
            **body, 'prompt_id': review_prompt['prompt_id'], 'item_version': review_prompt['item_version'],
            'client_action_id': 'school-review-answer'})
        assert reviewed_answer.status_code == 200, reviewed_answer.text
        review_attempt = reviewed_answer.json()['attempt']
        assert review_attempt['correctness'] == 1
        assert review_attempt['score_status'] == ('practice_only' if review_interval == 'immediate' else 'verified_local')
        assert review_attempt['attempt_id'] != attempt['attempt_id'] and review_attempt['review_task_id'] == review_id
        with db.connect() as c:
            assert c.execute('SELECT status FROM review_task WHERE review_task_id=?', (review_id,)).fetchone()[0] == 'completed'
    supply.withdraw(CHILD, published['candidate_id'], actor='parent', note='Original source withdrawn',
                    action_id='school-withdraw', expected_version=4)
    with db.connect() as c:
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, item_id)
        assert not pet_skill_gate(c, CHILD, item_id)['skill_scoring_allowed']
        from home_study_agent.learning_obligations import project_child_mastery_evidence
        projection = project_child_mastery_evidence(c, CHILD)[NODE]
        assert not projection['effective_evidence']
        assert {row['evidence_id'] for row in projection['excluded_evidence']} >= {attempt['evidence_id'], review_attempt['evidence_id']}
    with TestClient(create_app(settings), client=('127.0.0.1', 50000)) as client:
        historical = client.get(f'/v1/sessions/{sid}/diagnostic-prompt').json()
        assert historical['status'] == 'answered' and source['stimulus_text'] not in str(historical['prompt'])
        media = client.get(f"/v1/sessions/{sid}/pet-media/{reused['media_id']}").json()['media']
        assert media['status'] == 'invalidated' and not media['qualification']['skill_scoring_allowed']
        refused = client.get(f"/v1/sessions/{sid}/pet-media/{reused['media_id']}/audio",
                             params={'audio_sha256': fresh['audio']['sha256']})
        assert refused.status_code == 409


@pytest.mark.parametrize('changes', [{'grade': '四年级'}, {'subject': '数学'},
    {'knowledge_node_id': 'node-pet-listening-detail'}, {'schema': 'pet_listening_source_v1'},
    {'prompt': 'Mira, please wait at the red bridge.'}])
def test_school_source_scope_or_transcript_cannot_be_smuggled_into_pet_or_question(tmp_path, changes):
    source = {**school_packet(), **changes}
    from home_study_agent.knowledge_base import InvalidKnowledgeSourceError
    try:
        db, supply, spec, _, _ = prepared_school(tmp_path, source)
        result = supply.generate_draft(CHILD, spec, author='local:extractor', action_id='bad-school-source', expected_scope_version=1)
    except (LocalSupplyError, InvalidKnowledgeSourceError):
        return
    assert result['state'] == 'blocked'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_catalog_upgrade_hides_shipped_transcript_and_keeps_unheard_baseline_unqualified(tmp_path):
    db, _, _, _, _ = prepared_school(tmp_path)
    from home_study_agent.specialized_catalog import LEGACY_SCHOOL_LISTENING_PROMPT
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt=?,answer_type='text',stimulus_text=NULL WHERE diagnostic_item_id='diag-j3-english-listening-baseline'",
                  (LEGACY_SCHOOL_LISTENING_PROMPT,))
        c.commit()
    LearningStateService(db).seed_catalog()
    item = LearningStateService(db).next_diagnostic(CHILD, 'school', preferred_item_id='diag-j3-english-listening-baseline')
    assert item['answer_type'] == 'listening_text' and item['pet_audio_modality'] == 'listening'
    assert item['stimulus_text'] is None and 'The bus leaves' not in item['prompt']
    with db.connect() as c:
        assert not pet_skill_gate(c, CHILD, item['diagnostic_item_id'])['skill_scoring_allowed']


def test_school_schema_roundtrip_keeps_explicit_scope():
    assert parse_listening_source(json.dumps(school_packet())) == school_packet()
