"""Malformed historical goal bindings cannot crash recovery or gain authority."""
import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from test_writing_response_origin import writing
from test_writing_reference_revision import reference_revision


@pytest.mark.parametrize('field', ['phase', 'success_criteria', 'objective_id'])
def test_legacy_binding_missing_required_field_returns_blocked(reference_revision, field):
    source, _, _ = reference_revision
    db, sid, _, prompt, _ = source
    with db.connect() as c:
        evidence = c.execute('SELECT evidence_id FROM diagnostic_attempt WHERE attempt_id=(SELECT attempt_id FROM diagnostic_prompt WHERE prompt_id=?)',
            (prompt['prompt_id'],)).fetchone()[0]
        c.execute("""UPDATE agent_plan SET plan_json=json_set(plan_json,'$.local_learning_binding.phase','diagnose',
            '$.local_learning_binding.success_criteria',json('["地点","时间","学习内容"]'))
            WHERE json_type(plan_json,'$.local_learning_binding')='object'""")
        c.execute("UPDATE agent_plan SET plan_json=json_remove(plan_json,?)", ('$.local_learning_binding.' + field,))
        c.commit()
    runtime = AgentRuntimeStore(db)
    result = runtime.finalize_diagnostic_evaluation(sid, evidence)
    assert result['status'] == 'blocked'
    assert result['reason'] == 'original_learning_binding_invalid'
    with db.connect() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='learning_evaluation_execution'").fetchone():
            assert c.execute('SELECT count(*) FROM learning_evaluation_execution').fetchone()[0] == 0
