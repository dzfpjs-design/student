"""Offline, reproducible question supply using the existing candidate/job tables.

Model drafts require parent review; bounded local rules have explicit qualification.
Consumers must call validate_item before selection/submission and use
score_local_item for the local_exact_v1 rubric instead of keyword matching.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from fractions import Fraction
from uuid import uuid4
from .local_supply_templates import closed_template
from .learning_grade_scope import allows_learning_grade


class LocalSupplyError(ValueError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def item_snapshot(item) -> dict:
    return {**{key: item[key] for key in ("diagnostic_item_id", "knowledge_node_id", "prompt", "answer_type",
        "expected_answer_json", "explanation", "difficulty", "source_ref")}, **_stimuli(item)}


def _stimuli(item) -> dict:
    # NULL and empty text both mean absent; whitespace and asset identifiers are exact.
    return {key: item[key] if key in item.keys() and item[key] is not None else ""
            for key in ("stimulus_text", "stimulus_asset")}


def _validate_source_snapshot(snapshot, seed) -> None:
    """Seed must already pass recursive authority validation. Never migrate stored proof."""
    if not isinstance(snapshot, dict):
        raise LocalSupplyError("SUPPLY_SOURCE_REVIEW_REQUIRED")
    material_keys = {"stimulus_text", "stimulus_asset"}
    present = material_keys.intersection(snapshot)
    if present != material_keys:
        # Only the complete legacy shape and an authoritatively empty seed are compatible.
        if present or any(value != "" for value in _stimuli(seed).values()):
            raise LocalSupplyError("SUPPLY_SOURCE_REVIEW_REQUIRED")
    normalized = {**snapshot, **_stimuli(snapshot)}
    if normalized != item_snapshot(seed):
        raise LocalSupplyError("SUPPLY_SOURCE_CHANGED")


def _read_source_snapshot(value):
    try:
        snapshot = json.loads(value)
    except (TypeError, ValueError) as error:
        raise LocalSupplyError("SUPPLY_SOURCE_REVIEW_REQUIRED") from error
    if not isinstance(snapshot, dict):
        raise LocalSupplyError("SUPPLY_SOURCE_REVIEW_REQUIRED")
    return snapshot


def ensure_job_columns(connection, table: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, declaration in (("generation_mode", "TEXT NOT NULL DEFAULT 'model'"),
                              ("source_snapshot_json", "TEXT"), ("retry_count", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def owns_model_attempt(connection, table, claimed) -> bool:
    current = connection.execute(f"SELECT status,attempt_count,retry_count FROM {table} WHERE job_id=?", (claimed["job_id"],)).fetchone()
    return bool(current and current["status"] == "processing" and current["attempt_count"] == claimed["attempt_count"] + 1
                and current["retry_count"] == claimed["retry_count"])


def validate_item(connection, child_id: str, item_id: str, *, stage: str | None = None, mistake_id: str | None = None, _seen=None) -> dict:
    """Return the active owned/qualified item, or raise LocalSupplyError.

    Delayed review stages are timing constraints, not candidate transfer forms.
    The mistake gate still owns due times, exposure and independence admission.
    """
    seen = set(_seen or ())
    if item_id in seen or len(seen) >= 12:
        raise LocalSupplyError("SUPPLY_SOURCE_CYCLE")
    seen.add(item_id)
    item = connection.execute("""SELECT di.*,kn.grade_band,kn.track FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active'""", (item_id,)).fetchone()
    child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if not item or not child or not allows_learning_grade(child[0], item['grade_band'], item['track']):
        raise LocalSupplyError("SUPPLY_SOURCE_UNAVAILABLE")
    source = item["source_ref"] or ""
    if not (source.startswith('hsc_original') or
            (source.startswith('parent_approved_specialized:') and item['answer_type'] == 'open_response'
             and item['assessment_mode'] == 'open_response_rubric')):
        from .rubric_calibration_transaction import unsupported_dependency
        unsupported_dependency()
    if source.startswith('parent_approved_first:'):
        from .curriculum_first_source import validate_published
        validate_published(connection, child_id, item)
        if stage not in {None, 'parallel', 'delayed_48h', 'delayed_7d'} or mistake_id is not None:
            raise LocalSupplyError('SUPPLY_STAGE_OR_MISTAKE_MISMATCH')
    elif source.startswith(("parent_approved_", "local_verified_")):
        local = source.startswith("local_verified_")
        variant = source.startswith(("parent_approved_variant:", "local_verified_variant:"))
        table = "variant_candidate" if variant else "specialized_supply_candidate"
        prefix = ("local_verified_" if local else "parent_approved_") + ("variant:" if variant else "specialized:")
        if not source.startswith(prefix) or not connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            raise LocalSupplyError("SUPPLY_SOURCE_UNAVAILABLE")
        candidate = connection.execute(f"SELECT * FROM {table} WHERE candidate_id=? AND child_id=? AND diagnostic_item_id=? AND status='promoted'",
            (source[len(prefix):], child_id, item_id)).fetchone()
        if not candidate or not candidate["review_action_id"] or not candidate["reviewed_at"]:
            raise LocalSupplyError("SUPPLY_REVIEW_UNAVAILABLE")
        transfer_stage = None if stage in {'delayed_48h', 'delayed_7d'} else stage
        if variant and ((transfer_stage is not None and candidate["stage"] != transfer_stage) or (mistake_id is not None and candidate["mistake_id"] != mistake_id)):
            raise LocalSupplyError("SUPPLY_STAGE_OR_MISTAKE_MISMATCH")
        for key in ("prompt", "expected_answer_json", "explanation", "difficulty"):
            if item[key] != candidate[key]:
                raise LocalSupplyError("SUPPLY_CONTENT_CHANGED")
        material = _stimuli(candidate)
        if item['answer_type'] == 'open_response' or item['assessment_mode'] == 'open_response_rubric':
            from .open_response_source import candidate_source, MATERIAL_SCHEMA, VISUAL_SCHEMA
            opened = candidate_source(connection,candidate) if not variant and not local else None
            if (opened is None
                    or item['answer_type'] != 'open_response' or item['assessment_mode'] != 'open_response_rubric'):
                raise LocalSupplyError('SUPPLY_OPEN_RESPONSE_CONTRACT_REQUIRED')
            if opened['schema'] in {MATERIAL_SCHEMA, VISUAL_SCHEMA}:
                from .open_response_materials import resolve_source_materials
                resolved = resolve_source_materials(connection, child_id=child_id, source=opened,
                                                    candidate_id=candidate['candidate_id'])
                material = _stimuli({'stimulus_text':resolved['stimulus_text'], 'stimulus_asset':None})
        if not variant and not local:
            from .pet_oral_source import candidate_source as oral_source, materials as oral_materials
            oral = oral_source(connection, candidate)
            if oral is not None:
                if item['answer_type'] != oral['answer_type'] or item['assessment_mode'] != 'pet_reference_rubric':
                    raise LocalSupplyError('SUPPLY_CONTENT_CHANGED')
                material = _stimuli(oral_materials(connection, child_id, oral))
            from .pet_reviewed_source import candidate_listening_source
            original = candidate_listening_source(connection, candidate)
            if original is not None:
                material = _stimuli({"stimulus_text": original["stimulus_text"], "stimulus_asset": None})
                if item["answer_type"] != "listening_text" or item["assessment_mode"] != "objective":
                    raise LocalSupplyError("SUPPLY_CONTENT_CHANGED")
        if _stimuli(item) != material:
            raise LocalSupplyError("SUPPLY_CONTENT_CHANGED")
        validate_candidate_source(connection, table, candidate, seen=seen)
        if local and json.loads(candidate["validation_json"]).get("qualification") != "deterministic_answer_verified_v1":
            raise LocalSupplyError("LOCAL_QUALIFICATION_UNAVAILABLE")
        if not variant:
            from .learning_obligations import NEW_TRANSFER_STAGES
            if stage in NEW_TRANSFER_STAGES:
                from .stage_source_review import require_mistake_stage
                require_mistake_stage(connection, child_id, item, candidate, stage, mistake_id)
    else:
        # Source labels alone are not proof: compare to the shipped original.
        from .learning_state import ITEMS, EXTENDED_ITEMS, SPECIALIZED_ITEMS
        original = next((row for row in (*ITEMS, *SPECIALIZED_ITEMS, *EXTENDED_ITEMS) if row[0] == item_id), None)
        if not original or not source.startswith("hsc_original"):
            raise LocalSupplyError("SUPPLY_SEED_NOT_VERIFIED")
        expected = (original[1], original[2], original[3], original[4], original[5], original[6])
        actual = (item["knowledge_node_id"], item["prompt"], item["answer_type"], json.loads(item["expected_answer_json"]), item["explanation"], item["difficulty"])
        original_stimuli = {"stimulus_text": original[7] if len(original) > 7 else None,
                            "stimulus_asset": original[8] if len(original) > 8 else None}
        from .specialized_catalog import SCHOOL_LISTENING_STIMULI
        if item_id in SCHOOL_LISTENING_STIMULI:
            original_stimuli['stimulus_text'] = SCHOOL_LISTENING_STIMULI[item_id]
        expected_mode = original[9] if len(original) > 9 else 'objective'
        if (actual != expected or _stimuli(item) != _stimuli(original_stimuli)
                or item['assessment_mode'] != expected_mode):
            raise LocalSupplyError("SUPPLY_SEED_CHANGED")
    return dict(item)


