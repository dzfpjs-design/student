from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                data_dir=tmp_path,
                database_path=tmp_path / "test.db",
                data_encryption_key=b"0" * 32,
                maintenance_enabled=False,
            )
        )
    )


def _artifact(
    client: TestClient,
    child_id: str,
    artifact_id: str,
    lines: list[dict],
    local_pipeline: dict | None = None,
) -> None:
    session = client.post(
        "/v1/sessions",
        json={"child_id": child_id, "client_action_id": f"session-{artifact_id}"},
    ).json()
    database_path = client.app.state.settings.database_path
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO capture_artifact(
              artifact_id, child_id, session_id, artifact_type, local_path,
              extracted_text, confidence, details_json, status, created_at, client_action_id
            ) VALUES (?, ?, ?, 'document_ocr', NULL, ?, 0.91, ?, 'completed', datetime('now'), ?)
            """,
            (
                artifact_id,
                child_id,
                session["session_id"],
                "\n".join(line["text"] for line in lines),
                json.dumps({
                    "lines": lines,
                    "engine": "test-local",
                    **({"local_pipeline": local_pipeline} if local_pipeline else {}),
                }, ensure_ascii=False),
                f"ocr-{artifact_id}",
            ),
        )


def test_numbered_ocr_is_split_into_persistent_candidates(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _artifact(client, "child-primary-4", "artifact-multi", [
            {"text": "四年级数学练习", "confidence": 0.98, "box": [[0, 0], [300, 0], [300, 30], [0, 30]]},
            {"text": "1. 计算125×32。", "confidence": 0.95, "box": [[0, 40], [300, 40], [300, 70], [0, 70]]},
            {"text": "写出简便计算过程。", "confidence": 0.92, "box": [[0, 75], [300, 75], [300, 105], [0, 105]]},
            {"text": "2、找出数列1、3、6、10的下一个数。", "confidence": 0.90, "box": [[0, 120], [500, 120], [500, 150], [0, 150]]},
            {"text": "3．一个长方形长8厘米，宽5厘米，面积是多少？", "confidence": 0.88, "box": [[0, 165], [520, 165], [520, 195], [0, 195]]},
        ])

        prepared = client.post(
            "/v1/capture-artifacts/artifact-multi/mistake-candidates/prepare",
            json={"child_id": "child-primary-4", "client_action_id": "prepare-multi-001"},
        )
        assert prepared.status_code == 200
        items = prepared.json()["items"]
        assert len(items) == 3
        assert items[0]["question_text"] == "四年级数学练习\n1. 计算125×32。\n写出简便计算过程。"
        assert items[1]["question_text"].startswith("2、找出数列")
        assert items[0]["region"] == {"x": 0, "y": 0, "width": 300, "height": 105}
        assert all(item["status"] == "pending" for item in items)

        queues = client.get("/v1/children/child-primary-4/mistake-capture-queues")
        assert queues.status_code == 200
        assert queues.json()["items"][0]["artifact_id"] == "artifact-multi"
        assert queues.json()["items"][0]["pending_count"] == 3

        repeated = client.post(
            "/v1/capture-artifacts/artifact-multi/mistake-candidates/prepare",
            json={"child_id": "child-primary-4", "client_action_id": "prepare-multi-002"},
        )
        assert [item["candidate_id"] for item in repeated.json()["items"]] == [
            item["candidate_id"] for item in items
        ]

    with _client(tmp_path) as restarted:
        listed = restarted.get(
            "/v1/capture-artifacts/artifact-multi/mistake-candidates",
            params={"child_id": "child-primary-4"},
        )
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 3


def test_marked_wrong_question_is_the_only_persisted_candidate(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _artifact(
            client,
            "child-primary-4",
            "artifact-marked-target",
            [
                {"text": "1. What do you like?", "confidence": 0.97, "box": [[10, 20], [300, 20], [300, 55], [10, 55]]},
                {"text": "2. Would you like a banana?", "confidence": 0.96, "box": [[10, 70], [380, 70], [380, 105], [10, 105]]},
                {"text": "3. Where is my pencil case?", "confidence": 0.95, "box": [[10, 120], [360, 120], [360, 155], [10, 155]]},
            ],
            local_pipeline={
                "wrong_question_candidates": [{
                    "text": "2. Would you like a banana?",
                    "confidence": 0.88,
                    "ocr_confidence": 0.96,
                    "bbox_xywh": [10, 70, 370, 35],
                    "red_pixel_count": 42,
                    "red_mark_count": 1,
                    "evidence": "red_correction_mark_near_question",
                    "requires_child_confirmation": True,
                }]
            },
        )

        prepared = client.post(
            "/v1/capture-artifacts/artifact-marked-target/mistake-candidates/prepare",
            json={"child_id": "child-primary-4", "client_action_id": "prepare-marked-target-001"},
        )

        assert prepared.status_code == 200
        assert len(prepared.json()["items"]) == 1
        target = prepared.json()["items"][0]
        assert target["question_text"] == "2. Would you like a banana?"
        assert target["region"] == {"x": 10, "y": 70, "width": 370, "height": 35}
        assert target["confidence"] == 0.88


@pytest.mark.parametrize('child', ['child-primary-4', 'child-junior-3'])
def test_phonics_wrong_question_maps_to_phonics_and_has_verified_transfer_items(tmp_path: Path, child) -> None:
    with _client(tmp_path) as client:
        nodes = client.get(
            f"/v1/children/{child}/mistake-knowledge-candidates",
            params={
                "q": "选出下列单词中划线部分读音与其他不同的一项：A. grape B. make C. like",
                "subject": "英语",
            },
        ).json()["items"]

        assert nodes[0]["knowledge_node_id"] == "node-p4-english-phonics-discrimination"
        assert '读音' in nodes[0]['match_terms']
        assert nodes[0]['grade_relation'] == ('current' if child == 'child-primary-4' else 'foundation')
        assert all(node['subject'] == '英语' for node in nodes)
        with sqlite3.connect(client.app.state.settings.database_path) as connection:
            item_count = connection.execute(
                "SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id = ? AND status = 'active'",
                ("node-p4-english-phonics-discrimination",),
            ).fetchone()[0]
            relations = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT relation_type FROM knowledge_edge
                    WHERE source_node_id IN (?, ?) OR target_node_id = ?
                    """,
                    (
                        "node-p4-english-error-spelling-pronunciation",
                        "node-p4-english-phonics-discrimination",
                        "node-p4-english-phonics-discrimination",
                    ),
                )
            }
        assert item_count >= 3
        assert {"misconception_of", "remediated_by", "assessed_by"} <= relations


