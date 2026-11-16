"""Bound vocabulary receipts on synthetic, disposable SQLite databases."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.pet_vocabulary import PetVocabularyError, PetVocabularyService
from home_study_agent.repository import StudyRepository


CHILD = "child-primary-4"
OTHER = "child-junior-3"
NOW = datetime(2030, 9, 1, 12, tzinfo=timezone.utc)


@pytest.fixture
def flow(tmp_path):
    db = Database(tmp_path / "bound-vocabulary.db")
    db.initialize()
    repository = StudyRepository(db)
    repository.seed_family()
    sid = repository.start_session(CHILD, "vocabulary-session")["session_id"]
    with db.connect() as c:
        c.execute("""INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,source_ref,
            license_boundary,review_status,version_label,created_at,updated_at)
            VALUES('synthetic','Synthetic vocabulary','document','/synthetic','test','local',
                   'approved','v1',?,?)""", (NOW.isoformat(), NOW.isoformat()))
        for word, meaning in (("library", "place for books"), ("collect", "gather things")):
            c.execute("""INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,grade_scope,subject_scope,
                transcript_relative_path,transcript_sha256,metadata_json,source_ref,license_boundary,confidence,
                review_status,version_label,ingested_at,updated_at)
                VALUES(?,'synthetic',?,?,'四年级','English',?,'synthetic','{}',?,'local',1,'approved','v1',?,?)""",
                (word, word, word, word + ".txt", "document_page:" + word + ":page:1", NOW.isoformat(), NOW.isoformat()))
            c.execute("""INSERT INTO kg_language_lexeme(lexeme_id,language,surface,focus_kind,normalized,
                senses_json,collocations_json,confusables_json,pronunciations_json,compounds_json,
                common_errors_json,source_ref,version_label,license_boundary,created_at,updated_at)
                VALUES(?,'english',?,'word',?,?,'[]','[]','[]','[]','[]',?,'v1','local',?,?)""",
                (word, word, word, json.dumps([meaning]), "document_page:" + word + ":page:1", NOW.isoformat(), NOW.isoformat()))
            c.execute("""INSERT INTO kg_language_focus_candidate(candidate_id,resource_id,language,term,
                normalized,focus_kind,proposed_json,status,created_at,updated_at)
                VALUES(?,?,'english',?,?,'word',?,'confirmed',?,?)""",
                (word, word, word, word, json.dumps({"senses": [meaning]}), NOW.isoformat(), NOW.isoformat()))
        c.commit()
    service = PetVocabularyService(db)
    service.sync_confirmed_textbook_lexemes(CHILD)
    return db, service, sid


def rows(db, table):
    with db.connect() as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def submit(service, sid, prompt, answer="unrelated", action="bound-answer", **kwargs):
    return service.submit_prompt(sid, prompt_session_id=prompt["prompt_session_id"],
        card_version=prompt["card_version"], answer=answer, client_action_id=action, at=NOW, **kwargs)


def cancel(service, sid, prompt):
    return service.cancel_prompt(sid, prompt_session_id=prompt["prompt_session_id"], card_version=prompt["card_version"])


def test_prompt_binding_and_original_receipt_survive_restart_and_later_card(flow):
    db, service, sid = flow
    original = service.present_next(sid, NOW)
    assert original["session_id"] == sid and original["child_id"] == CHILD
    assert original["prompt_session_id"] and original["card_version"]
    restarted = PetVocabularyService(Database(db.path))
    assert restarted.active_prompt(sid) == original
    assert restarted.read_prompt(sid, prompt_session_id=original["prompt_session_id"])["prompt"] == original
    first = submit(restarted, sid, original)
    assert first["status"] == "answered"
    later = service.present_next(sid, NOW)
    assert later["card_id"] != original["card_id"]
    assert submit(restarted, sid, original) == first
    assert restarted.read_prompt(sid, prompt_session_id=original["prompt_session_id"]) == first
    assert service.active_prompt(sid) == later
    assert len(rows(db, "pet_vocabulary_attempt")) == 1
    with pytest.raises(PetVocabularyError):
        submit(service, sid, original, answer="changed answer")
    with pytest.raises(PetVocabularyError):
        submit(service, sid, original, action="another-key")
    with pytest.raises(PetVocabularyError):
        submit(service, sid, later)


def test_late_answer_or_cancel_never_targets_replacement(flow):
    db, service, sid = flow
    original = service.present_next(sid, NOW)
    assert cancel(service, sid, original)["status"] == "cancelled"
    later = service.present_next(sid, NOW)
    assert later["prompt_session_id"] != original["prompt_session_id"]
    assert submit(service, sid, original)["status"] == "cancelled"
    assert cancel(service, sid, original)["status"] == "cancelled"
    assert service.active_prompt(sid) == later
    assert rows(db, "pet_vocabulary_attempt") == []


@pytest.mark.parametrize("mutation", ["card_content", "card_progress", "suspended", "lexeme", "withdrawn"])
def test_changed_card_or_source_invalidates_waiting_prompt(flow, mutation):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    card = next(row for row in rows(db, "pet_vocabulary_card") if row["card_id"] == prompt["card_id"])
    if mutation == "card_content":
        change(db, "UPDATE pet_vocabulary_card SET senses_json='[\"changed\"]' WHERE card_id=?", (card["card_id"],))
    elif mutation == "card_progress":
        change(db, "UPDATE pet_vocabulary_card SET total_attempts=total_attempts+1 WHERE card_id=?", (card["card_id"],))
    elif mutation == "suspended":
        change(db, "UPDATE pet_vocabulary_card SET status='suspended' WHERE card_id=?", (card["card_id"],))
    elif mutation == "lexeme":
        change(db, "UPDATE kg_language_lexeme SET version_label='v2' WHERE lexeme_id=?", (card["lexeme_id"],))
    else:
        change(db, "UPDATE kg_language_focus_candidate SET status='withdrawn' WHERE candidate_id=?", (card["lexeme_id"],))
    assert submit(service, sid, prompt)["status"] == "invalidated"
    assert service.active_prompt(sid) is None
    assert rows(db, "pet_vocabulary_attempt") == []


@pytest.mark.parametrize("mutation", ["completed", "evolving", "archived", "switched_owner"])
def test_session_child_activity_is_checked_before_replay_and_submit(flow, mutation):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    if mutation == "completed":
        change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
    elif mutation == "evolving":
        change(db, "UPDATE daily_session SET state='evolving' WHERE session_id=?", (sid,))
    elif mutation == "archived":
        change(db, "UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,))
    else:
        change(db, "UPDATE daily_session SET child_id=? WHERE session_id=?", (OTHER, sid))
    with pytest.raises(PetVocabularyError):
        submit(service, sid, prompt)
    assert rows(db, "pet_vocabulary_attempt") == []


def test_same_child_different_session_cannot_read_or_replay_original(flow):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    submit(service, sid, prompt)
    change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
    other_sid = StudyRepository(db).start_session(CHILD, "another-session")["session_id"]
    for invoke in (lambda: service.read_prompt(other_sid, prompt_session_id=prompt["prompt_session_id"]),
                   lambda: submit(service, other_sid, prompt), lambda: cancel(service, other_sid, prompt),
                   lambda: service.submit_active(other_sid, "unrelated", "bound-answer", NOW)):
        with pytest.raises(PetVocabularyError):
            invoke()
    assert len(rows(db, "pet_vocabulary_attempt")) == 1


def test_wrong_version_and_empty_answer_do_not_change_prompt(flow):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    with pytest.raises(PetVocabularyError):
        submit(service, sid, {**prompt, "card_version": "forged"})
    with pytest.raises(PetVocabularyError):
        cancel(service, sid, {**prompt, "card_version": "forged"})
    with pytest.raises(PetVocabularyError):
        submit(service, sid, prompt, answer=" ")
    assert service.active_prompt(sid) == prompt
    assert rows(db, "pet_vocabulary_attempt") == []


def test_failed_receipt_rolls_back_scoring_completion_and_schedule(flow, monkeypatch):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    cards = rows(db, "pet_vocabulary_card")
    with monkeypatch.context() as patch:
        def crash(*args):
            raise RuntimeError("injected before receipt")
        patch.setattr(service, "_result_from_rows", crash)
        with pytest.raises(RuntimeError, match="before receipt"):
            submit(service, sid, prompt)
    assert rows(db, "pet_vocabulary_card") == cards
    assert rows(db, "pet_vocabulary_attempt") == []
    assert service.active_prompt(sid) == prompt
    assert submit(PetVocabularyService(Database(db.path)), sid, prompt)["status"] == "answered"


def test_concurrent_same_answer_and_cancel_submit_race_are_atomic(flow):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(PetVocabularyService(Database(db.path)), sid, prompt), range(2)))
    assert results[0] == results[1]
    assert len(rows(db, "pet_vocabulary_attempt")) == 1
    next_prompt = service.present_next(sid, NOW)
    def do_cancel():
        try:
            return cancel(PetVocabularyService(Database(db.path)), sid, next_prompt)
        except PetVocabularyError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(do_cancel)
        answered = pool.submit(submit, PetVocabularyService(Database(db.path)), sid, next_prompt,
                               action="racing-answer")
        cancelled.result()
        result = answered.result()
    stored = service.read_prompt(sid, prompt_session_id=next_prompt["prompt_session_id"])
    assert stored["status"] in {"answered", "cancelled"}
    assert result["status"] == stored["status"]
    assert len(rows(db, "pet_vocabulary_attempt")) == (2 if stored["status"] == "answered" else 1)


def test_scoring_holds_write_lock_against_cross_connection_cancel(flow, monkeypatch):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    original = service._is_correct
    checks = []
    def locked(card, mode, answer):
        c = sqlite3.connect(db.path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                c.execute("UPDATE pet_vocabulary_prompt_session SET status='cancelled'")
        finally:
            c.close()
        checks.append(True)
        return original(card, mode, answer)
    monkeypatch.setattr(service, "_is_correct", locked)
    assert submit(service, sid, prompt)["status"] == "answered"
    assert checks == [True]


def test_legacy_signatures_reject_payload_reuse_without_completing_next_card(flow):
    db, service, sid = flow
    first = service.present_next(sid, NOW)
    result = service.submit_active(sid, "unrelated", "legacy-active", NOW)
    later = service.present_next(sid, NOW)
    assert service.submit_active(sid, "unrelated", "legacy-active", NOW) == result
    assert service.active_prompt(sid) == later
    with pytest.raises(PetVocabularyError):
        service.submit_active(sid, "different", "legacy-active", NOW)
    with pytest.raises(PetVocabularyError):
        service.submit(CHILD, first["card_id"], "different", "legacy-active", NOW, session_id=sid)
    assert len(rows(db, "pet_vocabulary_attempt")) == 1


def test_lost_response_restores_original_receipt_without_touching_new_prompt(flow, monkeypatch):
    db, service, sid = flow
    original = service.present_next(sid, NOW)
    real_submit = service.submit_prompt
    def lose_response(*args, **kwargs):
        real_submit(*args, **kwargs)
        raise RuntimeError("response lost after commit")
    with monkeypatch.context() as patch:
        patch.setattr(service, "submit_prompt", lose_response)
        with pytest.raises(RuntimeError, match="after commit"):
            submit(service, sid, original)
    saved_attempt = rows(db, "pet_vocabulary_attempt")[0]["attempt_id"]
    newer = service.present_next(sid, NOW)
    restarted = PetVocabularyService(Database(db.path))
    result = submit(restarted, sid, original)
    assert result["attempt"]["attempt_id"] == saved_attempt
    assert restarted.active_prompt(sid) == newer
    assert len(rows(db, "pet_vocabulary_attempt")) == 1


def test_same_card_new_review_mode_cannot_be_changed_by_original_receipt(flow):
    db, service, sid = flow
    original = service.present_next(sid, NOW)
    change(db, "UPDATE pet_vocabulary_card SET status='suspended' WHERE card_id!=?", (original["card_id"],))
    first = submit(service, sid, original)
    later = service.present_next(sid, NOW + timedelta(minutes=10))
    assert later["card_id"] == original["card_id"]
    assert later["prompt_session_id"] != original["prompt_session_id"]
    assert later["card_version"] != original["card_version"]
    assert later["review_mode"] != original["review_mode"]
    assert submit(service, sid, original) == first
    assert service.active_prompt(sid) == later
    with pytest.raises(PetVocabularyError):
        submit(service, sid, later)
    assert len(rows(db, "pet_vocabulary_attempt")) == 1


def test_legacy_prompt_migration_does_not_forge_historical_version(flow):
    db, service, sid = flow
    card = service.next_card(CHILD, NOW)
    change(db, "DROP TABLE pet_vocabulary_prompt_session")
    change(db, """CREATE TABLE pet_vocabulary_prompt_session(
        prompt_session_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,child_id TEXT NOT NULL,
        card_id TEXT NOT NULL,status TEXT NOT NULL,presented_at TEXT NOT NULL,completed_at TEXT)""")
    change(db, "INSERT INTO pet_vocabulary_prompt_session VALUES('legacy',?,?,?,'active',?,NULL)",
           (sid, CHILD, card["card_id"], NOW.isoformat()))
    service.ensure_schema()
    migrated = rows(db, "pet_vocabulary_prompt_session")[0]
    assert migrated["card_version"] is None and migrated["card_snapshot_json"] is None
    result = service.read_prompt(sid, prompt_session_id="legacy")
    assert result["status"] == "invalidated" and result["reason"] == "legacy_prompt_unbound"
    assert result["prompt"] is None and result["attempt"] is None
    new = service.present_next(sid, NOW)
    assert new["prompt_session_id"] != "legacy" and new["card_version"]
    assert rows(db, "pet_vocabulary_attempt") == []


def test_current_card_edit_cannot_rewrite_original_scored_feedback(flow):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    first = submit(service, sid, prompt)
    change(db, "UPDATE pet_vocabulary_card SET senses_json='[\"new meaning\"]' WHERE card_id=?", (prompt["card_id"],))
    assert service.read_prompt(sid, prompt_session_id=prompt["prompt_session_id"]) == first
    assert submit(service, sid, prompt) == first


def test_nonempty_deck_without_due_or_qualified_source_is_not_waiting(flow):
    db, service, sid = flow
    change(db, "UPDATE kg_language_focus_candidate SET status='withdrawn'")
    assert service.present_next(sid, NOW) is None
    assert service.active_prompt(sid) is None
    assert rows(db, "pet_vocabulary_prompt_session") == []


def test_ended_or_switched_session_cannot_replay_successful_receipt(flow):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    submit(service, sid, prompt)
    change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
    with pytest.raises(PetVocabularyError):
        submit(service, sid, prompt)
    assert len(rows(db, "pet_vocabulary_attempt")) == 1


def test_independent_process_recovers_original_receipt_not_current_card(flow):
    db, service, sid = flow
    original = service.present_next(sid, NOW)
    receipt = submit(service, sid, original)
    later = service.present_next(sid, NOW)
    program = """