def validate_candidate_source(connection, table, candidate, *, seen=None):
    variant = table == "variant_candidate"
    job_table = "variant_generation_job" if variant else "specialized_supply_job"
    job = connection.execute(f"SELECT * FROM {job_table} WHERE job_id=? AND child_id=?", (candidate["job_id"], candidate["child_id"])).fetchone()
    if not job:
        raise LocalSupplyError("SUPPLY_JOB_UNAVAILABLE")
    if variant:
        mistake = connection.execute("SELECT * FROM mistake_case WHERE mistake_id=? AND child_id=? AND status!='excluded'",
            (candidate["mistake_id"], candidate["child_id"])).fetchone()
        if not mistake or not mistake["confirmed_at"]:
            raise LocalSupplyError("SUPPLY_MISTAKE_WITHDRAWN")
        seed = validated_variant_seed(connection, candidate["child_id"], candidate["mistake_id"], seen=seen)
    else:
        seed = validate_item(connection, candidate["child_id"], candidate["seed_item_id"], _seen=seen)
    if variant and candidate["knowledge_node_id"] != seed["knowledge_node_id"]:
        raise LocalSupplyError("SUPPLY_NODE_MISMATCH")
    snapshot = _read_source_snapshot(job["source_snapshot_json"] if "source_snapshot_json" in job.keys() else None)
    _validate_source_snapshot(snapshot, seed)
    validation = json.loads(candidate["validation_json"])
    if not variant and (job["generation_mode"] == "curriculum_review"
                        or validation.get("ruleset") == "curriculum_review_v1"):
        from .curriculum_supply import validate_curriculum_candidate
        validate_curriculum_candidate(connection, candidate)
    elif not variant and job['generation_mode'] == 'model':
        from .specialized_closed_contract import validate_candidate
        validate_candidate(connection, candidate, seed, job)
    if validation.get("ruleset") == "local_deterministic_v1":
        rebuilt = build_candidate(seed, candidate["stage"], validation["ordinal"])
        for key in ("prompt", "explanation", "difficulty"):
            if rebuilt[key] != candidate[key]:
                raise LocalSupplyError("SUPPLY_PROOF_MISMATCH")
        if rebuilt["expected_answer"] != json.loads(candidate["expected_answer_json"]):
            raise LocalSupplyError("SUPPLY_PROOF_MISMATCH")
    return seed


