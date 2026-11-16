import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import LocalSupplyError, build_candidate, item_snapshot, score_local_item, validate_item
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from home_study_agent.variant_supply import VariantSupplyService
from test_variant_supply import _prepared, FakeVariantGateway
from test_specialized_supply import prepared, FakeSupplyGateway

CHILD = "child-junior-3"
SEED = "diag-j3-discriminant-1"


def _material_seed(db, monkeypatch, field, value):
    from home_study_agent import learning_state
    original = next(row for row in learning_state.ITEMS if row[0] == SEED)
    monkeypatch.setattr(learning_state, "ITEMS", tuple(row for row in learning_state.ITEMS if row[0] != SEED))
    monkeypatch.setattr(learning_state, "EXTENDED_ITEMS", (*learning_state.EXTENDED_ITEMS,
        (*original, value if field == "stimulus_text" else None,
         value if field == "stimulus_asset" else None, "objective")))
    with db.connect() as c:
        c.execute(f"UPDATE diagnostic_item SET {field}=? WHERE diagnostic_item_id=?", (value, SEED))
        c.commit()


def _material_supply(tmp_path, variant, *, monkeypatch=None, field=None):
    if variant:
        db, _, mistake_id = _prepared(tmp_path)
        service = VariantSupplyService(db, ForbiddenGateway())
    else:
        db = prepared(tmp_path)
        service = SpecializedDiagnosticSupplyService(db, ForbiddenGateway())
    if field:
        _material_seed(db, monkeypatch, field, "Synthetic material" if field == "stimulus_text" else "asset:synthetic-page")
    job = service.enqueue_local(mistake_id if variant else SEED, CHILD, "material-job")
    return db, service, job


@pytest.mark.parametrize("field", ["stimulus_text", "stimulus_asset"])
@pytest.mark.parametrize("has_material", [False, True])
def test_shipped_source_material_must_match_authority(tmp_path, monkeypatch, field, has_material):
    db = prepared(tmp_path)
    if has_material:
        _material_seed(db, monkeypatch, field, "Synthetic source material")
    with db.connect() as c:
        verified = validate_item(c, CHILD, SEED)
        assert item_snapshot(verified)[field] == ("Synthetic source material" if has_material else "")
        for value in ("forged", None) if has_material else ("forged", " "):
            c.execute(f"UPDATE diagnostic_item SET {field}=? WHERE diagnostic_item_id=?", (value, SEED))
            with pytest.raises(LocalSupplyError, match="SUPPLY_SEED_CHANGED"):
                validate_item(c, CHILD, SEED)


