"""A repair invalidates real published/reviewed learning, retaining raw answers."""
import hashlib
import json
from uuid import uuid4

from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.writing_question_authorization import WritingQuestionAuthorizationService
from test_writing_source_publication import prepared, publish, CHILD
from test_writing_question_authorization import LICENSE


def test_partial_document_repair_revokes_old_question_permission_and_reviewed_stage(tmp_path):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.repository import StudyRepository
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.writing_response_origin import WritingOriginalService, bind_written_attempt
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.writing_rubric import prepare_writing_review
    from test_pet_media_evidence import local_review
    from test_specialized_media_stage import node
    db, supply, spec, packet = prepared(tmp_path)
    second = {**packet, 'prompt': packet['prompt'] + ' Additional original condition.' * 30}
    first_text, second_text = (json.dumps(p, ensure_ascii=False) for p in (packet, second))
    original_text = first_text + '\n' + second_text
    # Reconstruct a real legacy partial NDJSON import: first item complete,
    # second item truncated, full original hash. No invented eligibility rows.
    with db.connect() as c:
        source_id = c.execute('SELECT source_id FROM knowledge_excerpt WHERE excerpt_id=?', (spec['excerpt_id'],)).fetchone()[0]
        c.execute('UPDATE knowledge_source SET content_hash=? WHERE source_id=?',
            (hashlib.sha256(original_text.encode()).hexdigest(), source_id))
        c.execute('INSERT INTO knowledge_excerpt VALUES (?,?,2,NULL,?,?)',
            (str(uuid4()), source_id, second_text[:1000], hashlib.sha256(second_text[:1000].encode()).hexdigest()))
        c.commit()
    published = publish(supply, spec)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    authority = WritingQuestionAuthorizationService(db)
    authority.initialize()
    view = authority.read(CHILD, diagnostic_item_id=published['diagnostic_item_id'])
    granted = authority.grant(CHILD, diagnostic_item_id=published['diagnostic_item_id'],
        **{k: view[k] for k in ('item_version', 'source_version', 'curriculum_version')},
        license_basis=LICENSE, expected_revision=0, client_action_id='legacy-grant', actor_id='authenticated-parent')
    assert granted['current']['status'] == 'authorized'
    sid = StudyRepository(db).start_session(CHILD, 'writing')['session_id']
    prompts = DiagnosticPromptService(db)
    p = prompts.prepare(sid, track='pet', preferred_item_id=published['diagnostic_item_id'], client_action_id='ask')['prompt']
    text = 'Dear Ben, our school concert was in the hall on Friday. I enjoyed the violin performance because the music was cheerful. Would you like to come with me to our next concert in June? Best wishes, Mia.'
    original = WritingOriginalService(db).capture_native_response(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], text=text, client_action_id='write')
    answer = prompts.answer(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], answer=text, client_action_id='answer')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind_written_attempt(c, child_id=CHILD, session_id=sid, prompt_id=p['prompt_id'], item_version=p['item_version'],
            attempt_id=answer['attempt']['attempt_id'], original_artifact_id=original['artifact_id'])
        c.commit()
    assessment = local_review((db, None, sid, p, None, None), p, text, approve=False, scored=answer)
    rubric = RubricEvidenceService(db)
    with db.connect() as c:
        binding = prepare_writing_review(c, CHILD, assessment['assessment_id'])
    rubric.review(CHILD, assessment['assessment_id'], expected_revision=0, client_action_id='review-writing',
        reviewer_id='parent', reviewer_role='parent', operation='approve', reason='Synthetic original manuscript review',
        independent=True, hint_level=0, review_profile='pet_writing_text_v1', writing_evidence={'binding_version': binding['binding_version'],
            'task_point_evidence': [{'task_point': v, 'response_quote': text} for v in binding['task_points']]},
        dimensions=[dict(dimension_id=d['dimension_id'], score=3, response_quotes=[text], rationale='Reviewed exact original') for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=v, response_quote=text) for v in assessment['criteria']])
    assert node(db, 'node-pet-writing-email')['qualified_stage'] == 'baseline'
    with db.connect() as c:
        before = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (answer['attempt']['attempt_id'],)).fetchone())
    revisions = KnowledgeSourceRevisionService(db, replay=LearningStateService(db).replay_mastery)
    preview = revisions.preview(CHILD, source_id, original_text)
    revised = revisions.revise(CHILD, source_id, content_text=original_text, expected_version=preview['source_version'], client_action_id='repair-partial')
    assert node(db, 'node-pet-writing-email')['qualified_stage'] is None
    assert authority.read(CHILD, diagnostic_item_id=published['diagnostic_item_id'])['status'] != 'authorized'
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (answer['attempt']['attempt_id'],)).fetchone()) == before
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?', (assessment['assessment_id'],)).fetchone()[0] == 1
        assert not c.execute('SELECT 1 FROM writing_question_authorization_event WHERE source_id=?', (revised['current']['source_id'],)).fetchone()
    revisions.review(CHILD, revised['current']['source_id'], expected_version=revised['source_version'], client_action_id='new-source-review')
    assert node(db, 'node-pet-writing-email')['qualified_stage'] is None
    assert authority.read(CHILD, diagnostic_item_id=published['diagnostic_item_id'])['status'] != 'authorized'