def validated_variant_seed(connection, child_id, mistake_id, *, seen=None):
    """A confirmed paper question can seed drafts, never certify its original answer."""
    from .mistake_source_revision import latest, current
    revision = current(connection, mistake_id, child_id) if latest(connection, mistake_id, child_id) else None
    if revision and (revision['authority'] != 'child_confirmed_recapture' or revision['status'] != 'confirmed'):
        # A spoken correction is a teaching source, not independently reviewed
        # evidence for certifying generated transfer questions.
        raise LocalSupplyError('SUPPLY_PAPER_CORRECTION_REVIEW_REQUIRED')
    mistake = connection.execute("""SELECT mc.*,kn.grade_band,kn.track,kn.status AS node_status
        FROM mistake_case mc JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE mc.mistake_id=? AND mc.child_id=? AND mc.status!='excluded'
          AND mc.confirmed_at IS NOT NULL""", (mistake_id, child_id)).fetchone()
    if not mistake:
        raise LocalSupplyError("SUPPLY_MISTAKE_WITHDRAWN")
    if mistake["diagnostic_item_id"]:
        return validate_item(connection, child_id, mistake["diagnostic_item_id"], _seen=seen)
    child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
    if (not child or mistake["node_status"] != "active" or not allows_learning_grade(child[0], mistake['grade_band'], mistake['track'])
            or mistake["source_type"] != "document_camera"):
        raise LocalSupplyError("SUPPLY_SOURCE_UNAVAILABLE")
    artifact_id, separator, candidate_id = (mistake["source_ref"] or "").partition("#")
    if not separator:
        raise LocalSupplyError("SUPPLY_PAPER_CONFIRMATION_REQUIRED")
    row = connection.execute("""SELECT c.question_text,c.region_json,c.confidence,c.updated_at,
        a.extracted_text,a.details_json,a.status,a.session_id
        FROM mistake_capture_candidate c JOIN capture_artifact a USING(artifact_id)
        JOIN daily_session s ON s.session_id=a.session_id
        WHERE c.candidate_id=? AND c.artifact_id=? AND c.child_id=? AND a.child_id=?
          AND s.child_id=? AND c.mistake_id=? AND c.status='saved'
          AND a.artifact_type='document_ocr'""",
        (candidate_id, artifact_id, child_id, child_id, child_id, mistake_id)).fetchone()
    if not row or row["status"] not in {"completed", "quality_warning"} or row["question_text"] != mistake["prompt_summary"]:
        raise LocalSupplyError("SUPPLY_PAPER_SOURCE_CHANGED")
    # Hash local provenance rather than sending identifiers, whole pages or handwriting.
    provenance = digest({**dict(row), "artifact_id": artifact_id, "candidate_id": candidate_id,
                         "mistake_id": mistake_id, "confirmed_at": mistake["confirmed_at"]})
    from .paper_formula_review import resolve
    artifact = connection.execute('SELECT * FROM capture_artifact WHERE artifact_id=?', (artifact_id,)).fetchone()
    candidate = connection.execute('SELECT * FROM mistake_capture_candidate WHERE candidate_id=?', (candidate_id,)).fetchone()
    try:
        review = resolve(connection, artifact, candidate)
    except (ValueError, TypeError, KeyError) as error:
        raise LocalSupplyError('SUPPLY_PAPER_FORMULA_REVIEW_INVALID') from error
    if review:
        provenance = digest({'original': provenance, 'formula_review': review})
    return {"diagnostic_item_id": None, "knowledge_node_id": mistake["knowledge_node_id"],
            "prompt": review['question'] if review else row["question_text"], "answer_type": "text", "expected_answer_json": "{}",
            "explanation": "", "difficulty": 1, "stimulus_text": "", "stimulus_asset": "",
            "source_ref": "confirmed_paper_question_v1:" + provenance}


