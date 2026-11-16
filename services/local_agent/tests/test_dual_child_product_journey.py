from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from pet_plan_http_fixture import app_listening_resources as listening_resources
from pet_plan_http_fixture import start_pet_task, answer_pet_task


def _settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        database_path=root / "dual-child-journey.db",
        data_encryption_key=b"j" * 32,
        maintenance_enabled=False,
        agent_runtime_enabled=True,
    )


def _assert_ok(response):
    assert response.status_code in {200, 201}, response.text
    return response.json()


def _expected_answer(database: Database, diagnostic_item_id: str) -> str:
    with database.connect() as connection:
        expected = json.loads(connection.execute(
            "SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
            (diagnostic_item_id,),
        ).fetchone()[0])
    answers = expected.get("exact_answers") or []
    if answers:
        return str(answers[0])
    keywords = expected.get("keywords") or []
    if keywords:
        return " ".join(str(value) for value in keywords[:2])
    groups = expected.get("keyword_groups") or []
    answer = " and ".join(str(group[0]) for group in groups if group)
    return f"{answer} because this explains my answer, and then I checked it carefully." if answer else "不知道，需要讲解"


def _start_and_answer_bound_goal(
    client: TestClient, database: Database, session_id: str, item: dict, prefix: str
) -> None:
    turn = _assert_ok(client.post(
        f"/v1/sessions/{session_id}/agent-turn",
        json={"transcript": "开始任务", "client_action_id": f"{prefix}-bound-start-turn"},
    ))
    executed = _assert_ok(client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute"))
    prompt = executed["execution"]["result"]["diagnostic_prompt"]["prompt"]
    assert executed["execution"]["result"]["plan_item_id"] == item["plan_item_id"]
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/diagnostic-prompt/answer",
        json={
            "prompt_id": prompt["prompt_id"],
            "item_version": prompt["item_version"],
            "answer": _expected_answer(database, prompt["diagnostic_item_id"]),
            "client_action_id": f"{prefix}-bound-answer",
        },
    ))


def _answer_guided_group(client: TestClient, database: Database, session_id: str, item: dict, prefix: str) -> None:
    if item["source_type"] not in {"competition_math", "junior_exam_goal"}:
        if item["source_type"] == "pet_daily" and str(item.get("source_ref") or "").startswith("diag-pet-"):
            _assert_ok(client.post(
                f"/v1/sessions/{session_id}/diagnostics/answer",
                json={
                    "diagnostic_item_id": item["source_ref"],
                    "answer": "Last Friday I studied with my friend because we had a test, and then we checked our work together.",
                    "client_action_id": f"{prefix}-pet-answer",
                },
            ))
        return
    track = "competition_math" if item["source_type"] == "competition_math" else "school"
    subject = f"&subject={item['subject']}" if item["source_type"] == "junior_exam_goal" else ""
    diagnostic = _assert_ok(client.get(
        f"/v1/sessions/{session_id}/diagnostics/next?track={track}{subject}"
    ))["item"]
    attempt = 0
    while diagnostic:
        submitted = _assert_ok(client.post(
            f"/v1/sessions/{session_id}/diagnostics/answer",
            json={
                "diagnostic_item_id": diagnostic["diagnostic_item_id"],
                "answer": _expected_answer(database, diagnostic["diagnostic_item_id"]),
                "client_action_id": f"{prefix}-specialized-{attempt}",
            },
        ))
        run = submitted.get("specialized_assessment")
        diagnostic = submitted.get("next_specialized_item") if run and run["status"] == "active" else None
        attempt += 1


def _finish_plan(client: TestClient, database: Database, session_id: str, prefix: str) -> None:
    while True:
        workspace = _assert_ok(client.get(f"/v1/sessions/{session_id}"))
        unfinished = next(
            (item for item in workspace["plan_items"] if item["status"] in {"pending", "paused", "active"}),
            None,
        )
        if not unfinished:
            return
        if unfinished["source_type"] == "planned_break":
            _assert_ok(client.post(
                f"/v1/sessions/{session_id}/break",
                json={"client_action_id": f"{prefix}-break-{unfinished['position']}"},
            ))
            _assert_ok(client.post(
                f"/v1/sessions/{session_id}/resume",
                json={"client_action_id": f"{prefix}-resume-{unfinished['position']}"},
            ))
            continue
        if unfinished["status"] == "paused":
            _assert_ok(client.post(
                f"/v1/plan-items/{unfinished['plan_item_id']}/resume",
                json={"client_action_id": f"{prefix}-item-resume-{unfinished['position']}"},
            ))
        elif unfinished["status"] == "pending" and unfinished['source_type'] == 'pet_daily' and str(unfinished.get('source_ref') or '').startswith('diag-pet-'):
            prompt = start_pet_task(client, session_id, unfinished, f"{prefix}-item-start-{unfinished['position']}")
            answer_pet_task(client, session_id, prompt,
                'Last Friday I studied with my friend because we had a test, and then we checked our work together.',
                f"{prefix}-{unfinished['position']}-pet-original-answer")
        elif unfinished["status"] == "pending" and unfinished["source_type"] in {
            "competition_math", "junior_exam_goal"
        }:
            _start_and_answer_bound_goal(
                client, database, session_id, unfinished, f"{prefix}-{unfinished['position']}"
            )
        elif unfinished["status"] == "pending":
            _assert_ok(client.post(
                f"/v1/plan-items/{unfinished['plan_item_id']}/start",
                json={"client_action_id": f"{prefix}-item-start-{unfinished['position']}"},
            ))
        if unfinished['source_type'] != 'pet_daily':
            _answer_guided_group(client, database, session_id, unfinished, f"{prefix}-{unfinished['position']}")
        _assert_ok(client.post(
            f"/v1/plan-items/{unfinished['plan_item_id']}/complete",
            json={
                "actual_minutes": unfinished["expected_minutes"],
                "completion_evidence_type": "voice_confirmed",
                "completion_note": "合成旅程保存的孩子完成确认",
                "client_action_id": f"{prefix}-item-complete-{unfinished['position']}",
            },
        ))