@pytest.mark.parametrize('query', ['in', 'ra ma', 'P4 ENGLISH', 'J3 GRAMMAR'])
def test_latin_fragments_and_catalog_codes_are_not_topic_evidence(tmp_path, query):
    with _client(tmp_path) as client:
        nodes = client.get('/v1/children/child-junior-3/mistake-knowledge-candidates',
            params={'q': query, 'subject': '英语'}).json()['items']
        assert all(node['match_score'] == 0 and node['match_terms'] == [] for node in nodes)


def test_foundation_search_does_not_offer_higher_school_grades_or_other_subjects(tmp_path):
    with _client(tmp_path) as client:
        nodes = client.get('/v1/children/child-primary-4/mistake-knowledge-candidates',
            params={'q': '一元二次方程根的判别式', 'subject': '数学', 'limit': 20}).json()['items']
        assert nodes and all(node['subject'] == '数学' and not node['grade_band'].startswith('初') for node in nodes)


def test_marked_capture_to_diagnosed_weakness_is_durable_end_to_end(tmp_path: Path) -> None:
    mistake_id = ""
    with _client(tmp_path) as client:
        _artifact(
            client,
            "child-primary-4",
            "artifact-full-mistake-flow",
            [{
                "text": "(C) 2. Would you like a banana?",
                "confidence": 0.96,
                "box": [[20, 80], [420, 80], [420, 120], [20, 120]],
            }],
            local_pipeline={
                "wrong_question_candidates": [{
                    "text": "(C) 2. Would you like a banana?",
                    "confidence": 0.89,
                    "ocr_confidence": 0.96,
                    "bbox_xywh": [20, 80, 400, 40],
                    "red_pixel_count": 51,
                    "red_mark_count": 1,
                    "evidence": "red_correction_mark_near_question",
                    "requires_child_confirmation": True,
                }]
            },
        )
        candidate = client.post(
            "/v1/capture-artifacts/artifact-full-mistake-flow/mistake-candidates/prepare",
            json={"child_id": "child-primary-4", "client_action_id": "full-flow-prepare"},
        ).json()["items"][0]
        nodes = client.get(
            "/v1/children/child-primary-4/mistake-knowledge-candidates",
            params={"q": candidate["question_text"], "subject": "英语"},
        ).json()["items"]
        assert nodes and all(node["subject"] == "英语" for node in nodes)

        captured = client.post(
            f"/v1/mistake-capture-candidates/{candidate['candidate_id']}/capture",
            json={
                "child_id": "child-primary-4",
                "knowledge_node_id": nodes[0]["knowledge_node_id"],
                "subject": "英语",
                "title": nodes[0]["title"],
                "submitted_answer": "C",
                "client_action_id": "full-flow-capture",
            },
        )
        assert captured.status_code == 200
        mistake_id = captured.json()["mistake"]["mistake_id"]
        assert captured.json()["mistake"]["status"] == "awaiting_confirmation"

        confirmed = client.post(
            f"/v1/mistakes/{mistake_id}/confirm",
            json={"child_id": "child-primary-4", "decision": "confirm", "client_action_id": "full-flow-confirm"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "diagnosing"

        diagnosed = client.post(
            f"/v1/mistakes/{mistake_id}/diagnose",
            json={
                "child_id": "child-primary-4",
                "error_type": "reading_gap",
                "explanation": "我把 Would you like 当成了询问喜欢什么，没有读懂这是礼貌邀请。",
                "confidence": 0.8,
                "client_action_id": "full-flow-diagnose",
            },
        )
        assert diagnosed.status_code == 200
        payload = diagnosed.json()
        assert payload["status"] == "remediating"
        assert payload["diagnosis"]["error_type"] == "reading_gap"
        assert payload["diagnosis"]["status"] == "candidate"
        assert payload["mastery_gate"]["requirements"] == {
            "near_transfer": 2, "discrimination": 1, "delayed_48h": 1
        }

        with sqlite3.connect(client.app.state.settings.database_path) as connection:
            connection.row_factory = sqlite3.Row
            persisted = connection.execute(
                """
                SELECT mc.status, mc.subject, md.error_type, md.explanation,
                       cl.diagnosis_status, mg.requirements_json
                FROM mistake_case mc
                JOIN mistake_diagnosis md ON md.mistake_id = mc.mistake_id
                JOIN mistake_cluster cl ON cl.cluster_id = mc.cluster_id
                JOIN mistake_mastery_gate mg ON mg.mistake_id = mc.mistake_id
                WHERE mc.mistake_id = ?
                """,
                (mistake_id,),
            ).fetchone()
        assert dict(persisted)["status"] == "remediating"
        assert dict(persisted)["subject"] == "英语"
        assert dict(persisted)["diagnosis_status"] == "suspected"

    with _client(tmp_path) as restarted:
        restored = restarted.get("/v1/children/child-primary-4/mistakes").json()["items"]
        item = next(item for item in restored if item["mistake_id"] == mistake_id)
        assert item["status"] == "remediating"
        assert item["diagnosis"]["explanation"].startswith("我把 Would you like")
        assert item["source_ref"].startswith("artifact-full-mistake-flow#")


def test_unnumbered_ocr_remains_one_editable_candidate_and_is_child_isolated(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _artifact(client, "child-primary-4", "artifact-single", [
            {"text": "请用两种方法计算125×32。", "confidence": 0.86, "box": None},
            {"text": "写出每一步。", "confidence": 0.82, "box": None},
        ])
        prepared = client.post(
            "/v1/capture-artifacts/artifact-single/mistake-candidates/prepare",
            json={"child_id": "child-primary-4", "client_action_id": "prepare-single-001"},
        )
        assert prepared.status_code == 200
        assert [item["question_text"] for item in prepared.json()["items"]] == [
            "请用两种方法计算125×32。\n写出每一步。"
        ]

        forbidden = client.get(
            "/v1/capture-artifacts/artifact-single/mistake-candidates",
            params={"child_id": "child-junior-3"},
        )
        assert forbidden.status_code == 404


def test_layout_boundaries_are_authoritative_but_marks_do_not_hide_other_questions():
    from home_study_agent.mistake_capture import MistakeCaptureService, InvalidMistakeCaptureCandidateError
    import pytest
    blocks = [
        {"text": "1. Unmarked complete question", "block_kind": "question", "needs_manual_boundary_check": False},
        {"text": "2. Marked complete question", "block_kind": "question", "needs_manual_boundary_check": False},
        {"text": "3. Clipped question", "block_kind": "question", "needs_manual_boundary_check": True},
    ]
    pipeline = {"question_blocks": blocks, "wrong_question_candidates": [{"text": blocks[1]["text"]}, {"text": blocks[2]["text"]}]}
    artifact = {"details_json": json.dumps({"local_pipeline": pipeline}), "extracted_text": "do not fall back to full page", "confidence": .9}
    assert [x["question_text"] for x in MistakeCaptureService._segment(artifact)] == [blocks[1]["text"], blocks[0]["text"]]
    pipeline["question_blocks"] = [blocks[2]]
    artifact["details_json"] = json.dumps({"local_pipeline": pipeline})
    with pytest.raises(InvalidMistakeCaptureCandidateError, match="不能拼凑"):
        MistakeCaptureService._segment(artifact)


def test_verified_cloud_blocks_select_only_visibly_answered_questions(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _artifact(client, "child-junior-3", "artifact-cloud-physics", [
            {"text": "local unreadable", "confidence": 0.1, "box": None},
        ], local_pipeline={"question_blocks": [
            {"text": "1. 速度的计算", "block_kind": "question", "needs_manual_boundary_check": False},
            {"text": "3. 计算密度", "block_kind": "question", "needs_manual_boundary_check": False},
            {"text": "6. 页面下方未作答的题", "block_kind": "question",
             "needs_manual_boundary_check": False},
        ]})
        blocks = [
            {"kind": "text", "text": "1. 速度的计算", "bbox": [.1, .10, .8, .16], "confidence": .69,
             "source": "printed", "evidence_type": "ocr_guess", "review_required": True, "confidence_label": "unverified"},
            {"kind": "formula", "text": "v=s/t", "bbox": [.2, .17, .5, .21], "confidence": .35,
             "source": "handwritten", "evidence_type": "ocr_guess", "review_required": True, "confidence_label": "low"},
            {"kind": "text", "text": "2. 判断力的作用", "bbox": [.1, .30, .8, .36], "confidence": .69,
             "source": "printed", "evidence_type": "ocr_guess", "review_required": True, "confidence_label": "unverified"},
            {"kind": "text", "text": "3. 计算密度", "bbox": [.1, .50, .8, .56], "confidence": .69,
             "source": "printed", "evidence_type": "ocr_guess", "review_required": True, "confidence_label": "unverified"},
            {"kind": "formula", "text": "rho=m/V", "bbox": [.2, .57, .6, .62], "confidence": .35,
             "source": "handwritten", "evidence_type": "ocr_guess", "review_required": True, "confidence_label": "low"},
            {"kind": "text", "text": "5. 页面下方未作答的题", "bbox": [.1, .75, .8, .82], "confidence": .69,
             "source": "printed", "evidence_type": "ocr_guess", "review_required": True,
             "confidence_label": "unverified"},
        ]
        receipt = {
            "schema_version": "document_vision.v1", "status": "needs_review", "model": "qwen3-vl-flash",
            "evidence_type": "ocr_guess", "review_required": True, "blocks": blocks,
            "can_judge_correctness": False, "can_update_mastery": False, "total_tokens": 700,
            "privacy": {"original_frame_uploaded": False}, "page": {}, "provider_normalizations": [],
            "request_id": "artifact:artifact-cloud-physics", "latency_ms": 123, "text": "visible",
            "confidence": .35, "needs_confirmation": True, "reason": None,
            "provenance": {"source": "qwen_vl"},
        }
        database_path = client.app.state.settings.database_path
        with sqlite3.connect(database_path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS document_vision_request(
                child_id TEXT, request_id TEXT, status TEXT, result_json TEXT)""")
            connection.execute("""INSERT INTO document_vision_request(
                child_id,request_id,image_hash,status,reserved_tokens,charged_tokens,created_at,result_json
                ) VALUES (?,?,?,'succeeded',0,0,datetime('now'),?)""",
                ("child-junior-3", receipt["request_id"], "synthetic-safe-page-hash",
                 json.dumps(receipt, ensure_ascii=False)))
            details = json.loads(connection.execute(
                "SELECT details_json FROM capture_artifact WHERE artifact_id='artifact-cloud-physics'"
            ).fetchone()[0])
            details["cloud_vision_fallback"] = receipt
            connection.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='artifact-cloud-physics'",
                               (json.dumps(details, ensure_ascii=False),))
            connection.commit()

        response = client.post(
            "/v1/capture-artifacts/artifact-cloud-physics/mistake-candidates/prepare",
            json={"child_id": "child-junior-3", "client_action_id": "prepare-cloud-physics"},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["question_text"] for item in items] == ["1. 速度的计算", "3. 计算密度"]
        assert all(item["status"] == "pending" for item in items)
        assert all(item["region"]["coordinate_space"] == "normalized_safe_page" for item in items)
        assert all(item["region"]["observation_basis"] == "visible_snapshot" for item in items)
        assert all(item["region"]["temporal_baseline_available"] is False for item in items)


def test_cloud_question_renumbering_is_blocked_instead_of_creating_wrong_candidates(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        local_blocks = [
            {"text": "2. 规格分别为6V 3W和6V 6W的灯泡", "block_kind": "question",
             "needs_manual_boundary_check": False},
            {"text": "3. 探究电流产生热量与哪些因素有关", "block_kind": "question",
             "needs_manual_boundary_check": False},
        ]
        _artifact(client, "child-junior-3", "artifact-renumbered", [
            {"text": "2. 规格分别为6V 3W和6V 6W的灯泡", "confidence": .8, "box": None},
            {"text": "3. 探究电流产生热量与哪些因素有关", "confidence": .8, "box": None},
        ], local_pipeline={"question_blocks": local_blocks})
        blocks = [
            {"kind": "text", "text": "1. 规格分别为6V 3W和6V 6W的灯泡", "bbox": [.1,.1,.8,.2],
             "confidence": .69, "source": "printed", "evidence_type": "ocr_guess", "review_required": True,
             "confidence_label": "unverified"},
            {"kind": "text", "text": "A", "bbox": [.2,.18,.25,.21], "confidence": .69,
             "source": "handwritten", "evidence_type": "ocr_guess", "review_required": True,
             "confidence_label": "unverified"},
            {"kind": "text", "text": "3. 探究电流产生热量与哪些因素有关", "bbox": [.1,.3,.8,.4],
             "confidence": .69, "source": "printed", "evidence_type": "ocr_guess", "review_required": True,
             "confidence_label": "unverified"},
            {"kind": "text", "text": "D", "bbox": [.2,.38,.25,.41], "confidence": .69,
             "source": "handwritten", "evidence_type": "ocr_guess", "review_required": True,
             "confidence_label": "unverified"},
        ]
        receipt = {
            "schema_version": "document_vision.v1", "status": "needs_review", "model": "qwen3-vl-flash",
            "evidence_type": "ocr_guess", "review_required": True, "blocks": blocks,
            "can_judge_correctness": False, "can_update_mastery": False, "total_tokens": 400,
            "privacy": {"original_frame_uploaded": False}, "page": {}, "provider_normalizations": [],
            "request_id": "artifact:artifact-renumbered", "latency_ms": 50, "text": "visible",
            "confidence": .69, "needs_confirmation": True, "reason": None,
            "provenance": {"source": "qwen_vl"},
        }
        with sqlite3.connect(client.app.state.settings.database_path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS document_vision_request(
                child_id TEXT, request_id TEXT, status TEXT, result_json TEXT)""")
            connection.execute("""INSERT INTO document_vision_request(
                child_id,request_id,image_hash,status,reserved_tokens,charged_tokens,created_at,result_json
                ) VALUES (?,?,?,'succeeded',0,0,datetime('now'),?)""",
                ("child-junior-3", receipt["request_id"], "safe-page", json.dumps(receipt, ensure_ascii=False)))
            details = json.loads(connection.execute(
                "SELECT details_json FROM capture_artifact WHERE artifact_id='artifact-renumbered'"
            ).fetchone()[0])
            details["cloud_vision_fallback"] = receipt
            connection.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='artifact-renumbered'",
                               (json.dumps(details, ensure_ascii=False),))
            connection.commit()

        response = client.post(
            "/v1/capture-artifacts/artifact-renumbered/mistake-candidates/prepare",
            json={"child_id": "child-junior-3", "client_action_id": "prepare-renumbered"},
        )
        assert response.status_code == 422
        assert "题号和相邻题目配错" in response.json()["message"]
        assert client.get(
            "/v1/capture-artifacts/artifact-renumbered/mistake-candidates",
            params={"child_id": "child-junior-3"},
        ).json()["items"] == []


def test_section_number_is_not_treated_as_a_question_number() -> None:
    from home_study_agent.mistake_capture import MistakeCaptureService

    details = {"local_pipeline": {"question_blocks": [
        {"text": "15.3 电热器\n①空气净化器采用静电吸附尘埃", "block_kind": "question"},
        {"text": "2. 规格分别为6V 3W和6V 6W的两白炽灯泡", "block_kind": "question"},
        {"text": "3. 探究电流通过导体产生热量的实验装置", "block_kind": "question"},
        {"text": "4. 探究电流通过导体时产生热量的因素", "block_kind": "question"},
    ]}}
    cloud = [
        {"text": "1. 下列日常生活的用电器说法正确的是", "source": "printed"},
        {"text": "2. 规格分别为6V 3W和6V 6W的两白炽灯泡", "source": "printed"},
        {"text": "3. 探究电流通过导体产生热量的实验装置", "source": "printed"},
        {"text": "4. 探究电流通过导体时产生热量的因素", "source": "printed"},
    ]
    MistakeCaptureService._require_cloud_question_alignment({}, details, cloud)


def test_visible_answers_are_kept_when_newness_is_not_required(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _artifact(client, "child-junior-3", "artifact-count-mismatch", [
            {"text": "1. 物理选择题", "confidence": .8, "box": None},
            {"text": "2. 物理选择题", "confidence": .8, "box": None},
        ])
        database_path = client.app.state.settings.database_path
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            artifact = connection.execute(
                "SELECT * FROM capture_artifact WHERE artifact_id='artifact-count-mismatch'"
            ).fetchone()
            connection.execute(
                """INSERT INTO homework_item(
                     homework_id,child_id,session_id,subject,task_type,title,range_text,
                     expected_minutes,special_requirements,confirmation_status,status,created_at,client_action_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',datetime('now'),?)""",
                (
                    "homework-count-mismatch", "child-junior-3", artifact["session_id"], "物理",
                    "worksheet", "物理试卷三题检查", "本次完成3道题", 20, None,
                    "confirmed", "homework-count-mismatch-action",
                ),
            )
            blocks = [
                {"kind": "text", "text": "1. 物理选择题", "bbox": [.1,.1,.8,.2],
                 "confidence": .8, "source": "printed", "evidence_type": "ocr_guess",
                 "review_required": True, "confidence_label": "unverified"},
                {"kind": "text", "text": "A", "bbox": [.2,.18,.25,.21],
                 "confidence": .8, "source": "handwritten", "evidence_type": "ocr_guess",
                 "review_required": True, "confidence_label": "unverified"},
                {"kind": "text", "text": "2. 物理选择题", "bbox": [.1,.3,.8,.4],
                 "confidence": .8, "source": "printed", "evidence_type": "ocr_guess",
                 "review_required": True, "confidence_label": "unverified"},
                {"kind": "text", "text": "B", "bbox": [.2,.38,.25,.41],
                 "confidence": .8, "source": "handwritten", "evidence_type": "ocr_guess",
                 "review_required": True, "confidence_label": "unverified"},
            ]
            receipt = {
                "schema_version": "document_vision.v1", "status": "needs_review",
                "model": "qwen3-vl-flash", "evidence_type": "ocr_guess",
                "review_required": True, "blocks": blocks, "can_judge_correctness": False,
                "can_update_mastery": False, "total_tokens": 400,
                "privacy": {"original_frame_uploaded": False}, "page": {},
                "provider_normalizations": [], "request_id": "artifact:artifact-count-mismatch",
                "latency_ms": 50, "text": "visible", "confidence": .8,
                "needs_confirmation": True, "reason": None,
                "provenance": {"source": "qwen_vl"},
            }
            connection.execute(
                """INSERT INTO document_vision_request(
                     child_id,request_id,image_hash,status,reserved_tokens,charged_tokens,created_at,result_json
                   ) VALUES (?,?,?,'succeeded',0,0,datetime('now'),?)""",
                ("child-junior-3", receipt["request_id"], "safe-page", json.dumps(receipt, ensure_ascii=False)),
            )
            details = json.loads(artifact["details_json"])
            details["cloud_vision_fallback"] = receipt
            connection.execute(
                "UPDATE capture_artifact SET details_json=? WHERE artifact_id='artifact-count-mismatch'",
                (json.dumps(details, ensure_ascii=False),),
            )
            connection.commit()

        response = client.post(
            "/v1/capture-artifacts/artifact-count-mismatch/mistake-candidates/prepare",
            json={"child_id": "child-junior-3", "client_action_id": "prepare-count-mismatch"},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 2
        assert all(item["region"]["declared_scope"] == {
            "declared_question_count": 3,
            "recognized_candidate_count": 2,
            "evidence_basis": "visible_answer_marks",
            "newness_not_required": True,
        } for item in items)
        assert len(client.get(
            "/v1/capture-artifacts/artifact-count-mismatch/mistake-candidates",
            params={"child_id": "child-junior-3"},
        ).json()["items"]) == 2


def test_local_ocr_fallback_keeps_recognized_questions_when_newness_is_not_required(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _artifact(client, "child-junior-3", "artifact-local-count-mismatch", [
            {"text": f"{number}. 物理选择题{number}", "confidence": .9, "box": None}
            for number in range(1, 5)
        ], local_pipeline={"question_blocks": [
            {"text": f"{number}. 物理选择题{number}", "block_kind": "question",
             "needs_manual_boundary_check": False}
            for number in range(1, 5)
        ]})
        database_path = client.app.state.settings.database_path
        with sqlite3.connect(database_path) as connection:
            artifact = connection.execute(
                "SELECT session_id FROM capture_artifact WHERE artifact_id='artifact-local-count-mismatch'"
            ).fetchone()
            connection.execute(
                """INSERT INTO homework_item(
                     homework_id,child_id,session_id,subject,task_type,title,range_text,
                     expected_minutes,special_requirements,confirmation_status,status,created_at,client_action_id
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',datetime('now'),?)""",
                (
                    "homework-local-count-mismatch", "child-junior-3", artifact[0], "物理",
                    "worksheet", "物理试卷三题检查", "本次完成3道题", 20, None,
                    "confirmed", "homework-local-count-mismatch-action",
                ),
            )
            connection.commit()

        response = client.post(
            "/v1/capture-artifacts/artifact-local-count-mismatch/mistake-candidates/prepare",
            json={"child_id": "child-junior-3", "client_action_id": "prepare-local-count-mismatch"},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 4
        assert all(item["region"]["declared_scope"] == {
            "declared_question_count": 3,
            "recognized_candidate_count": 4,
            "evidence_basis": "question_blocks",
            "newness_not_required": True,
        } for item in items)
        assert len(client.get(
            "/v1/capture-artifacts/artifact-local-count-mismatch/mistake-candidates",
            params={"child_id": "child-junior-3"},
        ).json()["items"]) == 4


def test_candidate_edit_skip_capture_and_artifact_delete_are_durable(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _artifact(client, "child-primary-4", "artifact-actions", [
            {"text": "1. 计算125×32。", "confidence": 0.95, "box": None},
            {"text": "2. 找出数列1、3、6、10的下一个数。", "confidence": 0.91, "box": None},
        ])
        items = client.post(
            "/v1/capture-artifacts/artifact-actions/mistake-candidates/prepare",
            json={"child_id": "child-primary-4", "client_action_id": "prepare-actions-001"},
        ).json()["items"]

        edited = client.put(
            f"/v1/mistake-capture-candidates/{items[0]['candidate_id']}",
            json={
                "child_id": "child-primary-4",
                "question_text": "1. 计算125×32，并写出简便过程。",
                "client_action_id": "edit-actions-001",
            },
        )
        assert edited.status_code == 200
        assert "简便过程" in edited.json()["question_text"]

        skipped = client.post(
            f"/v1/mistake-capture-candidates/{items[1]['candidate_id']}/skip",
            json={"child_id": "child-primary-4", "client_action_id": "skip-actions-001"},
        )
        assert skipped.status_code == 200
        assert skipped.json()["status"] == "skipped"

        candidates = client.get(
            "/v1/children/child-primary-4/mistake-knowledge-candidates",
            params={"q": "计算125×32简便过程", "subject": "数学"},
        ).json()["items"]
        assert candidates
        selected = candidates[0]
        captured = client.post(
            f"/v1/mistake-capture-candidates/{items[0]['candidate_id']}/capture",
            json={
                "child_id": "child-primary-4",
                "knowledge_node_id": selected["knowledge_node_id"],
                "subject": "数学",
                "title": selected["title"],
                "submitted_answer": "4000",
                "client_action_id": "capture-actions-001",
            },
        )
        assert captured.status_code == 200
        assert captured.json()["candidate"]["status"] == "saved"
        assert captured.json()["candidate"]["mistake_id"] == captured.json()["mistake"]["mistake_id"]
        repeated = client.post(
            f"/v1/mistake-capture-candidates/{items[0]['candidate_id']}/capture",
            json={
                "child_id": "child-primary-4",
                "knowledge_node_id": selected["knowledge_node_id"],
                "subject": "数学",
                "title": selected["title"],
                "submitted_answer": "4000",
                "client_action_id": "capture-actions-001",
            },
        )
        assert repeated.status_code == 200
        assert repeated.json()["mistake"]["mistake_id"] == captured.json()["mistake"]["mistake_id"]

        preview = client.app.state.privacy_lifecycle.deletion_preview("child-primary-4")
        assert preview["mistake_capture_candidates"] == 7
        exported = client.app.state.privacy_lifecycle.export_child("child-primary-4")
        payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        assert len(payload["mistake_capture_candidates"]) == 2
        assert len(payload["mistake_capture_candidate_events"]) == 5

    with _client(tmp_path) as restarted:
        items = restarted.get(
            "/v1/capture-artifacts/artifact-actions/mistake-candidates",
            params={"child_id": "child-primary-4"},
        ).json()["items"]
        assert [item["status"] for item in items] == ["saved", "skipped"]
        assert "简便过程" in items[0]["question_text"]
        assert restarted.get(
            "/v1/children/child-primary-4/mistake-capture-queues"
        ).json()["items"] == []

        with sqlite3.connect(restarted.app.state.settings.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM capture_artifact WHERE artifact_id = 'artifact-actions'")
        with sqlite3.connect(restarted.app.state.settings.database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM mistake_capture_candidate WHERE artifact_id = 'artifact-actions'"
            ).fetchone()[0] == 0
