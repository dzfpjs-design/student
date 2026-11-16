"""Actual local HTTP oral review/source lifecycle; PCM is synthetic, not speech quality proof."""
import base64
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.knowledge_base import source_lines
from home_study_agent.local_media_ai import LocalMediaAI
from home_study_agent.specialized_stages import project_specialized_stages
from test_agent_continuation import Gateway
from test_diagnostic_prompt_http import start
from test_pet_media_evidence import local_review, wav_file
from test_pet_oral_supply_http import setup, packet, post, BASE, CHILD, OTHER, FAMILIES, VALIDATOR
from test_pet_typed_source_import import values
from test_voice_runtime_binding import capture, interpret, submit


def get(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:2000]
    return response.json()


def author(client, db, *, legacy):
    original = packet(db, 'topic_response')
    text = json.dumps(original, ensure_ascii=False)
    source = post(client, f'/v1/parent/children/{CHILD}/knowledge-sources', values(text))
    if legacy:
        # Reconstruct the actual old per-line 1000-character truncation format.
        # The first complete item is genuinely publishable; no qualification is seeded.
        assert len(text) <= 1000
        second = {**original, 'prompt': 'Describe a second outdoor visit.',
                  'stimulus_text': 'A synthetic second source detail. ' * 60}
        second_text = json.dumps(second, ensure_ascii=False)
        text += '\n' + second_text
        assert len(source_lines(text)[1]) == 2 and len(second_text) > 1000
        with db.connect() as c:
            c.execute('UPDATE knowledge_source SET content_hash=? WHERE source_id=?',
                      (hashlib.sha256(text.encode()).hexdigest(), source['source_id']))
            c.execute('INSERT INTO knowledge_excerpt VALUES (?,?,2,NULL,?,?)',
                      (uuid4().hex, source['source_id'], second_text[:1000],
                       hashlib.sha256(second_text[:1000].encode()).hexdigest()))
            c.commit()
    # A separate owner's same-content source must never be superseded or erased.
    other_source = post(client, f'/v1/parent/children/{OTHER}/knowledge-sources', values(text))
    options = get(client, BASE + f"/sources/{source['source_id']}/options")
    standard = next(s for s in options['standards'] if s['requirement_id'] == 'c03-req')
    selected = post(client, BASE + '/select', dict(action_id='select', expected_version=0,
        scope=dict(grade='四年级', subject='英语', edition='isolated-c03-v1', semester='上学期',
                   standard_scope=standard['stage_scope'], textbook_source_id=source['source_id'])))
    spec = dict(seed_item_id=FAMILIES['topic_response'], stage='parallel',
        excerpt_id=options['excerpts'][0]['excerpt_id'], requirement_id='c03-req',
        standard_page=standard['page_number'], standard_quote=standard['quote'],
        validator=VALIDATOR, difficulty=1, explanation='Synthetic source review, not a speech score.')
    result = post(client, BASE + '/generate', dict(action_id='generate', expected_version=0,
        expected_scope_version=selected['version'], source=spec))
    for version, operation in enumerate(('validate', 'review', 'publish'), 1):
        body = dict(action_id=operation, expected_version=version)
        if operation == 'review':
            body.update(decision='approve', note='Synthetic authored source only')
        result = post(client, BASE + f"/{result['candidate_id']}/{operation}", body)
    assert result['eligible']
    return original, source, other_source, text, result