@pytest.mark.parametrize("field", ["stimulus_text", "stimulus_asset"])
@pytest.mark.parametrize("variant", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_promoted_candidate_cannot_gain_unapproved_material(tmp_path, field, variant, local):
    db, service, _ = _material_supply(tmp_path, variant)
    service.process_local_pending()
    candidate = service.list(CHILD, "needs_review")["items"][0]
    promoted = (service.qualify_local(candidate["candidate_id"], CHILD, "material-qualify") if local
        else service.review(candidate["candidate_id"], "approve", "Checked synthetic question", "material-review"))
    item_id = promoted["diagnostic_item_id"]
    with db.connect() as c:
        validate_item(c, CHILD, item_id)
        c.execute(f"UPDATE diagnostic_item SET {field}='forged' WHERE diagnostic_item_id=?", (item_id,))
        with pytest.raises(LocalSupplyError, match="SUPPLY_CONTENT_CHANGED"):
            validate_item(c, CHILD, item_id)


@pytest.mark.parametrize("variant", [False, True])
@pytest.mark.parametrize("field", [None, "stimulus_text", "stimulus_asset"])
def test_legacy_snapshot_only_accepts_proven_empty_seed_without_rewriting_history(tmp_path, monkeypatch, variant, field):
    db, service, job = _material_supply(tmp_path, variant, monkeypatch=monkeypatch, field=field)
    service.process_local_pending()
    promoted = approve(service)[0]
    table = "variant_generation_job" if variant else "specialized_supply_job"
    with db.connect() as c:
        validate_item(c, CHILD, promoted["diagnostic_item_id"])
        original = json.loads(c.execute(f"SELECT source_snapshot_json FROM {table} WHERE job_id=?", (job["job_id"],)).fetchone()[0])
        assert {"stimulus_text", "stimulus_asset"} <= original.keys()
        legacy = {key: value for key, value in original.items() if key not in ("stimulus_text", "stimulus_asset")}
        stored = json.dumps(legacy)
        c.execute(f"UPDATE {table} SET source_snapshot_json=? WHERE job_id=?", (stored, job["job_id"]))
        c.commit()
        before = c.total_changes
        c.execute("PRAGMA query_only=ON")
        if field:
            with pytest.raises(LocalSupplyError, match="SUPPLY_SOURCE_REVIEW_REQUIRED"):
                validate_item(c, CHILD, promoted["diagnostic_item_id"])
        else:
            validate_item(c, CHILD, promoted["diagnostic_item_id"])
        assert c.total_changes == before
    reopened = type(service)(Database(db.path), ForbiddenGateway())
    seed_arg = job["mistake_id"] if variant else SEED
    if field:
        with pytest.raises(LocalSupplyError, match="SUPPLY_SOURCE_REVIEW_REQUIRED"):
            reopened.enqueue_local(seed_arg, CHILD, "material-job")
    else:
        assert reopened.enqueue_local(seed_arg, CHILD, "material-job")["job_id"] == job["job_id"]
    with db.connect() as c:
        assert c.execute(f"SELECT source_snapshot_json FROM {table} WHERE job_id=?", (job["job_id"],)).fetchone()[0] == stored
        if field:
            # Deleting the current material cannot turn an incomplete historical snapshot into proof.
            c.execute(f"UPDATE diagnostic_item SET {field}=NULL WHERE diagnostic_item_id=?", (SEED,))
            with pytest.raises(LocalSupplyError, match="SUPPLY_SEED_CHANGED"):
                validate_item(c, CHILD, promoted["diagnostic_item_id"])


@pytest.mark.parametrize("variant", [False, True])
@pytest.mark.parametrize("field", [None, "stimulus_text", "stimulus_asset"])
def test_legacy_pending_job_uses_same_material_gate(tmp_path, monkeypatch, variant, field):
    db, service, job = _material_supply(tmp_path, variant, monkeypatch=monkeypatch, field=field)
    table = "variant_generation_job" if variant else "specialized_supply_job"
    with db.connect() as c:
        snapshot = json.loads(c.execute(f"SELECT source_snapshot_json FROM {table} WHERE job_id=?", (job["job_id"],)).fetchone()[0])
        stored = json.dumps({key: value for key, value in snapshot.items() if key not in ("stimulus_text", "stimulus_asset")})
        c.execute(f"UPDATE {table} SET source_snapshot_json=? WHERE job_id=?", (stored, job["job_id"]))
        c.commit()
    result = service.process_local_pending()["jobs"][0]
    assert result["status"] == ("blocked" if field else "completed")
    assert result["error_code"] == ("SUPPLY_SOURCE_REVIEW_REQUIRED" if field else None)
    if field:
        assert result["candidate_count"] == 0
    with db.connect() as c:
        assert c.execute(f"SELECT source_snapshot_json FROM {table} WHERE job_id=?", (job["job_id"],)).fetchone()[0] == stored


@pytest.mark.parametrize("change", ["partial", "forged", "missing", "invalid", "base_changed"])
def test_source_snapshot_incomplete_or_changed_never_silently_qualifies(tmp_path, change):
    db, service, job = _material_supply(tmp_path, False)
    service.process_local_pending()
    promoted = approve(service)[0]
    with db.connect() as c:
        snapshot = json.loads(c.execute("SELECT source_snapshot_json FROM specialized_supply_job WHERE job_id=?", (job["job_id"],)).fetchone()[0])
        if change == "partial":
            snapshot.pop("stimulus_asset")
        elif change == "forged":
            snapshot["stimulus_text"] = "forged"
        elif change == "base_changed":
            snapshot.pop("stimulus_text")
            snapshot.pop("stimulus_asset")
            snapshot["prompt"] = "forged"
        stored = None if change == "missing" else "{" if change == "invalid" else json.dumps(snapshot)
        c.execute("UPDATE specialized_supply_job SET source_snapshot_json=? WHERE job_id=?", (stored, job["job_id"]))
        expected = "SUPPLY_SOURCE_CHANGED" if change in ("forged", "base_changed") else "SUPPLY_SOURCE_REVIEW_REQUIRED"
        with pytest.raises(LocalSupplyError, match=expected):
            validate_item(c, CHILD, promoted["diagnostic_item_id"])


@pytest.mark.parametrize("answer,expected", [("C", 1), ("我选C", 1), ("c", 1), ("选择 C", 1),
    ("不是C", 0), ("C或者A", 0), ("3", 0), ("13", 0)])
def test_exact_choice_normalization(answer, expected):
    item = {"expected_answer_json": json.dumps({"local_rule": "local_exact_v1", "exact_answers": ["C"]}), "explanation": "核对选项"}
    assert score_local_item(item, answer)[0] == expected


@pytest.mark.parametrize("answer,expected", [("4", 1), ("答案是4", 1), ("14", 0), ("-4", 0),
    ("不是4", 0), ("4或者5", 0), ("C", 0)])
def test_exact_numeric_does_not_match_substring(answer, expected):
    item = {"expected_answer_json": json.dumps({"local_rule": "local_exact_v1", "exact_answers": ["4"]}), "explanation": "结果4"}
    assert score_local_item(item, answer)[0] == expected


def test_real_delivery_drains_to_qualified_olympiad_and_pet_without_mutating_seeds(tmp_path):
    from home_study_agent.local_supply_delivery import refresh_local_question_supply
    db = prepared(tmp_path)
    specialized, variants = SpecializedDiagnosticSupplyService(db, ForbiddenGateway()), VariantSupplyService(db, ForbiddenGateway())
    with db.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM diagnostic_item ORDER BY diagnostic_item_id")]
    reports = [refresh_local_question_supply(db, specialized, variants) for _ in range(2)]
    assert sum(row["qualified"] for row in reports) == 10
    with db.connect() as connection:
        counts = dict(connection.execute("SELECT track,count(*) FROM specialized_supply_candidate WHERE status='promoted' GROUP BY track"))
        assert counts == {"junior_exam": 3, "competition_math": 5, "pet": 2}
        assert [tuple(row) for row in connection.execute("SELECT * FROM diagnostic_item WHERE source_ref NOT LIKE 'local_verified_%' ORDER BY diagnostic_item_id")] == before
        assert connection.execute("SELECT count(*) FROM diagnostic_attempt").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM mastery_state").fetchone()[0] == 0
        for row in connection.execute("SELECT * FROM specialized_supply_candidate WHERE status='promoted'"):
            item = validate_item(connection, row["child_id"], row["diagnostic_item_id"])
            expected = json.loads(item["expected_answer_json"])["exact_answers"][0]
            assert score_local_item(item, expected)[0] == 1
            assert score_local_item(item, "不是" + expected)[0] == 0
    reopened = SpecializedDiagnosticSupplyService(Database(db.path), ForbiddenGateway())
    assert refresh_local_question_supply(db, reopened, variants)["qualified"] == 0
    grammar = [row for row in reopened.list("child-primary-4")["items"] if row["domain_id"] == "pet.writing.grammar"]
    assert grammar and all(row["status"] == "needs_review" for row in grammar)
    for row in grammar:
        with pytest.raises(LocalSupplyError, match="OPEN_RESPONSE_SEED_REQUIRES_PARENT_REVIEW"):
            reopened.qualify_local(row["candidate_id"], "child-primary-4", "must-not-approve")


@pytest.mark.parametrize("node", ["node-p4-math-arithmetic", "node-p4-math-fractions-decimals"])
def test_primary_template_computation_is_exact_but_missing_seed_cannot_qualify(tmp_path, node):
    from fractions import Fraction
    from home_study_agent.local_supply_templates import closed_template
    db = prepared(tmp_path)
    for ordinal in range(36):
        for stage in ("parallel", "near_transfer", "discrimination"):
            result = closed_template({"knowledge_node_id": node}, stage, ordinal)
            proof = result["proof"]
            if node.endswith("arithmetic"):
                value = proof["a"] + proof["b"] * proof["c"] if stage == "discrimination" else (proof["a"] + proof["b"]) * proof["c"]
                assert result["answer"] == str(value)
            elif stage != "discrimination":
                assert Fraction(result["answer"]) == Fraction(proof["left"]) + Fraction(proof["right"])
            else:
                assert result["answer"] == "A" and Fraction(proof["left"]) > Fraction(proof["right"])
    with db.connect() as connection:
        with pytest.raises(LocalSupplyError, match="SUPPLY_SOURCE_UNAVAILABLE"):
            validate_item(connection, "child-primary-4", "not-a-real-seed:" + node)


@pytest.mark.parametrize("answer,expected", [("1/2", 1), ("2/4", 1), ("0.5", 1), ("0.50", 1),
    ("0.51", 0), ("1/0", 0), ("不是0.5", 0), ("1/2或1/3", 0)])
def test_fraction_decimal_exact_scoring(answer, expected):
    item = {"expected_answer_json": json.dumps({"local_rule": "local_exact_v1", "exact_answers": ["1/2"], "answer_format": "rational"}), "explanation": "通分核对"}
    assert score_local_item(item, answer)[0] == expected


class ForbiddenGateway:
    configured = True

    async def complete(self, request):
        raise AssertionError("local jobs must never call a model")


def approve(service, child_id=CHILD):
    return [service.review(row["candidate_id"], "approve", "逐题核对题意、答案与适用范围", "approve:" + row["candidate_id"])
            for row in service.list(child_id, "needs_review")["items"]]


def test_local_specialized_persists_candidates_never_auto_approves_or_calls_cloud(tmp_path):
    db = prepared(tmp_path)
    supply = SpecializedDiagnosticSupplyService(db, ForbiddenGateway())
    job = supply.enqueue_local(SEED, CHILD, "local-discriminant")
    assert job["generation_mode"] == "local"
    assert asyncio.run(supply.process_next()) is None
    assert supply.process_local_pending()["jobs"][0]["status"] == "completed"
    candidates = supply.list(CHILD)["items"]
    assert len(candidates) == 3 and all(row["status"] == "needs_review" for row in candidates)
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM diagnostic_item WHERE source_ref LIKE 'parent_approved_%'").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM mastery_state").fetchone()[0] == 0
    reopened = SpecializedDiagnosticSupplyService(Database(db.path), ForbiddenGateway())
    assert reopened.list(CHILD)["items"] == candidates
    assert reopened.enqueue_local(SEED, CHILD, "local-discriminant")["job_id"] == job["job_id"]
    with pytest.raises(LocalSupplyError, match="CONFLICT"):
        reopened.enqueue_local(SEED, CHILD, "local-discriminant", count=4)
    promoted = approve(reopened)
    with db.connect() as connection:
        for candidate in promoted:
            item = validate_item(connection, CHILD, candidate["diagnostic_item_id"])
            answer = json.loads(item["expected_answer_json"])["exact_answers"][0]
            assert score_local_item(item, answer)[0] == 1
            assert score_local_item(item, "不是" + answer)[0] == 0
            assert score_local_item(item, answer + "或者999")[0] == 0
            with pytest.raises(LocalSupplyError):
                validate_item(connection, "child-primary-4", item["diagnostic_item_id"])