def _complete_day(client: TestClient, database: Database, child_id: str, day_index: int) -> str:
    prefix = f"journey-{child_id}-{day_index}"
    subject = "英语" if child_id == "child-primary-4" else "数学"
    recall = "学习了一般过去时和短消息。" if subject == "英语" else "复习函数和一元二次方程。"
    session_id = _assert_ok(client.post(
        "/v1/sessions",
        json={"child_id": child_id, "client_action_id": f"{prefix}-session"},
    ))["session_id"]
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/transition",
        json={"expected_state": "check_in", "target_state": "classroom_recall", "reason": "voice_ready", "client_action_id": f"{prefix}-recall-state"},
    ))
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/recalls",
        json={"subject": subject, "summary": recall, "client_action_id": f"{prefix}-recall"},
    ))
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/transition",
        json={"expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "voice_done", "client_action_id": f"{prefix}-homework-state"},
    ))
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/homework",
        json={"subject": subject, "title": f"{subject}当日练习", "expected_minutes": 12, "client_action_id": f"{prefix}-homework"},
    ))
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/plan/generate",
        json={"client_action_id": f"{prefix}-plan"},
    ))
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/transition",
        json={"expected_state": "plan_review", "target_state": "study", "reason": "voice_confirmed", "client_action_id": f"{prefix}-study-state"},
    ))
    _finish_plan(client, database, session_id, prefix)
    _assert_ok(client.post(
        f"/v1/sessions/{session_id}/transition",
        json={"expected_state": "study", "target_state": "reflection", "reason": "all_tasks_done", "client_action_id": f"{prefix}-reflection-state"},
    ))
    finished = _assert_ok(client.post(
        f"/v1/sessions/{session_id}/reflection/complete",
        json={
            "learned_text": recall,
            "difficult_text": "有一步还需要延迟复测。",
            "feeling": "完成",
            "next_action": "下次先独立回答一道变式。",
            "client_action_id": f"{prefix}-reflection",
        },
    ))
    assert finished["session"]["status"] == "completed"
    assert finished["daily_report_ready"] is True
    return session_id


def _move_completed_day(database: Database, session_id: str, days_ago: int) -> None:
    timestamp = datetime.combine(date.today() - timedelta(days=days_ago), datetime.min.time(), tzinfo=timezone.utc).isoformat()
    report_date = (date.today() - timedelta(days=days_ago)).isoformat()
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET started_at=?,ended_at=?,last_checkpoint_at=? WHERE session_id=?", (timestamp, timestamp, timestamp, session_id))
        connection.execute("UPDATE daily_plan SET plan_date=?,created_at=? WHERE session_id=?", (report_date, timestamp, session_id))
        connection.execute("UPDATE daily_report SET report_date=?,created_at=? WHERE session_id=?", (report_date, timestamp, session_id))
        connection.commit()


def test_two_children_complete_seven_day_isolated_journeys_across_restart(tmp_path: Path, listening_resources) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    for child_id in ("child-primary-4", "child-junior-3"):
        for day_index in range(7):
            # Recreate the app every third day to verify persisted continuation,
            # not merely in-memory state.
            with TestClient(create_app(settings)) as client:
                session_id = _complete_day(client, database, child_id, day_index)
            _move_completed_day(database, session_id, 6 - day_index)

    with TestClient(create_app(settings)) as client:
        token = _assert_ok(client.post("/v1/parent/setup", json={"pin": "2468"}))["parent_token"]
        dashboard = _assert_ok(client.get("/v1/parent/dashboard", headers={"X-HSC-Parent": token}))
        primary_model = _assert_ok(client.get(
            "/v1/parent/children/child-primary-4/longitudinal-model", headers={"X-HSC-Parent": token}
        ))
        junior_model = _assert_ok(client.get(
            "/v1/parent/children/child-junior-3/longitudinal-model", headers={"X-HSC-Parent": token}
        ))

    child_rows = {row["child_id"]: row for row in dashboard["children"]}
    assert len(child_rows["child-primary-4"]["reports"]) == 7
    assert len(child_rows["child-junior-3"]["reports"]) == 7
    assert primary_model["payload"]["execution"]["sessions"] == 7
    assert junior_model["payload"]["execution"]["sessions"] == 7
    assert "child-junior-3" not in json.dumps(primary_model["payload"], ensure_ascii=False)
    assert "child-primary-4" not in json.dumps(junior_model["payload"], ensure_ascii=False)
    assert primary_model["snapshot_id"] != junior_model["snapshot_id"]
    with database.connect() as connection:
        session_counts = dict(connection.execute(
            "SELECT child_id,COUNT(*) FROM daily_session GROUP BY child_id"
        ).fetchall())
        cross_child_attempts = connection.execute(
            """SELECT COUNT(*) FROM diagnostic_attempt da JOIN daily_session ds ON ds.session_id=da.session_id
               WHERE da.child_id!=ds.child_id"""
        ).fetchone()[0]
    assert session_counts == {"child-junior-3": 7, "child-primary-4": 7}
    assert cross_child_attempts == 0
