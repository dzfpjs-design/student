from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.pet_vocabulary import PetVocabularyError, PetVocabularyService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.reports import build_daily_report
from home_study_agent.repository import StudyRepository


def setup_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    return database


def add_lexeme(database: Database, lexeme_id: str, term: str, senses: list[str], status: str = "confirmed") -> None:
    import hashlib
    transcript_root = database.path.parent.resolve(strict=True) / "registered-pet-transcripts"
    transcript_root.mkdir(mode=0o700, exist_ok=True)
    original = (term + ": " + json.dumps(senses, ensure_ascii=False)).encode("utf-8")
    with (transcript_root / (lexeme_id + ".txt")).open("xb") as stream:
        stream.write(original)
    transcript_hash = hashlib.sha256(original).hexdigest()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO kg_corpus(
              corpus_id,title,source_kind,transcript_root,source_ref,license_boundary,
              review_status,version_label,created_at,updated_at
            ) VALUES ('pet-corpus','南京教材词汇','document',?,'local-test',
              'family-local','approved','test-v1',datetime('now'),datetime('now'))
            """,
            (str(transcript_root),),
        )
        resource_id = f"resource-{lexeme_id}"
        connection.execute(
            """
            INSERT INTO kg_resource(
              resource_id,corpus_id,external_id,title,grade_scope,subject_scope,
              transcript_relative_path,transcript_sha256,metadata_json,source_ref,
              license_boundary,confidence,review_status,version_label,ingested_at,updated_at
            ) VALUES (?, 'pet-corpus', ?, ?, '四年级', '英语', ?, ?, '{}', ?,
              'family-local', 1, 'approved', 'test-v1', datetime('now'), datetime('now'))
            """,
            (resource_id, lexeme_id, term, f"{lexeme_id}.txt", transcript_hash, f"document_page:{resource_id}:page:1"),
        )
        connection.execute(
            """
            INSERT INTO kg_language_lexeme(
              lexeme_id,language,surface,focus_kind,normalized,senses_json,collocations_json,
              confusables_json,pronunciations_json,compounds_json,common_errors_json,source_ref,
              version_label,license_boundary,created_at,updated_at
            ) VALUES (?, 'english', ?, 'word', ?, ?, '[]', '[]', '[]', '[]', '[]', ?,
              'test-v1','family-local',datetime('now'),datetime('now'))
            """,
            (lexeme_id, term, term.lower(), json.dumps(senses, ensure_ascii=False), f"document_page:{resource_id}:page:1"),
        )
        connection.execute(
            """
            INSERT INTO kg_language_focus_candidate(
              candidate_id,resource_id,language,term,normalized,focus_kind,proposed_json,
              status,created_at,updated_at
            ) VALUES (?, ?, 'english', ?, ?, 'word', ?, ?, datetime('now'), datetime('now'))
            """,
            (f"candidate-{lexeme_id}", resource_id, term, term.lower(), json.dumps({"senses": senses}, ensure_ascii=False), status),
        )
        connection.commit()


def test_confirmed_textbook_words_become_isolated_due_cards_and_survive_restart(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "pet-vocabulary.db")
    add_lexeme(database, "lexeme-collect", "collect", ["领取", "收集"])
    add_lexeme(database, "lexeme-ignore", "ignore", ["忽略"], status="pending")
    add_lexeme(database, "lexeme-no-meaning", "empty", [])
    service = PetVocabularyService(database)

    synced = service.sync_confirmed_textbook_lexemes("child-primary-4")
    card = service.next_card("child-primary-4")

    assert synced == {"available_confirmed": 2, "inserted": 1, "skipped_without_meaning": 1, "deck_size": 1}
    assert card is not None
    assert card["review_mode"] == "recognition"
    assert "collect" in card["prompt"]
    assert service.summary("child-junior-3")["deck_size"] == 0
    assert PetVocabularyService(Database(database.path)).summary("child-primary-4")["due_count"] == 1


def test_review_alternates_recall_and_recognition_with_durable_interval_and_lapse(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "pet-vocabulary.db")
    add_lexeme(database, "lexeme-collect", "collect", ["领取", "收集"])
    service = PetVocabularyService(database)
    service.sync_confirmed_textbook_lexemes("child-primary-4")
    start = datetime(2030, 9, 1, 12, tzinfo=timezone.utc)
    card = service.next_card("child-primary-4", start)
    assert card is not None

    correct = service.submit("child-primary-4", card["card_id"], "领取", "pet-vocab-answer-001", start)
    with pytest.raises(PetVocabularyError, match='payload_conflict'):
        service.submit("child-primary-4", card["card_id"], "different", "pet-vocab-answer-001", start)
    replay = service.submit("child-primary-4", card["card_id"], "领取", "pet-vocab-answer-001", start)
    assert correct == replay
    assert correct["correct"] is True
    assert correct["interval_days"] == 1
    assert correct["next_due_at"] == (start + timedelta(days=1)).isoformat()

    recall = service.next_card("child-primary-4", start + timedelta(days=1))
    assert recall is not None and recall["review_mode"] == "recall"
    assert "领取" in recall["prompt"]
    missed = service.submit(
        "child-primary-4", recall["card_id"], "borrow", "pet-vocab-answer-002", start + timedelta(days=1)
    )
    assert missed["correct"] is False
    assert missed["interval_days"] == 0
    assert missed["next_due_at"] == (start + timedelta(days=1, minutes=10)).isoformat()
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status,total_attempts,lapse_count,consecutive_successes FROM pet_vocabulary_card"
        ).fetchone()
        assert tuple(row) == ("learning", 2, 1, 0)
        assert connection.execute("SELECT COUNT(*) FROM pet_vocabulary_attempt").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='pet_vocabulary_reviewed'"
        ).fetchone()[0] == 2


def test_non_pet_child_cannot_create_a_pet_deck(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "pet-vocabulary.db")
    service = PetVocabularyService(database)
    with pytest.raises(PetVocabularyError):
        service.sync_confirmed_textbook_lexemes("child-junior-3")


def test_presented_card_is_durable_and_voice_answer_completion_is_idempotent(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "pet-vocabulary.db")
    add_lexeme(database, "lexeme-library", "library", ["图书馆"])
    service = PetVocabularyService(database)
    service.sync_confirmed_textbook_lexemes("child-primary-4")
    session_id = StudyRepository(database).start_session("child-primary-4", "pet-vocab-session-001")["session_id"]

    first = service.present_next(session_id)
    restored = PetVocabularyService(Database(database.path)).active_prompt(session_id)
    assert restored == first

    result = service.submit_active(session_id, "图书馆", "pet-vocab-voice-answer-001")
    with pytest.raises(PetVocabularyError, match='payload_conflict'):
        service.submit_active(session_id, "完全不同", "pet-vocab-voice-answer-001")
    replay = service.submit_active(session_id, "图书馆", "pet-vocab-voice-answer-001")
    assert result == replay
    assert result["correct"] is True
    assert service.active_prompt(session_id) is None


def test_http_and_agent_voice_round_persist_across_app_restart(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "pet-http.db",
        data_encryption_key=b"v" * 32,
        maintenance_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        add_lexeme(Database(settings.database_path), "lexeme-library-http", "library", ["图书馆"])
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "pet-http-session-001"},
        ).json()
        session_id = session["session_id"]
        client.post(f"/v1/sessions/{session_id}/transition", json={
            "expected_state": "check_in", "target_state": "classroom_recall", "reason": "ready",
            "client_action_id": "pet-http-transition-001",
        })
        client.post(f"/v1/sessions/{session_id}/recalls/none", json={"client_action_id": "pet-http-recall-none"})
        client.post(f"/v1/sessions/{session_id}/transition", json={
            "expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "done",
            "client_action_id": "pet-http-transition-002",
        })
        client.post(f"/v1/sessions/{session_id}/homework/none", json={"client_action_id": "pet-http-homework-none"})
        plan = client.post(
            f"/v1/sessions/{session_id}/plan/generate",
            json={"client_action_id": "pet-http-plan-001"},
        ).json()
        pet_items = [item for item in plan["plan_items"] if item["source_type"] == "pet_daily"]
        assert any(item['task_type'] != 'memorization' for item in pet_items)
        pet_item = next(item for item in pet_items if item['task_type'] == 'memorization')
        assert pet_item["source_ref"] == "pet-vocabulary-due:1"
        assert pet_item["title"] == "PET词汇间隔复习 · 1张到期"
        assert pet_item["task_type"] == "memorization"
        client.post(f"/v1/sessions/{session_id}/transition", json={
            "expected_state": "plan_review", "target_state": "study", "reason": "confirmed",
            "client_action_id": "pet-http-transition-003",
        })
        client.post(
            f"/v1/plan-items/{pet_item['plan_item_id']}/start",
            json={"client_action_id": "pet-http-plan-start-001"},
        )

        start_turn = client.post(f"/v1/sessions/{session_id}/agent-turn", json={
            "transcript": "背单词", "client_action_id": "pet-http-agent-start",
        })
        presented = client.get(f"/v1/sessions/{session_id}/pet-vocabulary/next")
        missing_evidence = client.post(
            f"/v1/plan-items/{pet_item['plan_item_id']}/complete",
            json={
                "actual_minutes": 8,
                "completion_evidence_type": "child_checked",
                "completion_checklist": ["recall_attempted", "uncertain_parts_marked"],
                "client_action_id": "pet-http-plan-premature-complete-001",
            },
        )
        answer_turn = client.post(f"/v1/sessions/{session_id}/agent-turn", json={
            "transcript": "图书馆", "client_action_id": "pet-http-agent-answer",
        })
        assert answer_turn.json()['action']['source'] == 'pending_answer_clarification'
        assert client.post(f"/v1/agent-turns/{answer_turn.json()['agent_turn_id']}/execute").status_code == 200
        with Database(settings.database_path).connect() as c:
            assert c.execute('SELECT count(*) FROM pet_vocabulary_attempt').fetchone()[0] == 0
        confirmed_turn = client.post(f"/v1/sessions/{session_id}/agent-turn", json={
            'transcript': '刚才那句就是我的答案', 'client_action_id': 'pet-http-confirm-answer'})
        assert confirmed_turn.status_code == 200, confirmed_turn.text
        card = presented.json()['card']
        answered = client.post(f"/v1/sessions/{session_id}/pet-vocabulary/prompt/answer", json={
            'prompt_session_id': card['prompt_session_id'], 'card_version': card['card_version'],
            "answer": "图书馆", "client_action_id": "pet-http-answer-001",
            'authorized_turn_id': confirmed_turn.json()['agent_turn_id'],
        })
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        parent = client.get(
            "/v1/parent/children/child-primary-4/pet-vocabulary",
            headers={"X-HSC-Parent": token},
        )
        completed_item = client.post(
            f"/v1/plan-items/{pet_item['plan_item_id']}/complete",
            json={
                "actual_minutes": 8,
                "completion_evidence_type": "child_checked",
                "completion_checklist": ["recall_attempted", "uncertain_parts_marked"],
                "client_action_id": "pet-http-plan-complete-001",
            },
        )

    assert start_turn.status_code == 200
    assert start_turn.json()["action"]["type"] == "start_pet_vocabulary_review"
    assert presented.status_code == 200 and "library" in presented.json()["card"]["prompt"]
    assert missing_evidence.status_code == 422
    assert "1张词汇卡" in missing_evidence.json()["message"]
    assert confirmed_turn.json()["action"] == {
        "type": "submit_pet_vocabulary_answer", "answer": "图书馆",
        'answer_source_turn_id': answer_turn.json()['agent_turn_id'],
        'local_only': True, 'cloud_transmission_allowed': False,
        'vocabulary_prompt_id': presented.json()['card']['prompt_session_id'],
        'vocabulary_card_version': presented.json()['card']['card_version'],
    }
    assert answered.status_code == 200 and answered.json()["attempt"]["correct"] is True
    assert answered.json()["review"]["next_card"] is None
    assert parent.status_code == 200 and parent.json()["summary"]["total_attempts"] == 1
    assert completed_item.status_code == 200
    assert completed_item.json()["status"] == "completed"

    with TestClient(create_app(settings)) as restarted:
        token = restarted.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        restored = restarted.get(
            "/v1/parent/children/child-primary-4/pet-vocabulary",
            headers={"X-HSC-Parent": token},
        ).json()
    assert restored["summary"]["deck_size"] == 1
    assert restored["summary"]["total_attempts"] == 1
    assert restored["summary"]["due_count"] == 0


def test_vocabulary_evidence_is_exported_reported_and_deleted_with_learning_scope(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "pet-lifecycle.db")
    add_lexeme(database, "lexeme-collect-lifecycle", "collect", ["收集"])
    vocabulary = PetVocabularyService(database)
    vocabulary.sync_confirmed_textbook_lexemes("child-primary-4")
    session_id = StudyRepository(database).start_session(
        "child-primary-4", "pet-lifecycle-session-001"
    )["session_id"]
    vocabulary.present_next(session_id)
    vocabulary.submit_active(session_id, "收集", "pet-lifecycle-answer-001")

    lifecycle = PrivacyLifecycleService(database, tmp_path, b'0' * 32)
    lifecycle.initialize()
    exported = lifecycle.export_child("child-primary-4")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    preview = lifecycle.deletion_preview("child-primary-4")
    summary, _ = build_daily_report({
        "session": {"recall_declared_empty": 0, "homework_declared_empty": 0},
        "child": {"grade": "四年级"},
        "reflection": {"learned_text": "", "difficult_text": "", "feeling": "", "next_action": "", "skipped": True},
        "tasks": [], "recalls": [],
        "pet_vocabulary_attempts": [{"surface": "collect", "correct": 1}],
    })
    deleted = lifecycle.delete_child_scope(
        "child-primary-4", "learning", "确认删除", "pet-lifecycle-delete-001"
    )

    assert len(payload["pet_vocabulary_cards"]) == 1
    assert len(payload["pet_vocabulary_attempts"]) == 1
    assert len(payload["pet_vocabulary_prompt_sessions"]) == 1
    assert preview["pet_vocabulary"] == 3
    assert any("PET词汇间隔复习" in fact and "collect" in fact for fact in summary["facts"])
    assert deleted["status"] == "completed"
    assert lifecycle.deletion_preview("child-primary-4")["pet_vocabulary"] == 0
