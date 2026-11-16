"""Writing provenance/followups commit with the real prompt and answer."""
import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService, DiagnosticPromptError
from home_study_agent.writing_response_origin import WritingOriginalService
from test_writing_response_origin import writing, capture, TEXT, CHILD
from test_writing_teaching import ready, resolve


def test_real_answer_commits_original_binding_once_and_resumes_after_failure(writing, monkeypatch):
    db, sid, prompts, p, _ = writing
    original = capture(writing)
    kwargs = dict(prompt_id=p['prompt_id'], item_version=p['item_version'], answer=TEXT,
                  client_action_id='original-answer', writing_original_artifact_id=original['artifact_id'])
    import home_study_agent.writing_response_origin as origin
    actual = origin.bind_written_attempt
    def fail_after_bind(*args, **values):
        actual(*args, **values)
        raise OSError('synthetic binding acknowledgement failure')
    with monkeypatch.context() as scoped:
        scoped.setattr(origin, 'bind_written_attempt', fail_after_bind)
        with pytest.raises(OSError):
            prompts.answer(sid, **kwargs)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (p['prompt_id'],)).fetchone()
        assert row['status'] == 'answer_saved' and row['writing_original_artifact_id'] == original['artifact_id']
        assert 'attempt_binding' not in c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (original['artifact_id'],)).fetchone()[0]
    restarted = DiagnosticPromptService(db)
    result = restarted.resume(sid, prompt_id=p['prompt_id'])
    assert result['status'] == 'answered'
    assert restarted.answer(sid, **kwargs)['attempt']['attempt_id'] == result['attempt']['attempt_id']
    with pytest.raises(DiagnosticPromptError, match='writing_original_conflict'):
        restarted.answer(sid, **{**kwargs, 'writing_original_artifact_id': None})
    with db.connect() as c:
        prompt = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (p['prompt_id'],)).fetchone()
        attempt = c.execute('SELECT * FROM diagnostic_attempt').fetchone()
        assert origin.original_written_response(c, prompt, attempt)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1


def test_followup_create_is_atomic_and_revision_cannot_claim_zero_hints(writing):
    service, _, _ = ready(writing)
    resolved = resolve(service, writing)
    db, sid, prompts, p, _ = writing
    binding = {'parent_prompt_id': p['prompt_id'], 'parent_item_version': p['item_version'],
               'kind': 'revision', 'binding_version': resolved['binding_version']}
    kwargs = dict(track='pet', preferred_item_id=p['diagnostic_item_id'], client_action_id='actual-revision', writing_followup=binding)
    with pytest.raises(DiagnosticPromptError, match='writing_followup_binding_changed'):
        prompts.prepare(sid, **{**kwargs, 'writing_followup': {**binding, 'binding_version': '0' * 64}})
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 0
    target = prompts.prepare(sid, **kwargs)['prompt']
    assert prompts.prepare(sid, **kwargs)['prompt']['prompt_id'] == target['prompt_id']
    original = WritingOriginalService(db).capture_native_response(sid, prompt_id=target['prompt_id'],
        item_version=target['item_version'], text=TEXT, client_action_id='revised-writing-original')
    answer = prompts.answer(sid, prompt_id=target['prompt_id'], item_version=target['item_version'],
        answer=TEXT, hint_level=0, writing_original_artifact_id=original['artifact_id'], client_action_id='revised-answer')
    assert answer['status'] == 'answered'
    with db.connect() as c:
        evidence = c.execute('SELECT hint_level,independent FROM learning_evidence WHERE evidence_id=?', (answer['attempt']['evidence_id'],)).fetchone()
        assert evidence['hint_level'] >= 1 and not evidence['independent']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0] == 1
