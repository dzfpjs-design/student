"""An authorized fresh start must not revive revoked or answered preparation."""
import pytest

from home_study_agent.repository import StudyRepository
from home_study_agent.scheduled_writing_goal import validate_origin
from home_study_agent.task_start_binding import TaskStartBindingError
from test_school_open_reviews import school_case, answer_and_review, next_day, CHILD


@pytest.mark.parametrize('fault',['cancelled_command','cancelled_goal','answer_saved'])
def test_prepared_recovery_keeps_revocation_and_answer_boundaries(school_case,monkeypatch,fault):
    answer_and_review(school_case)
    db=school_case[0]
    with monkeypatch.context() as patch:
        def fail(*args,**kwargs):
            raise RuntimeError('synthetic failure before timer write')
        patch.setattr(StudyRepository,'start_plan_item',fail)
        with pytest.raises(RuntimeError,match='synthetic failure'):
            next_day(school_case,monkeypatch,key='prepared-guard')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        prompt=c.execute("SELECT * FROM diagnostic_prompt WHERE child_id=? AND status='waiting_answer'",(CHILD,)).fetchone()
        plan=c.execute("SELECT * FROM agent_plan WHERE json_extract(context_manifest_json,'$.scheduled_writing_goal.prompt_id')=?",(prompt['prompt_id'],)).fetchone()
        validate_origin(c,plan,prompt,prepared_recovery=True)
        if fault=='cancelled_command':
            c.execute("UPDATE agent_plan SET status='cancelled' WHERE plan_id=?",(plan['plan_id'],))
        elif fault=='cancelled_goal':
            c.execute("UPDATE agent_objective SET status='cancelled' WHERE objective_id=?",(plan['objective_id'],))
        else:
            c.execute("UPDATE diagnostic_prompt SET status='answer_saved',answer_action_id='already-saved' WHERE prompt_id=?",(prompt['prompt_id'],))
        plan=c.execute('SELECT * FROM agent_plan WHERE plan_id=?',(plan['plan_id'],)).fetchone()
        prompt=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
        with pytest.raises(TaskStartBindingError):
            validate_origin(c,plan,prompt,prepared_recovery=True)
        assert c.execute('SELECT count(*) FROM diagnostic_attempt WHERE review_task_id=?',(prompt['review_task_id'],)).fetchone()[0]==0
        c.rollback()
