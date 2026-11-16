from __future__ import annotations

from pathlib import Path
import hashlib
import json
import sqlite3
import time

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelRequest, ModelResult


def client_for(tmp_path: Path, model_gateway=None) -> TestClient:
    return TestClient(
        create_app(
            Settings(
                data_dir=tmp_path,
                database_path=tmp_path / "home-study.db",
                session_token=None,
                data_encryption_key=b"0" * 32,
                maintenance_enabled=False,
            ),
            model_gateway=model_gateway,
        )
    )


class RecordingGateway:
    configured = True

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return ModelResult(
            content="先想一想：小数点对齐后，每一列表示的计数单位是否相同？",
            provider="fake-provider",
            model="fake-model",
            latency_ms=8,
            prompt_tokens=120,
            completion_tokens=25,
        )


class InternalizationGateway:
    configured = True

    def __init__(self, spoof_source: bool = False) -> None:
        self.spoof_source = spoof_source
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        payload = json.loads(request.messages[-1]["content"])
        topic_ids = [item["topic_id"] for item in payload["topics"]]
        standard_evidence = payload["official_standard_evidence"]
        standard_alignments = []
        if standard_evidence:
            evidence = standard_evidence[0]
            quote = evidence["content_excerpt"].strip()[:40]
            standard_alignments = [{
                "source_ref": evidence["source_ref"], "source_quote": quote,
                "requirement_type": "content_requirement", "stage_scope": "第二学段（3~4年级）",
                "requirement_text": "学生应理解小数意义并能进行简单的小数加减运算。",
                "observable_outcome": "学生能说明数位对齐理由并独立完成简单小数加法。",
            }]
        if self.spoof_source:
            topic_ids = ["topic-not-in-input"]
        return ModelResult(
            content=json.dumps({"capabilities": [{
                "title": "按数位对齐进行小数加法",
                "capability_type": "procedure",
                "learning_objective": "能说明小数点对齐表示相同计数单位相加，并独立完成计算。",
                "prerequisites": ["理解小数数位和计数单位"],
                "misconceptions": ["把末位数字对齐而不是把小数点对齐"],
                "mastery_criteria": ["连续两道不同位数小数加法独立正确并能说明理由"],
                "diagnostic_blueprint": ["识别数位", "独立计算", "变式迁移或延迟复测"],
                "explanation_strategies": ["用元角分或数位表解释相同单位才能相加"],
                "transfer_goals": ["迁移到小数减法"],
                "source_topic_ids": topic_ids,
                "standard_alignments": standard_alignments,
                "confidence": 0.86,
            }]}, ensure_ascii=False),
            provider="fake-provider", model="fake-structurer-v1", latency_ms=12,
            prompt_tokens=600, completion_tokens=320,
        )


def create_seyk_source(root: Path) -> tuple[Path, Path, Path]:
    root.mkdir()
    database_path = root / "seyk_kb.db"
    transcript_root = root / "transcripts"
    video_root = root / "videos"
    transcript_root.mkdir()
    video_root.mkdir()
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE resources (
          resource_id TEXT PRIMARY KEY,
          title TEXT,
          description TEXT,
          teacher TEXT,
          video_url TEXT,
          module TEXT,
          file_id TEXT,
          video_name TEXT,
          video_size INTEGER,
          video_duration INTEGER,
          video_downloaded INTEGER DEFAULT 0,
          transcript TEXT,
          transcribed INTEGER DEFAULT 0
        );
        CREATE TABLE resource_tree (
          resource_id TEXT,
          stage_id INTEGER,
          stage_title TEXT,
          grade_id INTEGER,
          grade_title TEXT,
          subject_id TEXT,
          subject_title TEXT,
          version_id TEXT,
          version_title TEXT,
          volumn_id TEXT,
          volumn_title TEXT,
          unit_path TEXT,
          dir_id TEXT,
          dir_level INTEGER,
          chapter_title TEXT,
          PRIMARY KEY(resource_id, dir_id)
        );
        """
    )
    rows = [
        (
            "106582", "小数加法", "理解小数点对齐", "王老师", "数学四年级小数加法",
            "小学", "四年级", "小学数学", "苏教版", "下册", "下册/第六单元/小数加法", 720,
        ),
        (
            "206001", "有理数混合运算", "掌握运算顺序", "李老师", "初一有理数混合运算与乘方",
            "初中", "初三", "初中数学", "苏科版", "上册", "上册/第二章/有理数", 900,
        ),
        (
            "206002", "概率入门", "随机事件", "赵老师", "",
            "初中", "初一", "初中数学", "苏科版", "下册", "下册/第八章/概率", 600,
        ),
    ]
    for resource_id, title, description, teacher, transcript, stage, grade, subject, version, volume, unit_path, duration in rows:
        connection.execute(
            """
            INSERT INTO resources(
              resource_id, title, description, teacher, video_name, video_duration,
              video_downloaded, transcript, transcribed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_id, title, description, teacher, title, duration,
                int(bool(transcript)), transcript, int(bool(transcript)),
            ),
        )
        connection.execute(
            """
            INSERT INTO resource_tree(
              resource_id, stage_id, stage_title, grade_id, grade_title,
              subject_id, subject_title, version_id, version_title,
              volumn_id, volumn_title, unit_path, dir_id, dir_level, chapter_title
            ) VALUES (?, 1, ?, 4, ?, 'math', ?, 'v1', ?, 'vol1', ?, ?, ?, 1, ?)
            """,
            (resource_id, stage, grade, subject, version, volume, unit_path, resource_id, title),
        )
        if transcript:
            (transcript_root / f"{resource_id}.txt").write_text(transcript, encoding="utf-8")
            (video_root / f"{resource_id}.mp4").write_bytes(b"test-video")
    connection.commit()
    connection.close()
    return database_path, transcript_root, video_root