SUPPORTED_NODES = {"node-j3-math-discriminant", "node-oly-counting", "node-oly-pattern"}


def build_candidate(seed, stage: str, ordinal: int) -> dict:
    if stage not in {"parallel", "near_transfer", "representation_transfer", "discrimination", "transfer"}:
        raise LocalSupplyError("LOCAL_STAGE_UNSUPPORTED")
    if not 0 <= ordinal < 36:
        raise LocalSupplyError("LOCAL_TEMPLATE_UNAVAILABLE")
    try:
        extra = closed_template(seed, stage, ordinal)
    except ValueError as error:
        raise LocalSupplyError(str(error)) from error
    if extra:
        return {"stage": stage, "prompt": extra["prompt"], "answer_type": "text", "difficulty": 2,
            "expected_answer": {"keywords": [extra["answer"]], "local_rule": "local_exact_v1",
                "exact_answers": [extra["answer"]], "answer_format": extra["answer_format"]},
            "explanation": extra["explanation"], "validation": {"passed": True, "ruleset": "local_deterministic_v1",
                "ordinal": ordinal, "proof": extra["proof"], "source_item_id": seed["diagnostic_item_id"],
                "source_fingerprint": digest(item_snapshot(seed)), "auto_qualification_allowed": extra["auto_qualification"],
                "pedagogical_review_required": True}}
    if seed["knowledge_node_id"] not in SUPPORTED_NODES:
        raise LocalSupplyError("LOCAL_TEMPLATE_UNAVAILABLE")
    n = ordinal + 3
    node = seed["knowledge_node_id"]
    if node == "node-j3-math-discriminant":
        b, c = 2 * n, n * n + (ordinal % 3 - 1)
        delta = b * b - 4 * c
        if stage in {"parallel", "near_transfer"}:
            prompt = f"只计算方程 x²+{b}x+{c}=0 的判别式，回答一个整数。"
            answer = str(delta)
            explanation = f"判别式为 b²-4ac={b}²-4×1×{c}={delta}。"
        elif stage == "representation_transfer":
            prompt = f"二次方程的系数为a=1、b={b}、c={c}。不求根，只回答判别式的整数值。"
            answer = str(delta)
            explanation = f"将三个系数代入b²-4ac，得到{delta}。"
        elif stage == "discrimination":
            prompt = f"方程 x²+{b}x+{c}=0 有几个不同的实数根？A：0个；B：1个；C：2个。只回答选项字母。"
            answer = "C" if delta > 0 else "B" if delta == 0 else "A"
            explanation = f"判别式为{delta}，所以选择{answer}。"
        else:
            prompt = f"方程 x²+{b}x+k=0 有两个相等的实数根，k是多少？只回答一个整数。"
            answer = str(n * n)
            explanation = f"两个根相等要求判别式{b}²-4k=0，因此k={answer}。"
        proof = {"family": "quadratic_discriminant", "a": 1, "b": b, "c": c, "delta": delta}
    elif node == "node-oly-counting":
        a, b = n, n + 2
        if stage == "discrimination":
            prompt = f"甲路线有{a}班车，乙路线有{b}班车，两条路线都能直达同一目的地。只选一班车出行，共有几种选择？只回答整数。"
            answer = str(a + b)
            explanation = f"两类选择互不重叠，而且只选一班车，所以相加：{a}+{b}={answer}。"
        else:
            prompt = f"从甲地到乙地有{a}条路，从乙地到丙地有{b}条路。先到乙地再到丙地，每段各选一条路，共有几种走法？只回答整数。"
            answer = str(a * b)
            explanation = f"第一段的每种选择都有{b}种第二段选择，乘法计数为{a}×{b}={answer}。"
        proof = {"family": "finite_choices", "a": a, "b": b, "operation": "sum" if stage == "discrimination" else "product"}
    else:
        first, step, index = n, n + 1, n + 3
        prompt = f"一个数列第1项是{first}，以后每一项都比前一项增加{step}。第{index}项是多少？只回答整数。"
        answer = str(first + (index - 1) * step)
        explanation = f"从第1项到第{index}项共增加{index - 1}次，所以{first}+{index - 1}×{step}={answer}。"
        proof = {"family": "arithmetic_sequence", "first": first, "step": step, "index": index}
        if stage not in {"parallel", "near_transfer"}:
            raise LocalSupplyError("LOCAL_STAGE_UNSUPPORTED")
    # Identical templates must not pretend to cover representation/far transfer.
    if node == "node-oly-counting" and stage not in {"parallel", "near_transfer", "discrimination"}:
        raise LocalSupplyError("LOCAL_STAGE_UNSUPPORTED")
    return {"stage": stage, "prompt": prompt, "answer_type": "text", "difficulty": 2,
        "expected_answer": {"keywords": [answer], "local_rule": "local_exact_v1", "exact_answers": [answer]},
        "explanation": explanation, "validation": {"passed": True, "ruleset": "local_deterministic_v1",
            "ordinal": ordinal, "proof": proof, "source_item_id": seed["diagnostic_item_id"],
            "source_fingerprint": digest(item_snapshot(seed)), "pedagogical_review_required": True}}