def test_local_generation_atomic_failure_retry_and_concurrent_enqueue(tmp_path, monkeypatch):
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db)
    service.ensure_schema()
    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(lambda _: service.enqueue_local(SEED, CHILD, "same-local-action"), range(2)))
    assert jobs[0]["job_id"] == jobs[1]["job_id"]
    original = service._store

    def crash(*args):
        original(*args)
        raise RuntimeError("private source or unexpected storage detail")

    monkeypatch.setattr(service, "_store", crash)
    failed = service.process_local_pending()["jobs"][0]
    assert failed["status"] == "failed" and failed["error_code"] == "LOCAL_SUPPLY_FAILED"
    assert failed["candidate_count"] == 0
    monkeypatch.setattr(service, "_store", original)
    assert service.retry_job(CHILD, failed["job_id"])["status"] == "pending"
    assert service.process_local_pending()["jobs"][0]["candidate_count"] == 3
    assert service.process_local_pending()["processed"] == 0
    with pytest.raises(LocalSupplyError):
        service.retry_job("child-primary-4", failed["job_id"])


def test_withdrawn_or_modified_seed_blocks_generation_and_review(tmp_path):
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db)
    job = service.enqueue_local(SEED, CHILD, "withdraw-seed")
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (SEED,))
        connection.commit()
    assert service.process_local_pending()["jobs"][0]["status"] == "blocked"
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id=?", (SEED,))
        connection.commit()
    service.retry_job(CHILD, job["job_id"])
    service.process_local_pending()
    candidate = service.list(CHILD)["items"][0]
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET prompt='伪造的原题' WHERE diagnostic_item_id=?", (SEED,))
        connection.commit()
    with pytest.raises(LocalSupplyError, match="CHANGED"):
        service.review(candidate["candidate_id"], "approve", "审核", "cannot-approve-changed-source")


