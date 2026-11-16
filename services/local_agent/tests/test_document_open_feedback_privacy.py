"""Feedback history stays local even when its authority is withdrawn."""
import sqlite3

import pytest

from home_study_agent.document_check_followup import sources
from home_study_agent.document_revisit import retained
from home_study_agent.learning_rubric import RubricEvidenceService
from test_document_open_check import opened
from test_document_open_followup import evaluated
from test_document_revisit import say


def test_human_correction_then_retraction_and_redaction(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        prompt, saved, follow = evaluated(j, monkeypatch, 'reviewed-first')
        # Direct service reviews need the same managed-audio scope as the app.
        from home_study_agent.database import LocalMediaRuntime
        j['db'].local_media_runtime = LocalMediaRuntime(j['settings'].data_dir, j['settings'].data_encryption_key)
        service = RubricEvidenceService(j['db'])
        with j['db'].connect() as c:
            assessment_id = c.execute('SELECT assessment_id FROM rubric_assessment').fetchone()[0]
            original = service.get(prompt['child_id'], assessment_id, connection=c)
            with pytest.raises(sqlite3.IntegrityError, match='RUBRIC_HISTORY_IMMUTABLE'):
                c.execute("UPDATE rubric_assessment SET payload_json='{}' WHERE assessment_id=?", (assessment_id,))
            c.rollback()
        revised = [{**{key:d[key] for key in ('dimension_id','score','response_quotes')},
            'rationale':'合成复核：先核对每个数表示什么，再说明运算依据。'} for d in original['dimensions']]
        service.review(prompt['child_id'], assessment_id, expected_revision=0,
            client_action_id='parent-correction', reviewer_id='synthetic-parent', reviewer_role='parent',
            operation='approve', reason='Synthetic source-grounded review', dimensions=revised,
            criterion_evidence=[dict(criterion=p, response_quote=original['response']) for p in original['criteria']],
            independent=False, hint_level=1)
        corrected = say(j, monkeypatch, '刚才的建议是什么', 'status', 'corrected-status')
        assert revised[0]['rationale'] in corrected['execution']['response_text']
        service.review(prompt['child_id'], assessment_id, expected_revision=1,
            client_action_id='parent-retract', reviewer_id='synthetic-parent', reviewer_role='parent',
            operation='retract', reason='Synthetic review withdrawn')
        rejected = say(j, monkeypatch, '再说一下建议', 'help', 'retracted-help')
        assert '暂不据此判断对错' in rejected['execution']['response_text']
        assert revised[0]['rationale'] not in rejected['execution']['response_text']
        with j['db'].connect() as c:
            c.execute('BEGIN')
            from home_study_agent.document_open_feedback import private_feedback_texts
            row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
            assert revised[0]['rationale'] in private_feedback_texts(c, row)
            private = sources(c, retained(c, j['cid'], prompt['child_id']))
            assert revised[0]['rationale'] in private
            assert original['dimensions'][0]['rationale'] in private
            assert original['response'] in private
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 1
