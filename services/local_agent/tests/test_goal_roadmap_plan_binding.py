"""Real plan-consumer contract; only synthetic temporary database fixtures."""

import json

from home_study_agent.goal_program import GoalProgramService
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_goal_program import goal_db, exact_goal_item


def test_real_plan_preserves_selected_roadmap_node_action_and_version(goal_db, exact_goal_item):
    child = "child-junior-3"
    repo = StudyRepository(goal_db)
    service = GoalProgramService(goal_db)
    service.seed_defaults()
    with goal_db.connect() as c:
        node = c.execute("SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?", (exact_goal_item[0],)).fetchone()[0]
        c.execute("UPDATE knowledge_node SET status='inactive' WHERE grade_band LIKE '初三%' AND knowledge_node_id!=?", (node,))
        c.execute("DELETE FROM knowledge_edge WHERE target_node_id=?", (node,))
        c.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?", (child,))
        c.commit()
    session = repo.start_session(child, "roadmap-plan-session")["session_id"]
    with goal_db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1 WHERE session_id=?", (session,))
        c.commit()
    repo.add_homework(session, "数学", "校内作业", 10, "written", None, None, None, None, None, "roadmap-plan-homework")
    selected = service.roadmap(child, "junior_exam_readiness")["next_daily_action"]
    assert selected and selected["dispatchable"] and selected["knowledge_node_id"] == node
    workspace = repo.generate_plan(session, "roadmap-plan-generate")
    item = next(item for item in workspace["plan_items"] if item["source_type"] == "junior_exam_goal")
    with goal_db.connect() as c:
        decision = c.execute("""SELECT d.metadata_json FROM plan_constraint_decision d JOIN plan_item pi ON pi.plan_id=d.plan_id
            WHERE pi.plan_item_id=? AND d.obligation_type='junior_exam_goal' AND d.source_ref=?""",
                             (item["plan_item_id"], item["source_ref"])).fetchone()
    binding = json.loads(decision[0]).get("planning_evidence")
    assert binding, "generate_plan dropped the roadmap binding; cycle ID/title cannot select the original prerequisite"
    assert binding["knowledge_node_id"] == node
    assert binding["action"] == selected["action"]
    assert binding["roadmap_source_version"] == selected["roadmap_source_version"]
    assert binding["mastery_claim_allowed"] is False
    replay = StudyRepository(Database(goal_db.path)).get_workspace(session)
    assert [row["plan_item_id"] for row in replay["plan_items"]] == [row["plan_item_id"] for row in workspace["plan_items"]]