def score_local_item(item, answer: str) -> tuple[float, str] | None:
    rubric = json.loads(item["expected_answer_json"])
    if rubric.get("local_rule") != "local_exact_v1":
        return None
    if rubric.get('answer_format')=='reviewed_spoken_numeric_tuple_v1':
        from .spoken_numeric_tuple import score
        correctness=score(rubric,answer)
        if correctness is None:return None
        return (1.0,'回答正确。') if correctness else (0.0,'这次答案还需要核对。'+item['explanation'])
    if rubric.get('answer_format') == 'reviewed_spoken_numeric_v1':
        from .spoken_numeric import score
        correctness = score(rubric, answer)
        if correctness is None:
            return None
        return (1.0, "回答正确。") if correctness else (0.0, "这次答案还需要核对。" + item["explanation"])
    if rubric.get("answer_format") in {"pet_exact_text_v1", "reviewed_exact_text_v1"}:
        from .pet_reviewed_source import normalize_pet_exact_text
        if rubric['answer_format'] == 'reviewed_exact_text_v1':
            from .curriculum_objective_source import normalize as normalize_pet_exact_text
        value = normalize_pet_exact_text(answer)
        references = rubric.get("exact_answers")
        if not isinstance(references, list) or not references or not all(isinstance(v, str) and v.strip() for v in references):
            return None
        matched = bool(value) and value in {normalize_pet_exact_text(v) for v in references}
        return (1.0, "回答正确。") if matched else (0.0, "这次答案还需要核对。" + item["explanation"])
    from .learning_state import normalize_spoken_math
    value = normalize_spoken_math(unicodedata.normalize("NFKC", answer)).strip("。，,.!！ ")
    value = re.sub(r"^(?:答案是|答案为|我选|选择|选)", "", value).strip().lower()
    matched = value in {str(v).lower() for v in rubric["exact_answers"]}
    if rubric.get("answer_format") == "rational" and len(value) <= 32 and re.fullmatch(r"[+-]?\d+(?:/\d+|\.\d+)?", value):
        try:
            matched = Fraction(value) in {Fraction(v) for v in rubric["exact_answers"]}
        except (ValueError, ZeroDivisionError):
            matched = False
    return (1.0, "回答正确。") if matched else (0.0, "这次答案还需要核对。" + item["explanation"])


def qualified_items(connection, child_id: str) -> list[dict]:
    """Read-only source/ownership filtered candidates for plan and gap readers."""
    rows = connection.execute("""SELECT di.*,kn.subject,kn.title,kn.description,kn.grade_band,kn.track
        FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE di.status='active' AND kn.status='active' ORDER BY di.diagnostic_item_id""").fetchall()
    result = []
    for row in rows:
        try:
            validate_item(connection, child_id, row["diagnostic_item_id"])
        except (LocalSupplyError, ValueError, KeyError, TypeError):
            continue
        result.append(dict(row))
    return result