def test_tampered_answer_cannot_be_promoted_by_validation_passed_flag(tmp_path):
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db)
    service.enqueue_local(SEED, CHILD, "proof-job")
    service.process_local_pending()
    row = service.list(CHILD)["items"][0]
    with db.connect() as connection:
        connection.execute("UPDATE specialized_supply_candidate SET expected_answer_json=? WHERE candidate_id=?", ('{"keywords":["999"],"exact_answers":["999"],"local_rule":"local_exact_v1"}', row["candidate_id"]))
        connection.commit()
    with pytest.raises(LocalSupplyError, match="PROOF_MISMATCH"):
        service.review(row["candidate_id"], "approve", "通过", "tampered-approval")


def test_local_variant_is_consumed_by_existing_mistake_independent_check(tmp_path):
    db, mistakes, mistake_id = _prepared(tmp_path)
    service = VariantSupplyService(db, ForbiddenGateway())
    service.enqueue_local(mistake_id, CHILD, "local-variant")
    result = service.process_local_pending()["jobs"][0]
    assert result["status"] == "completed" and result["candidate_count"] == 4
    promoted = approve(service)
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id='diag-j3-discriminant-2'")
        connection.commit()
    mistakes.add_remediation(mistake_id, CHILD, "concept_explanation", "completed", "已讲解，进入实际作答", "local-remedy")
    selected = mistakes.next_activity(mistake_id, CHILD)
    item_id = selected["item"]["diagnostic_item_id"]
    assert item_id in {row["diagnostic_item_id"] for row in promoted}
    with db.connect() as connection:
        item = validate_item(connection, CHILD, item_id)
        answer = json.loads(item["expected_answer_json"])["exact_answers"][0]
    mistakes.submit_attempt(mistake_id, CHILD, "near_transfer", item_id, answer, 0, "local-real-attempt")
    with db.connect() as connection:
        attempt = connection.execute("SELECT diagnostic_item_id,correctness FROM mistake_attempt WHERE client_action_id='local-real-attempt'").fetchone()
        assert tuple(attempt) == (item_id, 1.0)
    assert mistakes.get(mistake_id, CHILD)["status"] != "mastered"