def import_payload(database_path: Path, transcript_root: Path, video_root: Path) -> dict:
    return {
        "title": "苏e优课四至九年级课程库",
        "database_path": str(database_path),
        "transcript_root": str(transcript_root),
        "video_root": str(video_root),
        "client_action_id": "import-seyk-corpus-001",
    }


def create_official_math_standard(root: Path, content: str) -> Path:
    root.mkdir()
    pdf = root / "mathematics.pdf"
    pdf.write_bytes(b"official-pdf-test")
    pages = root / "mathematics.ocr.pages.jsonl"
    pages.write_text(json.dumps({"page": 1, "text": content, "char_count": len(content)}, ensure_ascii=False) + "\n", encoding="utf-8")
    text_file = root / "mathematics.ocr.txt"
    text_file.write_text(content, encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "format": "hsc_official_curriculum_sources_v1", "publisher": "中华人民共和国教育部",
        "sources": [{
            "source_key": "moe-2022-mathematics", "jurisdiction": "CN",
            "source_type": "national_standard", "subject": "数学",
            "title": "义务教育数学课程标准（2022年版）", "version": "2022",
            "official_page_url": "http://www.moe.gov.cn/official",
            "pdf_filename": pdf.name, "page_count": 1,
            "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "ocr_status": "completed", "ocr_pages_filename": pages.name,
            "ocr_text_filename": text_file.name,
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return root


def test_parent_imports_searches_and_deletes_shared_curriculum_corpus(tmp_path: Path) -> None:
    source = create_seyk_source(tmp_path / "source")
    payload = import_payload(*source)

    with client_for(tmp_path) as client:
        unauthorized = client.post("/v1/parent/knowledge-corpora", json=payload)
        token = client.post("/v1/parent/setup", json={"pin": "8642"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        imported = client.post("/v1/parent/knowledge-corpora", headers=headers, json=payload)
        repeated = client.post("/v1/parent/knowledge-corpora", headers=headers, json=payload)
        listed = client.get("/v1/parent/knowledge-corpora", headers=headers)
        coverage = client.get(
            f"/v1/parent/knowledge-corpora/{imported.json()['corpus_id']}/coverage",
            headers=headers,
        )
        topics = client.get(
            "/v1/parent/children/child-primary-4/curriculum-topics",
            headers=headers,
            params={"q": "小数加法", "subject": "数学"},
        )
        primary_search = client.get(
            "/v1/parent/children/child-primary-4/knowledge-corpus/search",
            headers=headers,
            params={"q": "小数加法", "subject": "数学"},
        )
        junior_search = client.get(
            "/v1/parent/children/child-junior-3/knowledge-corpus/search",
            headers=headers,
            params={"q": "有理数混合运算", "subject": "数学"},
        )

        assert unauthorized.status_code == 401
        assert imported.status_code == 200
        corpus = imported.json()
        assert corpus["status"] == "ready"
        assert corpus["resource_count"] == 3
        assert corpus["transcript_count"] == 2
        assert corpus["local_video_count"] == 2
        assert corpus["topic_count"] == 3
        assert corpus["model_drafted_topic_count"] == 0
        assert corpus["verified_topic_count"] == 0
        assert repeated.json()["corpus_id"] == corpus["corpus_id"]
        assert len(listed.json()["items"]) == 1
        assert any(item["grade_title"] == "四年级" and item["semester"] == "下册" for item in coverage.json()["items"])
        assert any(item["grade_title"] == "初三" and item["semester"] == "上册" for item in coverage.json()["items"])
        assert topics.json()["items"][0]["title"] == "小数加法"
        assert topics.json()["items"][0]["maturity"] == "source_structured"
        assert topics.json()["items"][0]["learning_objective"] == "理解小数点对齐"
        assert primary_search.json()["items"][0]["external_resource_id"] == "106582"
        assert primary_search.json()["items"][0]["grade_title"] == "四年级"
        assert primary_search.json()["items"][0]["video_available"] is True
        assert primary_search.json()["items"][0]["video_path"].endswith("106582.mp4")
        assert "小数加法" in primary_search.json()["items"][0]["excerpt"]
        assert junior_search.json()["items"][0]["external_resource_id"] == "206001"

        deleted = client.delete(
            f"/v1/parent/knowledge-corpora/{corpus['corpus_id']}", headers=headers
        )
        empty = client.get(
            "/v1/parent/children/child-primary-4/knowledge-corpus/search",
            headers=headers,
            params={"q": "小数加法"},
        )

    assert deleted.json() == {"status": "deleted", "resource_count": 3}
    assert empty.json()["items"] == []


def test_curriculum_corpus_survives_restart_and_hides_local_paths_from_list(tmp_path: Path) -> None:
    source = create_seyk_source(tmp_path / "source")
    payload = import_payload(*source)

    with client_for(tmp_path) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        imported = client.post(
            "/v1/parent/knowledge-corpora",
            headers={"X-HSC-Parent": token},
            json=payload,
        ).json()

    with client_for(tmp_path) as restarted:
        token = restarted.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        listed = restarted.get("/v1/parent/knowledge-corpora", headers=headers).json()["items"]
        searched = restarted.get(
            "/v1/parent/children/child-primary-4/knowledge-corpus/search",
            headers=headers,
            params={"q": "小数点对齐"},
        ).json()["items"]

    assert listed[0]["corpus_id"] == imported["corpus_id"]
    assert "database_path" not in listed[0]
    assert "transcript_root" not in listed[0]
    assert "video_root" not in listed[0]
    assert searched[0]["title"] == "小数加法"


def test_curriculum_corpus_rejects_untrusted_database_shape(tmp_path: Path) -> None:
    invalid_database = tmp_path / "invalid.db"
    sqlite3.connect(invalid_database).close()

    with client_for(tmp_path) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        response = client.post(
            "/v1/parent/knowledge-corpora",
            headers={"X-HSC-Parent": token},
            json={
                "title": "无效课程库",
                "database_path": str(invalid_database),
                "transcript_root": str(tmp_path),
                "video_root": str(tmp_path),
                "client_action_id": "invalid-corpus-import-001",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_KNOWLEDGE_SOURCE"


def test_curriculum_corpus_proposes_low_confidence_recall_candidate(tmp_path: Path) -> None:
    source = create_seyk_source(tmp_path / "source")
    with client_for(tmp_path) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        client.post(
            "/v1/parent/knowledge-corpora",
            headers={"X-HSC-Parent": token},
            json=import_payload(*source),
        )
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "corpus-recall-session-001"},
        ).json()
        client.post(
            f"/v1/sessions/{session['session_id']}/transition",
            json={"expected_state": "check_in", "target_state": "classroom_recall", "reason": "ready", "client_action_id": "corpus-recall-transition-001"},
        )
        client.post(
            f"/v1/sessions/{session['session_id']}/recalls",
            json={"subject": "数学", "summary": "今天讲了小数点对齐后再做加法。", "client_action_id": "corpus-recall-add-001"},
        )
        workspace = client.get(f"/v1/sessions/{session['session_id']}").json()

    candidate = next(item for item in workspace["recalls"] if item["record_kind"] == "system_candidate")
    assert candidate["confirmation_status"] == "inferred"
    assert candidate["candidate_nodes"] == [{"title": "小数加法"}]
    assert candidate["textbook_mapping"] == {
        "status": "public_course_baseline",
        "label": "苏e优课公开课程基线，仍需孩子确认",
    }
    assert candidate["evidence_sources"] == ["孩子口述", "苏e优课公开课程"]
    assert candidate["confidence"] == 0.55


def test_tutor_receives_bounded_public_course_grounding_without_media(tmp_path: Path) -> None:
    source = create_seyk_source(tmp_path / "source")
    gateway = RecordingGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        client.post(
            "/v1/parent/knowledge-corpora",
            headers={"X-HSC-Parent": token},
            json=import_payload(*source),
        )
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "corpus-tutor-session-001"},
        ).json()
        session_id = session["session_id"]
        client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "check_in", "target_state": "classroom_recall", "reason": "ready", "client_action_id": "corpus-tutor-step-001"})
        client.post(f"/v1/sessions/{session_id}/recalls", json={"subject": "数学", "summary": "今天学习了小数加法。", "client_action_id": "corpus-tutor-step-002"})
        client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "done", "client_action_id": "corpus-tutor-step-003"})
        client.post(f"/v1/sessions/{session_id}/homework", json={"subject": "数学", "title": "小数加法练习", "expected_minutes": 20, "client_action_id": "corpus-tutor-step-004"})
        client.post(f"/v1/sessions/{session_id}/plan/generate", json={"client_action_id": "corpus-tutor-step-005"})
        client.post(f"/v1/sessions/{session_id}/transition", json={"expected_state": "plan_review", "target_state": "study", "reason": "confirmed", "client_action_id": "corpus-tutor-step-006"})
        response = client.post(
            f"/v1/sessions/{session_id}/tutor",
            json={"subject": "数学", "question": "小数加法时小数点为什么要对齐？", "hint_level": 1, "client_action_id": "corpus-tutor-question-001"},
        )

    assert response.status_code == 200
    request = gateway.requests[-1]
    assert "公开课程参考" in request.messages[-1]["content"]
    assert "小数加法" in request.messages[-1]["content"]
    assert request.context_manifest["knowledge_excerpt_count"] == 1
    assert request.context_manifest["raw_audio_or_video"] is False
    assert any(item.startswith("curriculum:") for item in request.evidence_refs)