class LocalSupplyQueue:
    """Atomic local work; no model/network dependency or long write lease."""
    def __init__(self, service, *, variant=False):
        self.service, self.database, self.variant = service, service.database, variant
        self.jobs = "variant_generation_job" if variant else "specialized_supply_job"
        self.candidates = "variant_candidate" if variant else "specialized_supply_candidate"

    def enqueue(self, child_id, seed_item_id, action_id, *, mistake_id=None, track=None, domain_id=None, count=3):
        self.service.ensure_schema()
        if not action_id.strip() or not 1 <= int(count) <= 12:
            raise LocalSupplyError("INVALID_LOCAL_REQUEST")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            seed = validate_item(connection, child_id, seed_item_id)
            old = connection.execute(f"SELECT * FROM {self.jobs} WHERE client_action_id=?", (action_id,)).fetchone()
            if old:
                if old["child_id"] != child_id or old["generation_mode"] != "local" or (self.variant and old["mistake_id"] != mistake_id) or (not self.variant and (old["track"] != track or old["domain_id"] != domain_id or len(json.loads(old["stages_json"])) != count)):
                    raise LocalSupplyError("SUPPLY_ACTION_CONFLICT")
                try:
                    _validate_source_snapshot(_read_source_snapshot(old["source_snapshot_json"]), seed)
                except LocalSupplyError as error:
                    if str(error) == "SUPPLY_SOURCE_CHANGED":
                        raise LocalSupplyError("SUPPLY_ACTION_CONFLICT") from error
                    raise
                return self.service._job(connection, old["job_id"])
            if self.variant:
                mistake = connection.execute("""SELECT mc.*,mmg.requirements_json FROM mistake_case mc JOIN mistake_mastery_gate mmg USING(mistake_id)
                    WHERE mc.mistake_id=? AND mc.child_id=? AND mc.status NOT IN ('excluded','mastered') AND mc.confirmed_at IS NOT NULL""", (mistake_id, child_id)).fetchone()
                if not mistake or mistake["diagnostic_item_id"] != seed_item_id or mistake["knowledge_node_id"] != seed["knowledge_node_id"]:
                    raise LocalSupplyError("SUPPLY_MISTAKE_UNAVAILABLE")
                requirements = json.loads(mistake["requirements_json"])
                stages = [stage for stage in ("near_transfer", "representation_transfer", "discrimination", "far_transfer") for _ in range(min(4, int(requirements.get(stage, 0))))]
                active = connection.execute(f"SELECT job_id FROM {self.jobs} WHERE child_id=? AND mistake_id=? AND generation_mode='local' AND status IN ('pending','processing','blocked')", (child_id, mistake_id)).fetchone()
            else:
                stages = ["parallel", "discrimination", "transfer"][:count]
                stages += ["parallel"] * max(0, count - 3)
                active = connection.execute(f"SELECT job_id FROM {self.jobs} WHERE child_id=? AND domain_id=? AND generation_mode='local' AND status IN ('pending','processing','blocked')", (child_id, domain_id)).fetchone()
            if active:
                return self.service._job(connection, active[0])
            job_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
            common = (job_id, child_id, json.dumps(stages), now, now, action_id, json.dumps(item_snapshot(seed), ensure_ascii=False))
            if self.variant:
                connection.execute(f"""INSERT INTO {self.jobs}(job_id,child_id,stages_json,created_at,updated_at,client_action_id,source_snapshot_json,mistake_id,requested_count,status,generation_mode)
                    VALUES (?,?,?,?,?,?,?,?,?,'pending','local')""", (*common, mistake_id, len(stages)))
            else:
                connection.execute(f"""INSERT INTO {self.jobs}(job_id,child_id,stages_json,created_at,updated_at,client_action_id,source_snapshot_json,track,domain_id,subject,seed_item_id,status,generation_mode)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending','local')""", (*common, track, domain_id, seed["subject"] if "subject" in seed else connection.execute("SELECT subject FROM knowledge_node WHERE knowledge_node_id=?", (seed["knowledge_node_id"],)).fetchone()[0], seed_item_id))
            connection.commit()
            return self.service._job(connection, job_id)

    def process_pending(self, limit=10, *, should_stop=None):
        if should_stop is not None and should_stop():
            return {"jobs": [], "processed": 0, "local_only": True, "cloud_transmission_allowed": False, "stopped": True}
        self.service.ensure_schema()
        results = []
        stopped = False
        for _ in range(max(0, min(int(limit), 50))):
            if should_stop is not None and should_stop():
                stopped = True
                break
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                job = connection.execute(f"SELECT * FROM {self.jobs} WHERE generation_mode='local' AND status='pending' ORDER BY created_at,job_id LIMIT 1").fetchone()
                if not job:
                    break
                try:
                    snapshot = _read_source_snapshot(job["source_snapshot_json"])
                    if not isinstance(snapshot.get("diagnostic_item_id"), str):
                        raise LocalSupplyError("SUPPLY_SOURCE_REVIEW_REQUIRED")
                    seed = validate_item(connection, job["child_id"], snapshot["diagnostic_item_id"])
                    _validate_source_snapshot(snapshot, seed)
                    if self.variant:
                        context = self.service._generation_context(connection, job["job_id"])
                    else:
                        context = self.service._context(connection, job["job_id"])
                    connection.execute("SAVEPOINT local_candidates")
                    stages = json.loads(job["stages_json"])
                    existing = connection.execute(f"SELECT stage FROM {self.candidates} WHERE job_id=?", (job["job_id"],)).fetchall()
                    for row in existing:
                        if row[0] in stages:
                            stages.remove(row[0])
                    blocked = []
                    for stage in stages:
                        stored = False
                        for ordinal in range(36):
                            try:
                                candidate = build_candidate(seed, stage, ordinal)
                            except LocalSupplyError as error:
                                blocked.append(str(error))
                                break
                            before = connection.execute(f"SELECT count(*) FROM {self.candidates} WHERE job_id=?", (job["job_id"],)).fetchone()[0]
                            (self.service._store_candidate if self.variant else self.service._store)(connection, job, context, candidate)
                            after = connection.execute(f"SELECT count(*) FROM {self.candidates} WHERE job_id=?", (job["job_id"],)).fetchone()[0]
                            if after > before:
                                stored = True
                                break
                        if not stored and not blocked:
                            blocked.append("LOCAL_TEMPLATE_EXHAUSTED")
                    status, error = ("blocked", blocked[0]) if blocked else ("completed", None)
                    connection.execute("RELEASE local_candidates")
                except LocalSupplyError as failure:
                    status, error = "blocked", str(failure)
                except Exception:
                    # A storage fault rolls back all new candidates in this attempt.
                    connection.rollback()
                    connection.execute("BEGIN IMMEDIATE")
                    status, error = "failed", "LOCAL_SUPPLY_FAILED"
                connection.execute(f"UPDATE {self.jobs} SET status=?,error_code=?,attempt_count=attempt_count+1,provider='local',model='deterministic_v1',updated_at=? WHERE job_id=?",
                    (status, error, datetime.now(timezone.utc).isoformat(), job["job_id"]))
                connection.commit()
                results.append(self.service._job(connection, job["job_id"]))
            if should_stop is not None and should_stop():
                stopped = True
                break
        report = {"jobs": results, "processed": len(results), "local_only": True, "cloud_transmission_allowed": False}
        if stopped:
            report["stopped"] = True
        return report

    def retry(self, child_id, job_id, *, allow_model_retry=False):
        self.service.ensure_schema()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT * FROM {self.jobs} WHERE job_id=? AND child_id=?", (job_id, child_id)).fetchone()
            if row and row["generation_mode"] == "curriculum_review":
                raise LocalSupplyError("CURRICULUM_USE_VERSIONED_REVIEW")
            if not row or (row["generation_mode"] != "local" and not allow_model_retry):
                raise LocalSupplyError("LOCAL_JOB_UNAVAILABLE")
            if row["status"] in {"blocked", "failed"}:
                connection.execute(f"UPDATE {self.jobs} SET status='pending',error_code=NULL,attempt_count=0,retry_count=retry_count+1,updated_at=? WHERE job_id=?", (datetime.now(timezone.utc).isoformat(), job_id))
            connection.commit()
            return self.service._job(connection, job_id)

    def qualify_local(self, child_id, candidate_id, action_id, *, connection=None):
        """Explicitly activate a reproducible local proof, never an LLM draft.

        Records local rule qualification, not a fabricated parent approval.
        """
        owns_transaction = connection is None
        if owns_transaction:
            self.service.ensure_schema()
        elif not connection.in_transaction:
            raise LocalSupplyError('SUPPLY_TRANSACTION_REQUIRED')
        if not action_id.strip():
            raise LocalSupplyError("QUALIFICATION_ACTION_REQUIRED")
        with self.database.connect() if owns_transaction else nullcontext(connection) as connection:
            if owns_transaction:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT * FROM {self.candidates} WHERE candidate_id=? AND child_id=?", (candidate_id, child_id)).fetchone()
            if not row:
                raise LocalSupplyError("LOCAL_CANDIDATE_UNAVAILABLE")
            key = "local-qualification:" + action_id
            used = connection.execute(f"SELECT candidate_id FROM {self.candidates} WHERE review_action_id=?", (key,)).fetchone()
            if used and used[0] != candidate_id:
                raise LocalSupplyError("QUALIFICATION_ACTION_CONFLICT")
            if row["status"] != "needs_review":
                if row["status"] == "promoted" and row["review_action_id"] == key:
                    validate_item(connection, child_id, row["diagnostic_item_id"])
                    return self.service._candidate(row)
                raise LocalSupplyError("LOCAL_CANDIDATE_ALREADY_REVIEWED")
            job = connection.execute(f"SELECT generation_mode FROM {self.jobs} WHERE job_id=? AND child_id=?", (row["job_id"], child_id)).fetchone()
            validation = json.loads(row["validation_json"])
            if not job or job[0] != "local" or validation.get("ruleset") != "local_deterministic_v1" or not validation.get("passed"):
                raise LocalSupplyError("MODEL_CANDIDATE_REQUIRES_PARENT_REVIEW")
            seed = validate_candidate_source(connection, self.candidates, row)
            rubric = json.loads(seed["expected_answer_json"])
            rebuilt = build_candidate(seed, row["stage"], validation["ordinal"])
            if seed["answer_type"] in {"writing", "speaking", "read_aloud"} or rubric.get("task_points") or rebuilt["validation"].get("auto_qualification_allowed") is False:
                raise LocalSupplyError("OPEN_RESPONSE_SEED_REQUIRES_PARENT_REVIEW")
            kind = "variant" if self.variant else "specialized"
            item_id = "local-" + kind + "-" + candidate_id
            connection.execute("""INSERT INTO diagnostic_item(diagnostic_item_id,knowledge_node_id,prompt,answer_type,
                expected_answer_json,explanation,difficulty,assessment_mode,source_ref,status)
                VALUES (?,?,?,'text',?,?,?,'objective',?,'active')""",
                (item_id, seed["knowledge_node_id"], row["prompt"], row["expected_answer_json"], row["explanation"], row["difficulty"],
                 "local_verified_" + kind + ":" + candidate_id))
            validation["qualification"] = "deterministic_answer_verified_v1"
            validation["parent_approved"] = False
            validation["pedagogical_review_required"] = False
            validation["qualification_scope"] = "bounded_versioned_closed_template_not_model_or_teacher_certification"
            connection.execute(f"UPDATE {self.candidates} SET status='promoted',diagnostic_item_id=?,reviewed_at=?,review_action_id=?,review_note=?,validation_json=? WHERE candidate_id=?",
                (item_id, datetime.now(timezone.utc).isoformat(), key, "本地确定性模板与答案证明已重算；不是家长审批或模型自评。", json.dumps(validation, ensure_ascii=False), candidate_id))
            if owns_transaction:
                connection.commit()
            return self.service._candidate(connection.execute(f"SELECT * FROM {self.candidates} WHERE candidate_id=?", (candidate_id,)).fetchone())

    def bind_due_reviews(self, child_id, limit=6):
        """Pin eligible promoted content into the existing durable review tasks.

        Never replace a question after execution started. Repeated maintenance
        keeps an already-qualified binding rather than rotating its question.
        """
        from .repository import StudyRepository, PlanInputError
        self.service.ensure_schema()
        bindings, skipped = [], []
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reviews = connection.execute("""SELECT * FROM review_task WHERE child_id=? AND status='pending'
                AND julianday(due_at)<=julianday('now') ORDER BY due_at,review_task_id LIMIT ?""", (child_id, max(0, min(int(limit), 50)))).fetchall()
            for review in reviews:
                try:
                    StudyRepository._validate_review_source(connection, review["review_task_id"], child_id, allowed_statuses=("pending",))
                except PlanInputError:
                    skipped.append({"review_task_id": review["review_task_id"], "reason": "source_unavailable"})
                    continue
                active = connection.execute("""SELECT 1 FROM plan_item WHERE source_type='due_review' AND source_ref=? AND started_at IS NOT NULL LIMIT 1""", (review["review_task_id"],)).fetchone()
                if active:
                    continue
                current = connection.execute("SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?", (review["diagnostic_item_id"],)).fetchone()
                if current and (current[0] or "").startswith(("parent_approved_", "local_verified_")):
                    try:
                        validate_item(connection, child_id, review["diagnostic_item_id"])
                        continue
                    except LocalSupplyError:
                        pass
                candidates = connection.execute(f"""SELECT di.diagnostic_item_id FROM {self.candidates} c JOIN diagnostic_item di USING(diagnostic_item_id)
                    WHERE c.child_id=? AND c.status='promoted' AND di.status='active' AND di.knowledge_node_id=?
                    ORDER BY (SELECT count(*) FROM diagnostic_attempt da WHERE da.child_id=c.child_id AND da.diagnostic_item_id=di.diagnostic_item_id),
                      (SELECT count(*) FROM review_task other WHERE other.child_id=c.child_id AND other.diagnostic_item_id=di.diagnostic_item_id AND other.status='pending'),
                      c.created_at,c.candidate_id""", (child_id, review["knowledge_node_id"])).fetchall()
                for candidate in candidates:
                    try:
                        item = validate_item(connection, child_id, candidate[0])
                    except LocalSupplyError:
                        continue
                    connection.execute("UPDATE review_task SET diagnostic_item_id=? WHERE review_task_id=?", (item["diagnostic_item_id"], review["review_task_id"]))
                    bindings.append({"review_task_id": review["review_task_id"], "diagnostic_item_id": item["diagnostic_item_id"]})
                    break
            connection.commit()
        return {"bindings": bindings, "skipped": skipped, "local_only": True, "cloud_transmission_allowed": False}