def record_and_review(client, db, settings, monkeypatch, item):
    sid = start(client)
    prompt = post(client, f'/v1/sessions/{sid}/diagnostic-prompt/prepare',
        dict(track='pet', preferred_item_id=item['diagnostic_item_id'], client_action_id='ask-oral'))['prompt']
    answer = 'I would like to visit the garden because it is quiet. I would walk there with my family and enjoy the flowers after school.'
    pcm = settings.data_dir / 'synthetic-original.wav'
    wav_file(pcm)
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav',
                        lambda *args, **kwargs: (answer, 1000, 'synthetic-local-recognizer', False))
    captured = post(client, f'/v1/sessions/{sid}/asr', dict(
        wav_base64=base64.b64encode(pcm.read_bytes()).decode(), purpose='pet_speaking',
        client_action_id='oral-audio', capture_reference={'child_id': CHILD,
            'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}}))
    assert captured['capture_binding']['raw_audio_available']
    voice_id = captured['voice_input_id']
    heard = interpret(client, sid, answer, 'original-answer', voice_id)
    if heard['intent'] == 'pending_answer_clarification':
        post(client, f"/v1/agent-turns/{heard['agent_turn_id']}/execute", {})
        confirmation = '刚才那句就是我的答案'
        confirmed_voice = capture(client, settings, monkeypatch, sid, confirmation, prompt, 'confirm-audio')
        heard = interpret(client, sid, confirmation, 'confirm-answer', confirmed_voice)
    assert heard['action']['source_voice_input_id'] == voice_id
    saved = submit(client, sid, prompt, heard, answer=answer, key='save-oral')
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    assert saved['status'] == 'answered'
    # Existing helper creates only the real local evaluator's candidate packet.
    # It does not insert or approve a rubric review; all authority below is HTTP.
    assessment = local_review((db, None, sid, prompt, pcm, None), prompt, answer,
                              approve=False, scored=saved)
    path = f"/v1/parent/children/{CHILD}/learning-rubrics/{assessment['assessment_id']}"
    before = get(client, path)
    assert not before['qualification']['eligible_for_mastery_gate']
    binding = before['audio_review']['bindings'][0]
    audio = client.get(path + '/audio', params={'media_id': binding['media_id'],
                       'audio_sha256': binding['audio']['sha256']})
    assert audio.status_code == 200, audio.text[:500]
    assert audio.content == pcm.read_bytes() and audio.headers['cache-control'] == 'no-store'
    body = dict(expected_revision=0, client_action_id='human-original-review', operation='approve',
        reason='Synthetic original-audio review fixture, not evidence about a real child.',
        independent=True, hint_level=0, review_profile='pet_speaking_audio_v1',
        audio_evidence={'media_id': binding['media_id'], 'review': {
            'schema': 'pet_speaking_audio_references_v1', 'binding_version': binding['binding_version'],
            'audio_sha256': binding['audio']['sha256'], 'observations': [dict(dimension_id=name,
                start_frame=0, end_frame=binding['audio']['frames'],
                heard_text='Synthetic fixture annotation; PCM is not real speech.',
                rationale='Fixture verifies exact original-frame transport and version binding.')
                for name in binding['dimensions_required']]}},
        dimensions=[dict(dimension_id=d['dimension_id'], score=3,
            response_quotes=[answer] if d['dimension_id'] in {'task', 'coherence', 'language'} else [],
            rationale='Synthetic human judgment tests source authority, not recognition accuracy.')
            for d in before['audio_review']['dimensions']],
        criterion_evidence=[dict(criterion=v, response_quote=answer) for v in assessment['criteria']])
    assert client.post(path + '/review', json=body,
        headers={'X-HSC-Parent': ''}).status_code == 401
    approved = post(client, path + '/review', body)
    assert approved['qualification']['eligible_for_mastery_gate'], approved
    assert post(client, path + '/review', body)['event'] == approved['event']
    return sid, prompt, saved, path, body


def facts(db, child):
    with db.connect() as c:
        return {table: [dict(r) for r in c.execute(f'SELECT * FROM {table} WHERE child_id=? ORDER BY rowid', (child,))]
                for table in ('diagnostic_attempt', 'learning_evidence', 'rubric_review_event')}


def other_state(db, as_of):
    with db.connect() as c:
        sources = [dict(r) for r in c.execute('SELECT * FROM knowledge_source WHERE child_id=? ORDER BY source_id', (OTHER,))]
        excerpts = [dict(r) for r in c.execute('SELECT ke.* FROM knowledge_excerpt ke JOIN knowledge_source ks USING(source_id) WHERE ks.child_id=? ORDER BY excerpt_id', (OTHER,))]
        stages = project_specialized_stages(c, OTHER, 'junior_exam', as_of=as_of)
        return sources, excerpts, stages, facts(db, OTHER)


def projections(db, sid, prompt):
    with db.connect() as c:
        stages = project_specialized_stages(c, CHILD, 'pet')
    stage = next(n for d in stages['domains'] for n in d['nodes']
                 if n['knowledge_node_id'] == prompt['item']['knowledge_node_id'])
    teaching = ChildModelService(db).tutor_controls(CHILD, 1, None, {}, session_id=sid,
        subject='英语', question=prompt['item']['prompt'], prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'])['audit']['teaching_context']
    return stage, teaching


@pytest.mark.parametrize('operation', ['withdraw_current', 'repair_legacy'])
def test_actual_oral_human_review_loses_authority_after_source_change_and_restart(tmp_path, monkeypatch, operation):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'C' * 32)
    db = Database(initial.path, media_root=settings.data_dir,
                  data_encryption_key=settings.data_encryption_key)
    gateway = Gateway({})
    gateway.configured = False
    local = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=gateway), headers=local,
                    client=('127.0.0.1', 41909)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        preference = client.put('/v1/parent/privacy', json={
            'raw_media_storage_allowed': True, 'cloud_model_text_allowed': False,
            'cloud_audio_allowed': False, 'model_improvement_allowed': False})
        assert preference.status_code == 200, preference.text
        original, source, other_source, text, published = author(client, db, legacy=operation == 'repair_legacy')
        sid, prompt, saved, path, review_body = record_and_review(client, db, settings, monkeypatch, published)
        stage, teaching = projections(db, sid, prompt)
        assert stage['qualified_stage'] == 'baseline', stage
        assert teaching['learning']['status'] == 'independent_observed', teaching
        assert not stage['mastery_claim_allowed'] and not teaching['mastery_claim_allowed']
        original_facts = facts(db, CHILD)
        as_of = datetime.now(timezone.utc).isoformat()
        other_before = other_state(db, as_of)
        root = f"/v1/parent/children/{CHILD}/knowledge-sources/{source['source_id']}/revision"
        preview = post(client, root + '/preview', {'content_text': text})
        revision_body = dict(content_text=text, expected_version=preview['source_version'],
            client_action_id='explicit-repair', confirmation='保留旧稿并创建待审核新版本')
        if operation == 'withdraw_current':
            # A valid current typed source is not a repair fixture.
            rejected = client.post(root, json=revision_body)
            assert rejected.status_code == 409 and 'SOURCE_REVISION_ALREADY_COMPLETE' in rejected.text
            assert facts(db, CHILD) == original_facts
            post(client, BASE + f"/{published['candidate_id']}/withdraw", dict(
                action_id='withdraw-source', expected_version=published['version'], note='Synthetic source withdrawn.'))
        else:
            repaired = post(client, root, revision_body)
            assert repaired['current']['review_status'] == 'quarantined'
            assert repaired['current']['source_id'] != source['source_id']
            assert not get(client, path)['qualification']['eligible_for_mastery_gate']
            assert projections(db, sid, prompt)[0]['qualified_stage'] is None
            assert facts(db, CHILD) == original_facts
            imported = post(client, f'/v1/parent/children/{CHILD}/knowledge-sources', values(text))
            assert imported['source_id'] == repaired['current']['source_id']
            assert imported['review_status'] == 'quarantined'
            assert post(client, root, revision_body)['receipt'] == repaired['receipt']
            old = get(client, root)
            assert old['current']['current_version'] == 0
            assert old['current']['review_status'] == 'superseded'
            assert old['stored_excerpts'][0]['text'] == json.dumps(original, ensure_ascii=False)
            new_root = root.replace(source['source_id'], repaired['current']['source_id'])
            post(client, new_root + '/review', dict(expected_version=repaired['source_version'],
                client_action_id='separate-source-review', confirmation='已核对新版完整内容与来源'))
        current = get(client, path)
        assert not current['qualification']['eligible_for_mastery_gate'], current
        stage, teaching = projections(db, sid, prompt)
        assert stage['qualified_stage'] is None, stage
        assert teaching['learning']['status'] == 'unknown', teaching
        assert facts(db, CHILD) == original_facts
        assert other_state(db, as_of) == other_before
        assert client.get(path.replace(CHILD, OTHER)).status_code in {404, 409}
        assert client.get(root.replace(CHILD, OTHER)).status_code == 404
        assert not get(client, BASE + '/' + published['candidate_id'])['eligible']
    with TestClient(create_app(settings, model_gateway=gateway), headers=local,
                    client=('127.0.0.1', 41910)) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        restored = get(client, path)
        assert not restored['qualification']['eligible_for_mastery_gate']
        assert restored['history'][0]['audio_evidence'] == review_body['audio_evidence']
        assert facts(db, CHILD) == original_facts
        assert other_state(db, as_of) == other_before
        assert projections(db, sid, prompt)[0]['qualified_stage'] is None
        if operation == 'repair_legacy':
            assert post(client, root, revision_body)['current']['review_status'] == 'approved'
            assert not get(client, path)['qualification']['eligible_for_mastery_gate']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM model_call').fetchone()[0] == 0
    assert not gateway.requests
