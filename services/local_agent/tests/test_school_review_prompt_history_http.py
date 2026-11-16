"""Corrupt unrelated shown content must not masquerade as fresh same-question proof."""
import json

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD


def test_other_subject_bad_optional_prompt_does_not_remove_math_review(school_case, monkeypatch):
    db, client, _, sid = school_case[:4]
    service = DiagnosticPromptService(db)
    original = service.prepare(sid, track='school', preferred_item_id='diag-j3-chinese-reading-1',
        client_action_id='history-chinese-prompt')['prompt']
    cancelled = service.cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
    assert cancelled['status'] == 'cancelled'
    with db.connect() as c:
        c.execute('UPDATE diagnostic_prompt SET item_json=? WHERE prompt_id=?', ('[]', original['prompt_id']))
        c.commit()
    _, scored, _, _, _ = answer_and_review(school_case)
    with db.connect() as c:
        rows = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND status='pending'",
            (scored['attempt']['evidence_id'],)).fetchall()
    assert len(rows) == 1, 'Unrelated Chinese historical corruption removed the independently sourced math review'
    case, task, prompt, _, _ = next_day(school_case, monkeypatch, key='optional-prompt')
    reopened = case[1].school_reopen()
    got = reopened.get(f'/v1/sessions/{case[3]}/diagnostic-prompt', params={'prompt_id':prompt['prompt_id']})
    assert got.status_code == 200 and got.json()['status'] == 'waiting_answer', got.text
    with db.connect() as c:
        assert c.execute('SELECT item_json FROM diagnostic_prompt WHERE prompt_id=?', (original['prompt_id'],)).fetchone()[0] == '[]'
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE review_task_id=?', (task['source_ref'],)).fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['same_item', 'changed_version', 'changed_snapshot'])
def test_bad_prompt_is_not_a_freshness_certificate(school_case, fault):
    db, _, _, sid, _, near = school_case[:6]
    service = DiagnosticPromptService(db)
    item_id = near['diagnostic_item_id'] if fault in {'same_item', 'changed_snapshot'} else 'diag-j3-chinese-reading-1'
    original = service.prepare(sid, track='school', preferred_item_id=item_id,
        client_action_id='bad-proof-prompt')['prompt']
    service.cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
    stored = json.dumps({**original['item'], 'prompt': '损坏的旧快照，不能被视为另一道题。'}, ensure_ascii=False) if fault == 'changed_snapshot' else '[]'
    with db.connect() as c:
        c.execute('UPDATE diagnostic_prompt SET item_json=? WHERE prompt_id=?', (stored, original['prompt_id']))
        if fault == 'changed_version':
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (item_id,))
        c.commit()
    _, scored, _, _, _ = answer_and_review(school_case)
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM review_task WHERE source_evidence_id=? AND status='pending'",
            (scored['attempt']['evidence_id'],)).fetchone()
        assert c.execute('SELECT item_json FROM diagnostic_prompt WHERE prompt_id=?', (original['prompt_id'],)).fetchone()[0] == stored