def test_parent_internalizes_and_reviews_executable_curriculum_capability(tmp_path: Path) -> None:
    source = create_seyk_source(tmp_path / "source")
    standards = create_official_math_standard(
        tmp_path / "standards",
        "小数加法学习中，学生理解小数的意义，能进行简单的小数加减运算，并形成运算能力。",
    )
    gateway = InternalizationGateway()
    with client_for(tmp_path, model_gateway=gateway) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        assert client.put('/v1/parent/privacy', headers=headers,
            json={'cloud_model_text_allowed': True}).status_code == 200
        imported_standards = client.post(
            "/v1/parent/curriculum-standards/import", headers=headers,
            json={"root_path": str(standards)},
        )
        assert imported_standards.status_code == 200
        corpus = client.post(
            "/v1/parent/knowledge-corpora", headers=headers, json=import_payload(*source)
        ).json()
        scopes = client.get(
            f"/v1/parent/knowledge-corpora/{corpus['corpus_id']}/internalization-scopes",
            headers=headers, params={"child_id": "child-primary-4"},
        ).json()["items"]
        assert any(item["subject_title"] == "小学数学" and item["semester"] == "下册" for item in scopes)

        started = client.post(
            f"/v1/parent/knowledge-corpora/{corpus['corpus_id']}/internalization-jobs",
            headers=headers,
            json={
                "child_id": "child-primary-4", "subject_title": "小学数学",
                "version_title": "苏教版", "semester": "下册", "max_units": 1,
                "client_action_id": "internalize-math-unit-001",
            },
        )
        assert started.status_code == 200
        job_id = started.json()["job_id"]
        for _ in range(50):
            jobs = client.get(
                f"/v1/parent/knowledge-corpora/{corpus['corpus_id']}/internalization-jobs",
                headers=headers, params={"child_id": "child-primary-4"},
            ).json()["items"]
            job = next(item for item in jobs if item["job_id"] == job_id)
            if job["status"] != "processing":
                break
            time.sleep(0.01)
        assert job["status"] == "completed"
        assert job["capability_count"] == 1

        capabilities = client.get(
            "/v1/parent/children/child-primary-4/curriculum-capabilities",
            headers=headers, params={"subject": "小学数学"},
        ).json()["items"]
        assert capabilities[0]["maturity"] == "model_drafted"
        assert capabilities[0]["source_titles"] == ["小数加法"]
        assert len(capabilities[0]["diagnostic_blueprint"]) == 3
        reviewed = client.post(
            f"/v1/parent/curriculum-capabilities/{capabilities[0]['capability_id']}/review",
            headers=headers, json={"decision": "verify", "note": "已对照课程证据确认"},
        )
        assert reviewed.json()["maturity"] == "verified"
        assert gateway.requests[0].output_type == "knowledge_structuring"
        assert "mp4" not in gateway.requests[0].messages[-1]["content"]
        model_payload = json.loads(gateway.requests[0].messages[-1]["content"])
        assert model_payload["official_standard_evidence"]

    connection = sqlite3.connect(tmp_path / "home-study.db")
    assert connection.execute(
        "SELECT COUNT(*) FROM curriculum_topic_standard_candidate WHERE alignment_status = 'candidate'"
    ).fetchone()[0] >= 1
    assert connection.execute("SELECT COUNT(*) FROM curriculum_standard_requirement").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM curriculum_standard_topic_alignment").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM curriculum_capability_standard").fetchone()[0] == 1
    connection.close()


