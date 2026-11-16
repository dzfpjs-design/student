"""Unknown is persisted, not a zero hidden by response formatting."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService, UnknownDiagnosticItemError
from home_study_agent.repository import StudyRepository
from test_open_response_supply_http import CHILD, prepared, publish_open


@pytest.mark.parametrize('mode',['written','spoken_explanation'])
def test_unassessed_original_response_is_null_in_both_tables_and_retries(tmp_path,mode):
    db,service,spec,source,_ = prepared(tmp_path,'数学')
    source['response_mode']=mode
    with db.connect() as c:
        c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?',(json.dumps(source),spec['excerpt_id']))
        c.commit()
    published = publish_open(service,spec)
    sid = StudyRepository(db).start_session(CHILD,'start')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?",(sid,))
        c.commit()
    answer = ' '.join(source['task_points'])
    attempt = LearningStateService(db).submit_diagnostic(sid,published['diagnostic_item_id'],answer,'answer')
    assert attempt['correctness'] is None
    assert attempt['score_status']=='needs_review' and not attempt['mastery_update_allowed']
    with db.connect() as c:
        row = c.execute('SELECT da.correctness,le.correctness,le.independent,le.confidence FROM diagnostic_attempt da JOIN learning_evidence le USING(evidence_id)').fetchone()
        assert tuple(row)==(None,None,0,0.0)
        assert not c.execute('SELECT 1 FROM review_task').fetchone()
        assert not c.execute('SELECT 1 FROM mastery_update_event').fetchone()
        assert not c.execute('SELECT 1 FROM mastery_state').fetchone()
    restarted = Database(db.path)
    restarted.initialize()
    replay = LearningStateService(restarted).submit_diagnostic(sid,published['diagnostic_item_id'],answer,'answer')
    assert replay['attempt_id']==attempt['attempt_id'] and replay['correctness'] is None
    service.withdraw(CHILD,published['candidate_id'],actor='parent',action_id='withdraw',note='Source withdrawn',expected_version=4)
    with pytest.raises(UnknownDiagnosticItemError):
        LearningStateService(db).submit_diagnostic(sid,published['diagnostic_item_id'],answer,'new-answer')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1


def test_open_mode_cannot_take_keyword_or_exact_shortcut():
    for rubric in ({'keywords':['correct']},{'local_rule':'local_exact_v1','exact_answers':['correct']}):
        score,feedback = LearningStateService._score_answer(dict(answer_type='open_response',
            assessment_mode='open_response_rubric',expected_answer_json=json.dumps(rubric),explanation='Reference'),'correct')
        assert score is None and '正确' not in feedback