import json, sys
from pathlib import Path
from home_study_agent.database import Database
from home_study_agent.pet_vocabulary import PetVocabularyService
s = PetVocabularyService(Database(Path(sys.argv[1])))
result = s.submit_prompt(sys.argv[2], prompt_session_id=sys.argv[3], card_version=sys.argv[4],
                         answer='unrelated', client_action_id='bound-answer')
print(json.dumps({'receipt': result, 'active': s.active_prompt(sys.argv[2])}))
"""
    completed = subprocess.run([sys.executable, "-c", program, str(db.path), sid,
        original["prompt_session_id"], original["card_version"]], check=True, capture_output=True,
        text=True, timeout=20, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    recovered = json.loads(completed.stdout)
    assert recovered["receipt"] == receipt and recovered["active"] == later
    assert len(rows(db, "pet_vocabulary_attempt")) == 1


def test_unbound_direct_score_invalidates_prompt_instead_of_adopting_receipt(flow):
    db, service, sid = flow
    prompt = service.present_next(sid, NOW)
    service.submit(CHILD, prompt["card_id"], "unrelated", "direct-legacy-score", NOW, session_id=sid)
    with pytest.raises(PetVocabularyError, match="receipt_conflict"):
        submit(service, sid, prompt, action="direct-legacy-score")
    result = submit(service, sid, prompt, action="different-bound-key")
    assert result["status"] == "invalidated" and result["reason"] == "card_version_changed"
    assert len(rows(db, "pet_vocabulary_attempt")) == 1