def test_internalization_rejects_spoofed_source_topic_atomically(tmp_path: Path) -> None:
    source = create_seyk_source(tmp_path / "source")
    with client_for(tmp_path, model_gateway=InternalizationGateway(spoof_source=True)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        assert client.put('/v1/parent/privacy', headers=headers,
            json={'cloud_model_text_allowed': True}).status_code == 200
        corpus = client.post(
            "/v1/parent/knowledge-corpora", headers=headers, json=import_payload(*source)
        ).json()
        started = client.post(
            f"/v1/parent/knowledge-corpora/{corpus['corpus_id']}/internalization-jobs",
            headers=headers,
            json={
                "child_id": "child-primary-4", "subject_title": "小学数学",
                "version_title": "苏教版", "semester": "下册", "max_units": 1,
                "client_action_id": "spoofed-topic-job-001",
            },
        ).json()
        for _ in range(50):
            job = client.get(
                f"/v1/parent/knowledge-corpora/{corpus['corpus_id']}/internalization-jobs",
                headers=headers, params={"child_id": "child-primary-4"},
            ).json()["items"][0]
            if job["status"] != "processing":
                break
            time.sleep(0.01)
        capabilities = client.get(
            "/v1/parent/children/child-primary-4/curriculum-capabilities", headers=headers
        ).json()["items"]
    assert started["status"] == "processing"
    assert job["status"] == "failed"
    assert job["error_code"] == "INVALID_INTERNALIZATION_OUTPUT"
    assert capabilities == []


def test_parent_imports_hashed_official_standard_pages_and_searches_them(tmp_path: Path) -> None:
    root = create_official_math_standard(
        tmp_path / "official-standards",
        "第二学段学生理解小数的意义，能进行简单的小数加减运算。学业要求包括形成数感、运算能力和初步的推理意识。",
    )

    with client_for(tmp_path) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        imported = client.post(
            "/v1/parent/curriculum-standards/import", headers=headers,
            json={"root_path": str(root)},
        )
        listed = client.get("/v1/parent/curriculum-standards", headers=headers)
        searched = client.get(
            "/v1/parent/curriculum-standards/search", headers=headers,
            params={"q": "小数加减运算", "subject": "数学"},
        )

    assert imported.json() == {"status": "imported", "source_count": 1, "page_count": 1}
    assert listed.json()["items"][0]["status"] == "ocr_ready"
    assert "小数加减运算" in searched.json()["items"][0]["excerpt"]
    assert "local_root_path" not in listed.json()["items"][0]


def test_every_course_topic_gets_truthful_standard_alignment_audit_state(tmp_path: Path) -> None:
    source = create_seyk_source(tmp_path / "source")
    standards = create_official_math_standard(
        tmp_path / "official-standards",
        "第二学段学生理解小数的意义，能进行简单的小数加减运算，并形成运算能力。",
    )
    with client_for(tmp_path) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        client.post(
            "/v1/parent/curriculum-standards/import", headers=headers,
            json={"root_path": str(standards)},
        )
        corpus = client.post(
            "/v1/parent/knowledge-corpora", headers=headers, json=import_payload(*source)
        ).json()
        summary = client.get(
            f"/v1/parent/knowledge-corpora/{corpus['corpus_id']}/curriculum-standard-alignment",
            headers=headers,
        )

    assert summary.status_code == 200
    assert summary.json()["total_topic_count"] == 3
    assert summary.json()["candidate_topic_count"] >= 1
    assert summary.json()["candidate_topic_count"] + summary.json()["unresolved_topic_count"] == 3
    connection = sqlite3.connect(tmp_path / "home-study.db")
    rows = connection.execute(
        "SELECT alignment_status, source_id, page_number FROM curriculum_topic_standard_candidate"
    ).fetchall()
    connection.close()
    assert len(rows) == 3
    assert any(status == "candidate" and source_id and page_number == 1 for status, source_id, page_number in rows)