def test_promoted_local_question_binds_existing_review_and_survives_restart(tmp_path, monkeypatch):
    from home_study_agent import learning_state
    seed = "diag-test-discriminant-closed"
    monkeypatch.setattr(learning_state, "ITEMS", (*learning_state.ITEMS,
        (seed, "node-j3-math-discriminant", "For x^2-4x+4=0, give only the integer discriminant.", "text",
         {"local_rule": "local_exact_v1", "exact_answers": ["0"]}, "16 - 16 = 0.", 1)))
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db, ForbiddenGateway())
    repository = StudyRepository(db)
    sid = repository.start_session(CHILD, "supply-learning-session")["session_id"]
    with db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    learning = LearningStateService(db)
    source = learning.submit_diagnostic(sid, seed, "不知道", "supply-wrong-answer")
    assert source["score_status"] == "verified_local"
    service.enqueue_local(seed, CHILD, "local-review-supply")
    assert service.process_local_pending()["jobs"][0]["status"] == "completed"
    promoted = approve(service)
    bound = service.bind_due_reviews(CHILD)
    assert len(bound["bindings"]) == 1
    receipt = bound["bindings"][0]
    assert receipt["diagnostic_item_id"] in {row["diagnostic_item_id"] for row in promoted}
    assert SpecializedDiagnosticSupplyService(Database(db.path)).bind_due_reviews(CHILD)["bindings"] == []
    next_question = LearningStateService(Database(db.path)).next_diagnostic(CHILD, "school")
    assert next_question["diagnostic_item_id"] == receipt["diagnostic_item_id"]
    with db.connect() as connection:
        item = validate_item(connection, CHILD, receipt["diagnostic_item_id"])
        answer = json.loads(item["expected_answer_json"])["exact_answers"][0]
    actual = learning.submit_diagnostic(sid, receipt["diagnostic_item_id"], answer, "real-supplied-retest", receipt["review_task_id"])
    assert actual["correctness"] == 1
    assert actual["score_status"] == "verified_local"
    assert learning.submit_diagnostic(sid, receipt["diagnostic_item_id"], answer, "real-supplied-retest", receipt["review_task_id"]) == actual


