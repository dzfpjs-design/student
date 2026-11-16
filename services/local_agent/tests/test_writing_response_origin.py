"""Synthetic written originals test provenance, not real writing proficiency."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_response_origin import (
    WritingOriginalService, bind_written_attempt, original_written_response,
)

CHILD = 'child-primary-4'
TEXT = 'Dear Alex, please meet me at the library on Saturday morning at ten. We can study English together and practise our reading. Bring your notebook and a pencil. I hope to see you there. Best wishes, Mia.'


@pytest.fixture
def writing(tmp_path):
    db = Database(tmp_path / 'writing.db')
    db.initialize()
    from home_study_agent.period_reports import PeriodReportService
    PeriodReportService(db).initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    sid = repo.start_session(CHILD, 'start')['session_id']
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid, track='pet', preferred_item_id='diag-pet-writing-1', client_action_id='ask')['prompt']
    return db, sid, prompts, prompt, WritingOriginalService(db)


def capture(writing, **overrides):
    _, sid, _, prompt, service = writing
    return service.capture_native_response(sid, **dict(prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        text=TEXT, client_action_id='original', **overrides))


def answer(writing, text=TEXT):
    _, sid, prompts, p, _ = writing
    return prompts.answer(sid, prompt_id=p['prompt_id'], item_version=p['item_version'], answer=text, client_action_id='answer')


def bind(c, writing, original):
    _, sid, _, p, _ = writing
    attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()
    return bind_written_attempt(c, child_id=CHILD, session_id=sid, prompt_id=p['prompt_id'], item_version=p['item_version'],
                                attempt_id=attempt['attempt_id'], original_artifact_id=original['artifact_id'])


def test_original_reference_transaction_restart_exact_retry_and_no_new_score(writing):
    db, sid, _, p, _ = writing
    original = capture(writing)
    assert capture(writing) == original
    answer(writing)
    with db.connect() as c:
        with pytest.raises(ValueError, match='TRANSACTION_REQUIRED'): bind(c, writing, original)
        c.execute('BEGIN IMMEDIATE')
        result = bind(c, writing, original)
        assert bind(c, writing, original) == result
        c.commit()
    restarted = Database(db.path)
    with restarted.connect() as c:
        prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (p['prompt_id'],)).fetchone()
        attempt = c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        assert original_written_response(c, prompt, attempt) == result
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='writing_original'").fetchone()[0] == 1
    assert WritingOriginalService(restarted).capture_native_response(sid, prompt_id=p['prompt_id'],
        item_version=p['item_version'], text=TEXT, client_action_id='original') == original


@pytest.mark.parametrize('fault', ['response', 'withdrawn', 'question', 'child', 'late', 'voice'])
def test_written_source_changes_and_voice_never_gain_qualification(writing, fault):
    db, _, _, p, _ = writing
    original = capture(writing)
    answer(writing)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if fault == 'response': c.execute("UPDATE capture_artifact SET extracted_text='a confirmation'")
        elif fault == 'withdrawn': c.execute("UPDATE capture_artifact SET status='withdrawn'")
        elif fault == 'question': c.execute("UPDATE diagnostic_item SET prompt='different question' WHERE diagnostic_item_id=?", (p['diagnostic_item_id'],))
        elif fault == 'child': c.execute("UPDATE capture_artifact SET child_id='child-junior-3'")
        elif fault == 'late': c.execute("UPDATE capture_artifact SET created_at='2099-01-01T00:00:00+00:00'")
        else: c.execute("UPDATE capture_artifact SET details_json=json_set(details_json,'$.origin.kind','voice_input')")
        with pytest.raises(ValueError): bind(c, writing, original)
        c.rollback()
        assert json.loads(c.execute('SELECT details_json FROM capture_artifact').fetchone()[0]).get('attempt_binding') is None


def test_failed_answer_transaction_rolls_back_binding_and_missing_provenance_is_not_inferred(writing):
    db, _, _, _, _ = writing
    original = capture(writing)
    answer(writing)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        bind(c, writing, original)
        c.rollback()
        prompt = c.execute('SELECT * FROM diagnostic_prompt').fetchone()
        attempt = c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        with pytest.raises(ValueError, match='ATTEMPT_BINDING_REQUIRED'): original_written_response(c, prompt, attempt)
        c.execute('DELETE FROM capture_artifact')
        with pytest.raises(ValueError, match='MISSING_OR_AMBIGUOUS'): original_written_response(c, prompt, attempt)


def test_origin_capture_cannot_rebind_text_cross_session_or_use_old_ocr_confirmation(writing):
    _, sid, _, p, service = writing
    with pytest.raises(ValueError, match='OCR_USE_AUTHORIZATION_UNAVAILABLE'):
        service.bind_confirmed_document_response(sid, prompt_id=p['prompt_id'], item_version=p['item_version'],
            artifact_id='ocr', revision_id='revision', confirmation_id='tutor-confirmation',
            use_authorization_id='untrusted-http-value', client_action_id='ocr')
    capture(writing)
    base = dict(prompt_id=p['prompt_id'], item_version=p['item_version'], text=TEXT, client_action_id='original')
    with pytest.raises(ValueError, match='ACTION_CONFLICT'):
        service.capture_native_response(sid, **{**base, 'text':'changed'})
    with pytest.raises(ValueError, match='ACTION_CONFLICT'):
        service.capture_native_response('another-session', **base)
    with pytest.raises(ValueError, match='ALREADY_BOUND'):
        service.capture_native_response(sid, **{**base, 'client_action_id':'another-key'})
