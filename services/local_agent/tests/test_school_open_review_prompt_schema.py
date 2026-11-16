"""Real published school answers stay unqualified without original prompt schema."""
from datetime import datetime, timezone

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from home_study_agent.school_open_reviews import SchoolReviewError, _anchor
from test_open_response_supply_http import CHILD, prepared, publish_open


@pytest.mark.parametrize('schema', ['missing', 'incomplete', 'no_original_binding'])
def test_saved_school_original_without_prompt_schema_fails_closed_readonly(tmp_path, schema):
    db, supply, spec, _, _ = prepared(tmp_path, '物理')
    item = publish_open(supply, spec)
    sid = StudyRepository(db).start_session(CHILD, 'unbound-school-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    saved = LearningStateService(db).submit_diagnostic(sid, item['diagnostic_item_id'],
        '我说明变量与实验结论的适用条件。', 'unbound-original')
    assert saved['correctness'] is None
    if schema == 'no_original_binding':
        DiagnosticPromptService(db)
    with db.connect() as c:
        if schema == 'incomplete':
            c.execute('CREATE TABLE diagnostic_prompt(child_id TEXT, attempt_id TEXT)')
            c.commit()
        before_schema = [tuple(row) for row in c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]
        before = dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (saved['attempt_id'],)).fetchone())
        with pytest.raises(SchoolReviewError, match='^SCHOOL_REVIEW_ORIGINAL_PROMPT_REQUIRED$'):
            _anchor(c, CHILD, saved['evidence_id'], datetime.now(timezone.utc))
        assert [tuple(row) for row in c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')] == before_schema
        assert dict(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (saved['attempt_id'],)).fetchone()) == before
    with Database(db.path).connect() as c:
        reopened = LearningStateService._attempt(c, saved['attempt_id'])
    assert reopened['correctness'] is None