def test_unsupported_domains_have_durable_blocker_not_fake_complete(tmp_path):
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db)
    queued = service.enqueue_local_gaps("child-primary-4", "competition_math", "local-gap-run", limit=10)
    assert queued["jobs"]
    processed = service.process_local_pending(10)
    assert processed["cloud_transmission_allowed"] is False
    assert any(row["status"] == "blocked" for row in processed["jobs"])
    assert any(row["candidate_count"] > 0 for row in processed["jobs"])
    assert all(row["error_code"] for row in processed["jobs"] if row["status"] == "blocked")
    assert service.process_local_pending()["processed"] == 0


def test_review_action_conflicts_and_unknown_model_restart_requires_explicit_retry(tmp_path):
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db, FakeSupplyGateway())
    job = service.enqueue_local(SEED, CHILD, "review-actions")
    service.process_local_pending()
    candidate = service.list(CHILD)["items"][0]
    result = service.review(candidate["candidate_id"], "approve", "核对通过", "same-review")
    assert service.review(candidate["candidate_id"], "approve", "核对通过", "same-review") == result
    with pytest.raises(ValueError):
        service.review(candidate["candidate_id"], "reject", "核对通过", "same-review")
    with db.connect() as connection:
        connection.execute("UPDATE specialized_supply_job SET generation_mode='model',status='processing',attempt_count=1 WHERE job_id=?", (job["job_id"],))
        connection.commit()
    service.recover()
    current = service.list(CHILD)["jobs"][0]
    assert current["status"] == "blocked" and current["error_code"] == "GENERATION_OUTCOME_UNKNOWN"
    assert asyncio.run(service.process_next()) is None
    with pytest.raises(LocalSupplyError):
        service.retry_job(CHILD, job["job_id"])
    assert service.retry_job(CHILD, job["job_id"], allow_model_retry=True)["status"] == "pending"


@pytest.mark.parametrize("variant", [False, True])
def test_explicit_deterministic_qualification_has_true_source_and_owned_scope(tmp_path, variant):
    if variant:
        db, _, mistake_id = _prepared(tmp_path)
        service = VariantSupplyService(db)
        service.enqueue_local(mistake_id, CHILD, "qualify-source")
    else:
        db = prepared(tmp_path)
        service = SpecializedDiagnosticSupplyService(db)
        service.enqueue_local(SEED, CHILD, "qualify-source")
    service.process_local_pending()
    row = service.list(CHILD, "needs_review")["items"][0]
    qualified = service.qualify_local(row["candidate_id"], CHILD, "rule-check")
    assert qualified["status"] == "promoted" and qualified["validation"]["parent_approved"] is False
    assert service.qualify_local(row["candidate_id"], CHILD, "rule-check") == qualified
    with db.connect() as connection:
        item = validate_item(connection, CHILD, qualified["diagnostic_item_id"])
        assert item["source_ref"].startswith("local_verified_")
        assert not item["source_ref"].startswith("parent_approved_")
        with pytest.raises(LocalSupplyError):
            validate_item(connection, "child-primary-4", item["diagnostic_item_id"])
        if variant:
            with pytest.raises(LocalSupplyError, match="STAGE_OR_MISTAKE"):
                validate_item(connection, CHILD, item["diagnostic_item_id"], stage="far_transfer")
    with pytest.raises(LocalSupplyError):
        service.qualify_local(row["candidate_id"], "child-primary-4", "foreign-qualification")


def test_model_candidate_cannot_be_qualified_even_when_local_validation_passed(tmp_path):
    db, _, mistake_id = _prepared(tmp_path)
    service = VariantSupplyService(db, FakeVariantGateway())
    service.enqueue(mistake_id, CHILD, "model-source")
    asyncio.run(service.process_next())
    row = service.list(CHILD, "needs_review")["items"][0]
    assert row["validation"]["passed"]
    with pytest.raises(LocalSupplyError, match="MODEL_CANDIDATE"):
        service.qualify_local(row["candidate_id"], CHILD, "not-local")
    assert service.list(CHILD)["items"][0]["status"] == "needs_review"


def test_gaps_do_not_count_another_childs_promoted_content_or_withdrawn_seed(tmp_path):
    db = prepared(tmp_path)
    service = SpecializedDiagnosticSupplyService(db)
    service.enqueue_local(SEED, CHILD, "own-depth")
    service.process_local_pending()
    for row in service.list(CHILD)["items"]:
        service.qualify_local(row["candidate_id"], CHILD, "qualify:" + row["candidate_id"])
    with db.connect() as connection:
        connection.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            SELECT 'same-grade-peer','另一孩子',grade,curriculum_region,status,created_at,updated_at FROM child_profile WHERE child_id=?""", (CHILD,))
        connection.commit()
    own = next(row for row in service.gaps(CHILD, "junior_exam")["domains"] if row["domain_id"] == "junior_exam.数学.方程与不等式")
    peer = next(row for row in service.gaps("same-grade-peer", "junior_exam")["domains"] if row["domain_id"] == own["domain_id"])
    assert own["active_count"] == peer["active_count"] + 3
    with db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (SEED,))
        connection.commit()
    withdrawn = next(row for row in service.gaps(CHILD, "junior_exam")["domains"] if row["domain_id"] == own["domain_id"])
    assert withdrawn["active_count"] == own["active_count"] - 4
    assert withdrawn["ready"] is False
