"""Reviewed curriculum supply; no model calls, implicit approvals or learner writes.

Actor identifiers must come from authenticated operator identities, not model JSON.
This service owns its schema. Privacy learning-scope deletion must remove packages
(events cascade) and selections before the existing supply/source cleanup.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from .family_clock import family_today
from fractions import Fraction
import json
from uuid import uuid4

from .local_supply import LocalSupplyError, digest, item_snapshot, validate_item


class CurriculumSupplyError(LocalSupplyError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _require(value, code):
    if not value:
        raise CurriculumSupplyError(code)


def _text(value, maximum=1200):
    _require(isinstance(value, str) and 0 < len(value.strip()) <= maximum, "CURRICULUM_INPUT")
    return value.strip()


def _arithmetic_answer(prompt):
    """Compute the question itself, never a separate author-supplied expression."""
    _require(prompt.startswith("计算：") and prompt.endswith("。"), "CURRICULUM_QUESTION_FORM_UNSUPPORTED")
    expression = prompt[3:-1]
    _require(len(expression) <= 200, "CURRICULUM_EXPRESSION_LIMIT")
    try:
        tree = ast.parse(expression, mode="eval")
        _require(len(list(ast.walk(tree))) <= 80, "CURRICULUM_EXPRESSION_LIMIT")

        def evaluate(node):
            if isinstance(node, ast.Constant) and type(node.value) is int:
                value = Fraction(node.value)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Add):
                    value = left + right
                elif isinstance(node.op, ast.Sub):
                    value = left - right
                elif isinstance(node.op, ast.Mult):
                    value = left * right
                else:
                    value = left / right
            else:
                raise CurriculumSupplyError("CURRICULUM_QUESTION_FORM_UNSUPPORTED")
            _require(abs(value.numerator) <= 10**9 and value.denominator <= 10**9,
                     "CURRICULUM_EXPRESSION_LIMIT")
            return value

        return str(evaluate(tree.body))
    except (SyntaxError, ValueError, ZeroDivisionError, RecursionError) as error:
        if isinstance(error, CurriculumSupplyError):
            raise
        raise CurriculumSupplyError("CURRICULUM_QUESTION_FORM_UNSUPPORTED") from error


def _question_key(prompt):
    if prompt.startswith("计算：") and prompt.endswith("。") and len(prompt) <= 205:
        try:
            return ast.dump(ast.parse(prompt[3:-1].strip(), mode="eval"), include_attributes=False)
        except (SyntaxError, RecursionError):
            pass
    from .learning_obligations import question_identity
    return question_identity({"prompt": prompt})


def _selection(connection, child_id, subject):
    row = connection.execute("""SELECT * FROM curriculum_supply_selection
        WHERE child_id=? AND subject=? ORDER BY version DESC LIMIT 1""", (child_id, subject)).fetchone()
    _require(row is not None, "CURRICULUM_SELECTION_REQUIRED")
    return dict(row)


from .standard_relocation_provenance import snapshot_read


@snapshot_read
def _evidence(connection, child_id, payload, *, candidate_id=None, first_node_id=None):
    if first_node_id is not None:
        seed = None
        raw_node = connection.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=? AND status='active'", (first_node_id,)).fetchone()
        child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
        _require(raw_node is not None and child is not None and raw_node['grade_band'] == child['grade']
                 and raw_node['track'] == 'school' and raw_node['node_type'] == 'skill', 'CURRICULUM_FIRST_NODE_UNAVAILABLE')
        node = dict(raw_node)
    else:
        seed = validate_item(connection, child_id, payload["seed_item_id"])
        node = dict(connection.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=?",
                                       (seed["knowledge_node_id"],)).fetchone())
    selection = _selection(connection, child_id, node["subject"])
    scope = json.loads(selection["scope_json"])
    from .pet_reviewed_source import VALIDATOR, parse_listening_source
    from . import curriculum_objective_source as objective
    from . import pet_writing_source as writing
    from . import open_response_source as opened
    from . import pet_oral_source as oral
    pet = payload["validator"] == VALIDATOR
    textual = payload['validator'] == objective.VALIDATOR
    written = payload['validator'] == writing.VALIDATOR
    open_task = payload['validator'] == opened.VALIDATOR
    oral_task = payload['validator'] == oral.VALIDATOR
    if oral_task:
        original = oral.parse(_json(payload.get('pet_oral')))
        _require(oral.node_allowed(original, node, seed) and original['grade'] == scope['grade'],
                 'CURRICULUM_SCOPE_MISMATCH')
    elif pet:
        from .listening_modality import listening_source_matches_node
        _require(listening_source_matches_node(payload['pet_listening'], node)
                 and node["grade_band"] == scope["grade"] + ('-PET' if node['track'] == 'pet' else ''),
                 "CURRICULUM_SCOPE_MISMATCH")
    elif written:
        _require(node['track'] == 'pet' and node['code'].startswith('PET.WRITING.')
                 and seed['answer_type'] == 'writing' and node['grade_band'] == scope['grade']+'-PET', 'CURRICULUM_SCOPE_MISMATCH')
    elif textual:
        _require(objective.node_allowed(node, payload['objective_source'] if first_node_id is not None else seed) and node['grade_band'] == scope['grade'] +
                 ({'pet': '-PET', 'competition_math': '竞赛'}.get(node['track'], '')), 'CURRICULUM_SCOPE_MISMATCH')
    else:
        _require(scope["grade"] == node["grade_band"], "CURRICULUM_SCOPE_MISMATCH")
    source = connection.execute("""SELECT ks.*,ke.excerpt_id,ke.content_text,
        ke.content_hash AS excerpt_hash,ke.sequence FROM knowledge_source ks
        JOIN knowledge_excerpt ke USING(source_id) WHERE ke.excerpt_id=? AND ks.child_id=?""",
        (payload["excerpt_id"], child_id)).fetchone()
    _require(source is not None, "CURRICULUM_TEXTBOOK_UNAVAILABLE")
    source = dict(source)
    from .knowledge_graph.paper_objective_bridge import assert_source_current
    paired = assert_source_current(connection, None, child_id, source)
    if paired is None:
        _require(source["source_id"] == scope["textbook_source_id"], "CURRICULUM_SCOPE_MISMATCH")
    else:
        # Only this exact parent-confirmed, immutable paired-page receipt can be
        # supplemental to the unchanged selected textbook; ordinary paths stay strict.
        _require((paired['seed_choice'] is None and first_node_id is not None
                  or first_node_id is None and seed is not None and paired['seed_choice'] == {
                      'diagnostic_item_id': seed['diagnostic_item_id'], 'binding': digest(seed)})
                 and textual and paired["node"]["knowledge_node_id"] == node["knowledge_node_id"]
                 and paired["node"]["binding"] == digest(node) and paired["selection"]["binding"] == digest(selection)
                 and all(payload[name] == paired["standard"][name]
                         for name in ("requirement_id", "standard_page", "standard_quote")),
                 "CURRICULUM_SCOPE_MISMATCH")
    _require(source["review_status"] == "approved" and source.get('current_version', 1) == 1
             and source["license_status"] in {"public", "family_owned_index"}
             and source["source_type"] in {"textbook_index", "school_material", "curriculum"},
             "CURRICULUM_TEXTBOOK_UNREVIEWED")
    today = family_today().isoformat()
    _require((not source["effective_from"] or source["effective_from"] <= today)
             and (not source["effective_to"] or source["effective_to"] >= today), "CURRICULUM_TEXTBOOK_EXPIRED")
    _require((source["grade_band"], source["subject"], source["version_label"]) ==
             (scope["grade"], scope["subject"], scope["edition"]), "CURRICULUM_SCOPE_MISMATCH")
    # This validator accepts a reviewed source question, not an invented derivative.
    if oral_task:
        original = oral.parse(source['content_text'])
        _require(payload.get('pet_oral') == original and payload['prompt'] == original['prompt'],
                 'CURRICULUM_QUESTION_SOURCE_MISMATCH')
        oral.check(payload, seed)
    elif open_task:
        original = opened.parse(source['content_text'])
        _require(payload.get('open_response') == original, 'CURRICULUM_QUESTION_SOURCE_MISMATCH')
        opened.check(payload,seed,node=node,connection=connection,child_id=child_id)
        if original['schema'] == opened.VISUAL_SCHEMA:
            from .visual_material_host import assert_host_visual_current
            for material in original['material_bindings']:
                assert_host_visual_current(connection, child_id, material['visual'], require_managed=True)
    elif pet:
        original = parse_listening_source(source["content_text"])
        _require(payload.get("pet_listening") == original and payload["prompt"] == original["prompt"],
                 "CURRICULUM_QUESTION_SOURCE_MISMATCH")
    elif written:
        original = writing.parse(source['content_text'])
        _require(payload.get('pet_writing') == original and payload['prompt'] == original['prompt']
                 and (original['subject'],original['grade'],original['knowledge_node_id']) ==
                 (node['subject'],scope['grade'],node['knowledge_node_id']), 'CURRICULUM_QUESTION_SOURCE_MISMATCH')
    elif textual:
        original = objective.parse(source['content_text'])
        _require(payload.get('objective_source') == original and payload['prompt'] == original['prompt']
                 and (original['subject'], original['grade'], original['knowledge_node_id']) ==
                 (node['subject'], scope['grade'], node['knowledge_node_id']), 'CURRICULUM_QUESTION_SOURCE_MISMATCH')
    else:
        _require(payload["prompt"] == source["content_text"].strip(), "CURRICULUM_QUESTION_SOURCE_MISMATCH")
    requirement = connection.execute("SELECT * FROM curriculum_standard_requirement WHERE requirement_id=?",
                                     (payload["requirement_id"],)).fetchone()
    _require(requirement is not None and requirement["maturity"] == "verified", "CURRICULUM_STANDARD_UNREVIEWED")
    requirement = dict(requirement)
    _require(requirement["subject_title"] == scope["subject"] and requirement["stage_scope"] == scope["standard_scope"],
             "CURRICULUM_SCOPE_MISMATCH")
    _require(type(payload["standard_page"]) is int and requirement["page_start"] <= payload["standard_page"] <= requirement["page_end"],
             "CURRICULUM_STANDARD_PAGE_MISMATCH")
    standard = connection.execute("SELECT * FROM curriculum_standard_source WHERE source_id=?",
                                  (requirement["source_id"],)).fetchone()
    page = connection.execute("SELECT * FROM curriculum_standard_page WHERE source_id=? AND page_number=?",
                              (requirement["source_id"], payload["standard_page"])).fetchone()
    from .manual_standard_scope import ManualScopeError, manual_requirement
    try:
        manual = manual_requirement(connection, child_id, payload["requirement_id"],
                                    payload["standard_page"], payload["standard_quote"],
                                    knowledge_node_id=node["knowledge_node_id"])
    except ManualScopeError as e:
        raise CurriculumSupplyError(e.code) from None
    if manual is None:
        _require(standard is not None and standard["status"] in {"ocr_ready", "structured"} and page is not None,
                 "CURRICULUM_STANDARD_UNAVAILABLE")
        _require(len(payload["standard_quote"]) >= 8 and payload["standard_quote"] in page["content_text"],
                 "CURRICULUM_STANDARD_QUOTE_MISMATCH")
    else:
        _require(standard is not None and page is not None, "CURRICULUM_STANDARD_UNAVAILABLE")
    standard = {key: standard[key] for key in ("source_id", "subject_title", "version_label", "status",
        "official_url", "pdf_sha256", "text_sha256", "updated_at")}
    proof = {"selection": selection, "source": source, "requirement": requirement,
             "standard": standard, "page": dict(page), "seed": item_snapshot(seed) if seed else None}
    if paired is not None:
        proof["paired_paper_receipt_sha256"] = paired["receipt_sha256"]
    if manual is not None:
        proof["manual_requirement_review"] = manual
    if pet:
        proof["pet_node"] = node
    if oral_task:
        proof['oral_node'] = node
        proof['oral_materials'] = oral.materials(connection, child_id, original)
    if textual:
        proof['objective_node'] = node
    if written:
        proof['writing_node'] = node
    if open_task:
        proof['open_response_node'] = node
        if original['schema'] in {opened.MATERIAL_SCHEMA, opened.VISUAL_SCHEMA}:
            from .open_response_materials import resolve_source_materials
            proof['open_response_materials'] = resolve_source_materials(connection, child_id=child_id, source=original,
                                                                     candidate_id=candidate_id)
    return proof


def _check(payload, seed, *, connection, child_id):
    from . import pet_oral_source as oral
    if payload['validator'] == oral.VALIDATOR:
        return oral.check(payload, seed)
    from . import open_response_source as opened
    if payload['validator'] == opened.VALIDATOR:
        checked = opened.check_reference(payload,seed,connection=connection,child_id=child_id)
        # The full local image proof is already frozen and rechecked in _evidence.
        # Keep the public check receipt bounded, without duplicating source records.
        return {k: v for k, v in checked.items() if k != 'visual_materials'}
    from . import pet_writing_source as writing
    if payload['validator'] == writing.VALIDATOR:
        return writing.check(payload, seed)
    from . import curriculum_objective_source as objective
    if payload['validator'] == objective.VALIDATOR:
        return objective.check(payload, seed)
    from .pet_reviewed_source import VALIDATOR, check_listening_payload
    if payload["validator"] == VALIDATOR:
        return check_listening_payload(payload, seed)
    _require(payload["stage"] == "parallel", "CURRICULUM_STAGE_VALIDATOR_UNAVAILABLE")
    _require(payload["validator"] == "arithmetic_expression_v1" and seed["knowledge_node_id"] ==
             "node-p4-math-arithmetic", "CURRICULUM_VALIDATOR_UNAVAILABLE")
    answer = _arithmetic_answer(payload["prompt"])
    try:
        if payload["expected_answer"] is not None:
            _require(Fraction(payload["expected_answer"]) == Fraction(answer), "CURRICULUM_ANSWER_MISMATCH")
    except (ValueError, ZeroDivisionError) as error:
        if isinstance(error, CurriculumSupplyError):
            raise
        raise CurriculumSupplyError("CURRICULUM_ANSWER_MISMATCH") from error
    _require(payload["prompt"] != seed["prompt"], "CURRICULUM_DUPLICATE_QUESTION")
    return {"validator": "arithmetic_expression_v1", "computed_answer": answer,
            "question_hash": digest(payload["prompt"]), "payload_hash": digest(payload)}


def _source_snapshot_matches(current, frozen):
    if current == frozen:
        return True
    if not isinstance(frozen, dict) or not isinstance(frozen.get('source'), dict):
        return False
    source = current['source']
    added = {'current_version', 'predecessor_id'}
    # Migration 71 adds defaults to untouched originals. Preserve the old proof;
    # never ignore metadata on new snapshots or any real source revision.
    if (added.intersection(frozen['source']) or not added.issubset(source)
            or source['current_version'] != 1 or source['predecessor_id'] is not None):
        return False
    return {**current, 'source': {k: v for k, v in source.items() if k not in added}} == frozen


@snapshot_read
def validate_curriculum_candidate(connection, candidate):
    """Read-time eligibility used by existing plan, score and evidence projections."""
    package = connection.execute("SELECT * FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?",
                                 (candidate["candidate_id"], candidate["child_id"])).fetchone()
    _require(package is not None and package["state"] == "published", "CURRICULUM_NOT_PUBLISHED")
    payload = json.loads(package["payload_json"])
    proof = _evidence(connection, candidate["child_id"], payload, candidate_id=candidate['candidate_id'])
    _require(_source_snapshot_matches(proof, json.loads(package["source_snapshot_json"])), "CURRICULUM_SOURCE_CHANGED")
    checked = _check(payload, proof["seed"], connection=connection, child_id=candidate['child_id'])
    events = connection.execute("SELECT * FROM curriculum_supply_event WHERE candidate_id=? ORDER BY version",
                                (candidate["candidate_id"],)).fetchall()
    validations = [e for e in events if e["kind"] == "validate" and json.loads(e["result_json"])["state"] == "validated"]
    reviews = [e for e in events if e["kind"] == "review" and json.loads(e["result_json"])["state"] == "reviewed"]
    _require(validations and reviews and validations[-1]["actor"] != package["author"]
             and reviews[-1]["actor"] != package["author"] and reviews[-1]["version"] > validations[-1]["version"],
             "CURRICULUM_INDEPENDENT_REVIEW_REQUIRED")
    _require(json.loads(validations[-1]["result_json"])["check"] == checked, "CURRICULUM_PROOF_CHANGED")
    expected = checked.get("expected_answer", {"local_rule": "local_exact_v1", "exact_answers": [checked["computed_answer"]]})
    _require(json.loads(candidate["expected_answer_json"]) == expected
             and all(candidate[key] == payload[key] for key in ("prompt", "explanation", "difficulty", "stage", "seed_item_id")),
             "CURRICULUM_CONTENT_CHANGED")
    if candidate["diagnostic_item_id"]:
        item = connection.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?",
                                  (candidate["diagnostic_item_id"],)).fetchone()
        _require(item is not None and item["knowledge_node_id"] == proof["seed"]["knowledge_node_id"]
                 and item["assessment_mode"] == ('open_response_rubric' if 'open_response' in payload else
                     'pet_reference_rubric' if 'pet_writing' in payload or 'pet_oral' in payload else 'objective'), "CURRICULUM_CONTENT_CHANGED")
        if 'pet_oral' in payload:
            material = proof['oral_materials']
            _require(item['answer_type'] == payload['pet_oral']['answer_type']
                     and (item['stimulus_text'] or None) == material['stimulus_text']
                     and (item['stimulus_asset'] or None) == material['stimulus_asset'], 'CURRICULUM_CONTENT_CHANGED')
        if 'open_response' in payload:
            material = proof.get('open_response_materials')
            _require(item['answer_type'] == 'open_response' and not item['stimulus_asset']
                     and (item['stimulus_text'] or '') == (material['stimulus_text'] if material else ''),
                     'CURRICULUM_CONTENT_CHANGED')
        if 'pet_writing' in payload:
            _require(item['answer_type'] == 'writing' and not item['stimulus_text'] and not item['stimulus_asset'],
                     'CURRICULUM_CONTENT_CHANGED')
        if 'objective_source' in payload:
            _require(item['answer_type'] == 'text' and not item['stimulus_text'] and not item['stimulus_asset'],
                     'CURRICULUM_CONTENT_CHANGED')
        if "pet_listening" in checked:
            original = checked["pet_listening"]
            _require(item["answer_type"] == "listening_text" and item["stimulus_text"] == original["stimulus_text"]
                     and not item["stimulus_asset"] and item["prompt"] == original["prompt"]
                     and json.loads(item["expected_answer_json"]) == expected
                     and item["source_ref"] == "parent_approved_specialized:" + candidate["candidate_id"],
                     "CURRICULUM_CONTENT_CHANGED")


class CurriculumSupplyService:
    def __init__(self, database):
        self.database = database

    def ensure_schema(self):
        from .specialized_supply import SpecializedDiagnosticSupplyService
        SpecializedDiagnosticSupplyService(self.database).ensure_schema()
        with self.database.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS curriculum_supply_selection(
                    selection_id TEXT PRIMARY KEY,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    subject TEXT NOT NULL, version INTEGER NOT NULL, scope_json TEXT NOT NULL,
                    actor TEXT NOT NULL, action_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(child_id,subject,version), UNIQUE(child_id,action_id));
                CREATE TABLE IF NOT EXISTS curriculum_supply_package(
                    candidate_id TEXT PRIMARY KEY REFERENCES specialized_supply_candidate(candidate_id) ON DELETE CASCADE,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    author TEXT NOT NULL, payload_json TEXT NOT NULL, source_snapshot_json TEXT NOT NULL,
                    state TEXT NOT NULL, version INTEGER NOT NULL, supersedes TEXT,
                    blockers_json TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS curriculum_supply_event(
                    event_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES curriculum_supply_package(candidate_id) ON DELETE CASCADE,
                    child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                    version INTEGER NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
                    action_id TEXT NOT NULL, request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(candidate_id,version), UNIQUE(child_id,action_id));
            """)

    def select_curriculum(self, child_id, scope, *, actor, action_id, expected_version):
        self.ensure_schema()
        _require(isinstance(scope, dict), "CURRICULUM_INPUT")
        scope = {key: _text(scope.get(key), 100) for key in ("grade", "subject", "edition", "semester", "standard_scope", "textbook_source_id")}
        _require(scope["semester"] in {"上学期", "下学期"}, "CURRICULUM_INPUT")
        _require(type(expected_version) is int and expected_version >= 0, "CURRICULUM_VERSION_CONFLICT")
        actor, action_id = _text(actor, 100), _text(action_id, 200)
        # A completed replay does not change the selected source and must not
        # wait behind an unrelated mail submission.
        with self.database.connect() as c:
            c.execute('BEGIN')
            existing = c.execute("SELECT * FROM curriculum_supply_selection WHERE child_id=? AND action_id=?", (child_id, action_id)).fetchone()
            if existing:
                child = c.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
                _require(child is not None and child[0] == scope["grade"], "CURRICULUM_SCOPE_MISMATCH")
                source = c.execute("SELECT * FROM knowledge_source WHERE source_id=? AND child_id=?",
                                   (scope["textbook_source_id"], child_id)).fetchone()
                _require(source is not None and (source["grade_band"], source["subject"], source["version_label"]) ==
                         (scope["grade"], scope["subject"], scope["edition"]), "CURRICULUM_SCOPE_MISMATCH")
                _require(existing["scope_json"] == _json(scope) and existing["actor"] == actor and existing["version"] == expected_version + 1,
                         "CURRICULUM_ACTION_CONFLICT")
                return {"version": existing["version"], "scope": scope}
        # Appending a newer version replaces the effective selection for all
        # earlier source qualifications. Serialize it with report disclosure.
        with self.database.external_effect_guard(timeout=2), self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            child = c.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
            _require(child is not None and child[0] == scope["grade"], "CURRICULUM_SCOPE_MISMATCH")
            source = c.execute("SELECT * FROM knowledge_source WHERE source_id=? AND child_id=?",
                               (scope["textbook_source_id"], child_id)).fetchone()
            _require(source is not None and (source["grade_band"], source["subject"], source["version_label"]) ==
                     (scope["grade"], scope["subject"], scope["edition"]), "CURRICULUM_SCOPE_MISMATCH")
            existing = c.execute("SELECT * FROM curriculum_supply_selection WHERE child_id=? AND action_id=?", (child_id, action_id)).fetchone()
            if existing:
                _require(existing["scope_json"] == _json(scope) and existing["actor"] == actor and existing["version"] == expected_version + 1,
                         "CURRICULUM_ACTION_CONFLICT")
                return {"version": existing["version"], "scope": scope}
            version = c.execute("SELECT COALESCE(MAX(version),0) FROM curriculum_supply_selection WHERE child_id=? AND subject=?",
                                 (child_id, scope["subject"])).fetchone()[0]
            _require(type(expected_version) is int and version == expected_version, "CURRICULUM_VERSION_CONFLICT")
            c.execute("INSERT INTO curriculum_supply_selection VALUES(?,?,?,?,?,?,?,?)",
                      (str(uuid4()), child_id, scope["subject"], version+1, _json(scope), actor, action_id, _now()))
            c.commit()
        return {"version": version+1, "scope": scope}

    def generate_draft(self, child_id, source_spec, *, author, action_id, expected_scope_version=None,
                       supersedes=None, supersedes_version=None):
        """Extract an existing question into an unscored draft, not a fake variant.

        No model or template invents missing material. The independent validator
        computes the answer later; source/relevance review is still required.
        """
        _require(isinstance(source_spec, dict) and set(source_spec) == {
            "seed_item_id", "stage", "explanation", "difficulty", "excerpt_id", "requirement_id",
            "standard_page", "standard_quote", "validator"},
                 "CURRICULUM_INPUT")
        with self.database.connect() as c:
            excerpt = c.execute("""SELECT ke.content_text FROM knowledge_excerpt ke JOIN knowledge_source ks USING(source_id)
                WHERE ke.excerpt_id=? AND ks.child_id=?""", (source_spec.get("excerpt_id"), child_id)).fetchone()
        from .pet_reviewed_source import VALIDATOR, source_payload
        from . import curriculum_objective_source as objective
        from . import pet_writing_source as writing
        from . import open_response_source as opened
        from . import pet_oral_source as oral
        material = (source_payload(excerpt[0] if excerpt else None) if source_spec["validator"] == VALIDATOR
                    else oral.source_payload(excerpt[0] if excerpt else None) if source_spec['validator'] == oral.VALIDATOR
                    else opened.source_payload(excerpt[0] if excerpt else None) if source_spec['validator'] == opened.VALIDATOR
                    else writing.source_payload(excerpt[0] if excerpt else None) if source_spec['validator'] == writing.VALIDATOR
                    else objective.source_payload(excerpt[0] if excerpt else None) if source_spec['validator'] == objective.VALIDATOR
                    else {"prompt": excerpt[0].strip() if excerpt else "待补充审核题源。", "expected_answer": None})
        return self.draft(child_id, {**source_spec, **material}, author=author, action_id=action_id,
                          expected_scope_version=expected_scope_version,
                          supersedes=supersedes, supersedes_version=supersedes_version)

    def draft(self, child_id, payload, *, author, action_id, supersedes=None, supersedes_version=None, expected_scope_version=None):
        self.ensure_schema()
        author, action_id = _text(author, 100), _text(action_id, 200)
        required = {"seed_item_id", "stage", "prompt", "expected_answer", "explanation", "difficulty", "excerpt_id",
                    "requirement_id", "standard_page", "standard_quote", "validator"}
        from .pet_reviewed_source import VALIDATOR, parse_listening_source, listening_exact_rubric
        from . import curriculum_objective_source as objective
        from . import pet_writing_source as writing
        from . import open_response_source as opened
        from . import pet_oral_source as oral
        pet = isinstance(payload, dict) and payload.get("validator") == VALIDATOR
        textual = isinstance(payload, dict) and payload.get('validator') == objective.VALIDATOR
        written = isinstance(payload, dict) and payload.get('validator') == writing.VALIDATOR
        open_task = isinstance(payload,dict) and payload.get('validator') == opened.VALIDATOR
        oral_task = isinstance(payload, dict) and payload.get('validator') == oral.VALIDATOR
        if oral_task:
            required.add('pet_oral')
        if open_task:
            required.add('open_response')
        if pet:
            required.add("pet_listening")
        if textual:
            required.add('objective_source')
        if written:
            required.add('pet_writing')
        _require(isinstance(payload, dict) and set(payload) == required, "CURRICULUM_INPUT")
        payload = dict(payload)
        for key in required - {"difficulty", "standard_page", "expected_answer", "pet_listening", "objective_source", 'pet_writing', 'open_response', 'pet_oral'}:
            payload[key] = _text(payload[key])
        if pet:
            payload["pet_listening"] = parse_listening_source(_json(payload["pet_listening"]))
            _require(payload["expected_answer"] is None, "CURRICULUM_INPUT")
        if textual:
            payload['objective_source'] = objective.parse(_json(payload['objective_source']))
            _require(payload['expected_answer'] is None, 'CURRICULUM_INPUT')
        if written:
            payload['pet_writing'] = writing.parse(_json(payload['pet_writing']))
            _require(payload['expected_answer'] is None, 'CURRICULUM_INPUT')
        if open_task:
            payload['open_response'] = opened.parse(_json(payload['open_response']))
            _require(payload['expected_answer'] is None, 'CURRICULUM_INPUT')
        if oral_task:
            payload['pet_oral'] = oral.parse(_json(payload['pet_oral']))
            _require(payload['expected_answer'] is None, 'CURRICULUM_INPUT')
        if payload["expected_answer"] is not None:
            payload["expected_answer"] = _text(payload["expected_answer"], 100)
        _require(type(payload["difficulty"]) is int and 1 <= payload["difficulty"] <= 5, "CURRICULUM_INPUT")
        _require(payload["stage"] in {"parallel", "near_transfer", "representation_transfer", "discrimination", "transfer", "far_transfer"}, "CURRICULUM_INPUT")
        request = digest({"kind": "draft", "payload": payload, "author": author, "supersedes": supersedes,
                          "supersedes_version": supersedes_version, "expected_scope_version": expected_scope_version})
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM curriculum_supply_event WHERE child_id=? AND action_id=?", (child_id, action_id)).fetchone()
            if existing:
                _require(existing["request_hash"] == request, "CURRICULUM_ACTION_CONFLICT")
                return self._get(c, child_id, existing["candidate_id"])
            seed = validate_item(c, child_id, payload["seed_item_id"])
            node = dict(c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=?", (seed["knowledge_node_id"],)).fetchone())
            if expected_scope_version is not None:
                selected = _selection(c, child_id, node["subject"])
                _require(type(expected_scope_version) is int and selected["version"] == expected_scope_version,
                         "CURRICULUM_SCOPE_VERSION_CONFLICT")
            snapshot, blockers = {}, []
            try:
                snapshot = _evidence(c, child_id, payload)
            except LocalSupplyError as error:
                blockers = [str(error)]
            if supersedes:
                old = self._row(c, child_id, supersedes)
                _require(type(supersedes_version) is int and old["version"] == supersedes_version,
                         "CURRICULUM_VERSION_CONFLICT")
                _require(not c.execute("SELECT 1 FROM curriculum_supply_package WHERE supersedes=?", (supersedes,)).fetchone(),
                         "CURRICULUM_REVISION_CONFLICT")
                self._invalidate(c, old)
                self._event(c, old, "withdraw", author, "supersede:"+action_id, digest([request, supersedes]),
                            {"state": "withdrawn", "blockers": [], "note": "superseded by a new draft"})
            # A prompt alias is never another independent assessment opportunity.
            ancestors, ancestor = set(), supersedes
            while ancestor and ancestor not in ancestors:
                ancestors.add(ancestor)
                ancestor = self._row(c, child_id, ancestor)["supersedes"]
            from .learning_obligations import question_identity
            for prior in c.execute("SELECT candidate_id,prompt FROM specialized_supply_candidate WHERE child_id=?", (child_id,)):
                if _question_key(prior["prompt"]) == _question_key(payload["prompt"]):
                    _require(prior["candidate_id"] in ancestors and
                             question_identity({"prompt": prior["prompt"]}) == question_identity(payload),
                             "CURRICULUM_DUPLICATE_QUESTION")
            cid, job_id, now = str(uuid4()), str(uuid4()), _now()
            track = "junior_exam" if node["track"] == "school" and node["grade_band"] == "初三" else node["track"]
            from .specialized_program import SpecializedProgramService
            domain = SpecializedProgramService._mastery_domain(track, node) or node["knowledge_node_id"]
            c.execute("""INSERT INTO specialized_supply_job(job_id,child_id,track,subject,domain_id,seed_item_id,
                stages_json,status,generation_mode,source_snapshot_json,created_at,updated_at,client_action_id)
                VALUES(?,?,?,?,?,?,?,'completed','curriculum_review',?,?,?,?)""",
                (job_id, child_id, track, node["subject"], domain, payload["seed_item_id"], _json([payload["stage"]]),
                 _json(item_snapshot(seed)), now, now, "curriculum:"+cid))
            # Revision identity is separate from canonical question identity; selectors
            # still see the original prompt for their existing alias evidence rules.
            fingerprint = digest([child_id, payload["prompt"].strip()]) if not supersedes else digest([child_id, payload["prompt"].strip(), cid])
            c.execute("""INSERT INTO specialized_supply_candidate(candidate_id,job_id,child_id,track,subject,
                domain_id,seed_item_id,stage,prompt,expected_answer_json,explanation,difficulty,
                content_fingerprint,validation_json,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'needs_review',?)""",
                (cid, job_id, child_id, track, node["subject"], domain, payload["seed_item_id"], payload["stage"],
                 payload["prompt"], _json(listening_exact_rubric(payload["pet_listening"]) if pet else
                                          oral.rubric(payload['pet_oral']) if oral_task else
                                          opened.rubric(payload['open_response']) if open_task else
                                          writing.rubric(payload['pet_writing']) if written else
                                          objective.exact_rubric(payload['objective_source']) if textual else
                                          {"local_rule": "local_exact_v1", "exact_answers": [payload["expected_answer"]]}),
                 payload["explanation"], payload["difficulty"], fingerprint,
                 _json({"passed": False, "ruleset": "curriculum_review_v1"}), now))
            c.execute("INSERT INTO curriculum_supply_package VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (cid, child_id, author, _json(payload), _json(snapshot), "draft", 0, supersedes, "[]", now))
            row = self._row(c, child_id, cid)
            self._event(c, row, "draft", author, action_id, request,
                        {"state": "blocked" if blockers else "draft", "blockers": blockers})
            c.commit()
            return self._get(c, child_id, cid)

    def validate(self, child_id, candidate_id, *, actor, action_id, expected_version):
        return self._transition(child_id, candidate_id, "validate", actor, action_id, expected_version)

    def review(self, child_id, candidate_id, *, decision, actor, note, action_id, expected_version, stage_review=None):
        _require(decision in {"approve", "reject"}, "CURRICULUM_INPUT")
        _require(stage_review is None or decision == 'approve', 'CURRICULUM_STAGE_REVIEW_REJECTED')
        return self._transition(child_id, candidate_id, "review", actor, action_id, expected_version,
                                {"decision": decision, "note": _text(note, 500),
                                 **({'stage_review': stage_review} if stage_review is not None else {})})

    def publish(self, child_id, candidate_id, *, actor, action_id, expected_version):
        return self._transition(child_id, candidate_id, "publish", actor, action_id, expected_version)

    def withdraw(self, child_id, candidate_id, *, actor, note, action_id, expected_version):
        # First-source transitions own their guard internally, including when
        # called directly; do not nest a borrowed transaction inside it.
        if candidate_id.startswith('first-'):
            return self._transition(child_id, candidate_id, "withdraw", actor, action_id, expected_version, {"note": _text(note, 500)})
        with self.database.external_effect_guard(timeout=2):
            return self._transition(child_id, candidate_id, "withdraw", actor, action_id, expected_version, {"note": _text(note, 500)})

    def _transition(self, child_id, cid, kind, actor, action_id, expected_version, values=None):
        if cid.startswith('first-'):
            from .curriculum_first_source import CurriculumFirstSourceService, VALIDATOR
            return CurriculumFirstSourceService(self.database).transition(child_id, cid, kind,
                actor=VALIDATOR if kind == 'validate' and actor.startswith('local:') else actor,
                action_id=action_id, expected_version=expected_version, values=values)
        self.ensure_schema()
        actor, action_id = _text(actor, 100), _text(action_id, 200)
        values = values or {}
        request = digest([cid, kind, actor, expected_version, values])
        with self.database.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self._row(c, child_id, cid)
            existing = c.execute("SELECT * FROM curriculum_supply_event WHERE child_id=? AND action_id=?", (child_id, action_id)).fetchone()
            if existing:
                _require(existing["request_hash"] == request, "CURRICULUM_ACTION_CONFLICT")
                return self._get(c, child_id, cid)
            _require(type(expected_version) is int and row["version"] == expected_version, "CURRICULUM_VERSION_CONFLICT")
            payload = json.loads(row["payload_json"])
            result = {**values, "state": row["state"], "blockers": []}
            if kind == "withdraw":
                _require(row["state"] != "withdrawn", "CURRICULUM_STATE_CONFLICT")
                self._invalidate(c, row)
                result["state"] = "withdrawn"
            elif kind == "review" and values["decision"] == "reject":
                _require(row["state"] not in {"withdrawn", "published", "rejected"}, "CURRICULUM_STATE_CONFLICT")
                result["state"] = "rejected"
                self._invalidate(c, row)
            else:
                allowed = {"validate": {"draft", "blocked"}, "review": {"validated"}, "publish": {"reviewed"}}
                _require(row["state"] in allowed[kind], "CURRICULUM_STATE_CONFLICT")
                if kind in {"validate", "review"}:
                    _require(actor != row["author"], "CURRICULUM_INDEPENDENT_REVIEW_REQUIRED")
                if kind == 'review' and 'pet_writing' in payload and payload['stage'] in {'near_transfer','far_transfer'}:
                    _require(isinstance(values.get('stage_review'), dict)
                             and values['stage_review'].get('stage') == payload['stage'], 'CURRICULUM_WRITING_STAGE_REVIEW_REQUIRED')
                if kind == 'review' and 'open_response' in payload and payload['stage'] in {'near_transfer','far_transfer'}:
                    _require(isinstance(values.get('stage_review'),dict)
                             and values['stage_review'].get('stage') == payload['stage'], 'CURRICULUM_OPEN_STAGE_REVIEW_REQUIRED')
                try:
                    proof = _evidence(c, child_id, payload, candidate_id=cid)
                    _require(_source_snapshot_matches(proof, json.loads(row["source_snapshot_json"])), "CURRICULUM_SOURCE_CHANGED")
                    check = _check(payload, proof["seed"], connection=c, child_id=child_id)
                    if kind == 'review' and values.get('stage_review') is not None:
                        from .stage_source_review import validate_stage_review
                        validate_stage_review(c, child_id, cid, values['stage_review'])
                        candidate = c.execute('SELECT validation_json FROM specialized_supply_candidate WHERE candidate_id=?', (cid,)).fetchone()
                        validation = {**json.loads(candidate['validation_json']), 'stage_review': values['stage_review']}
                        c.execute('UPDATE specialized_supply_candidate SET validation_json=? WHERE candidate_id=?', (_json(validation), cid))
                    result["check"] = check
                    result["state"] = {"validate": "validated", "review": "reviewed", "publish": "published"}[kind]
                except LocalSupplyError as error:
                    result.update(state="blocked", blockers=[str(error)])
                if result["state"] == "validated":
                    c.execute("UPDATE specialized_supply_candidate SET expected_answer_json=?,validation_json=? WHERE candidate_id=?",
                              (_json(check.get("expected_answer", {"local_rule": "local_exact_v1", "exact_answers": [check["computed_answer"]]})),
                               _json({"passed": True, "ruleset": "curriculum_review_v1"}), cid))
            self._event(c, row, kind, actor, action_id, request, result)
            if result["state"] == "published":
                from .specialized_supply import promote_candidate
                candidate = c.execute("SELECT * FROM specialized_supply_candidate WHERE candidate_id=?", (cid,)).fetchone()
                validate_curriculum_candidate(c, candidate)
                item_id = promote_candidate(c, candidate)
                c.execute("""UPDATE specialized_supply_candidate SET status='promoted',diagnostic_item_id=?,
                    reviewed_at=?,review_note=?,review_action_id=? WHERE candidate_id=?""",
                    (item_id, _now(), "curriculum independent check and review", "curriculum-publish:"+cid, cid))
            c.commit()
            return self._get(c, child_id, cid)

    @staticmethod
    def _invalidate(c, row):
        c.execute("""UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id IN
            (SELECT diagnostic_item_id FROM specialized_supply_candidate WHERE candidate_id=?)""", (row["candidate_id"],))
        c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE candidate_id=?", (row["candidate_id"],))

    @staticmethod
    def _row(c, child_id, cid):
        row = c.execute("SELECT * FROM curriculum_supply_package WHERE candidate_id=? AND child_id=?", (cid, child_id)).fetchone()
        if row is None:
            raise KeyError(cid)
        return dict(row)

    @staticmethod
    def _event(c, row, kind, actor, action_id, request, result):
        c.execute("INSERT INTO curriculum_supply_event VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (str(uuid4()), row["candidate_id"], row["child_id"], row["version"]+1, kind, actor, action_id, request, _json(result), _now()))
        c.execute("UPDATE curriculum_supply_package SET state=?,version=version+1,blockers_json=? WHERE candidate_id=?",
                  (result["state"], _json(result["blockers"]), row["candidate_id"]))
        blocked = result["state"] in {"blocked", "withdrawn", "rejected"}
        reason = next(iter(result["blockers"]), "CURRICULUM_"+result["state"].upper()) if blocked else None
        c.execute("""UPDATE specialized_supply_job SET status=?,error_code=?,updated_at=?
            WHERE job_id=(SELECT job_id FROM specialized_supply_candidate WHERE candidate_id=?)""",
            ("blocked" if blocked else "completed", reason, _now(), row["candidate_id"]))
        if result['state']=='withdrawn':
            from .oral_daily_withdrawal import record
            event = c.execute('SELECT event_id FROM curriculum_supply_event WHERE child_id=? AND action_id=?',
                (row['child_id'], action_id)).fetchone()
            record(c, row['child_id'], row['candidate_id'], event['event_id'])

    def audit(self, child_id):
        self.ensure_schema()
        from .curriculum_first_source import CurriculumFirstSourceService
        first = CurriculumFirstSourceService(self.database)
        first.initialize()
        with self.database.connect() as c:
            # Material validators borrow this read snapshot instead of rolling
            # back between packets and invalidating the active audit cursor.
            c.execute('BEGIN')
            _require(c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone(),
                     "CURRICULUM_CHILD_UNAVAILABLE")
            counts = {row[0] or "UNSPECIFIED_BLOCKER": row[1] for row in c.execute("""SELECT error_code,COUNT(*) FROM specialized_supply_job
                WHERE child_id=? AND status='blocked' GROUP BY error_code""", (child_id,))}
            packages = [self._get(c, child_id, row[0]) for row in c.execute(
                "SELECT candidate_id FROM curriculum_supply_package WHERE child_id=? ORDER BY created_at,candidate_id", (child_id,))]
            packages.extend(first.get_in(c, child_id, row[0]) for row in c.execute(
                'SELECT candidate_id FROM curriculum_first_source WHERE child_id=? ORDER BY created_at,candidate_id', (child_id,)).fetchall())
            selections = [{"version": row["version"], "scope": json.loads(row["scope_json"])} for row in c.execute(
                """SELECT s.* FROM curriculum_supply_selection s WHERE child_id=? AND version=(
                    SELECT MAX(version) FROM curriculum_supply_selection other
                    WHERE other.child_id=s.child_id AND other.subject=s.subject) ORDER BY subject""", (child_id,))]
        eligible = [p for p in packages if p["eligible"]]
        return {"child_id": child_id, "current_curriculum": selections, "blocked_job_counts": counts,
                "eligible_curriculum_items": len(eligible), "qualified_stages": sorted({p["payload"]["stage"] for p in eligible}),
                "packages": [{**{key: p[key] for key in ("candidate_id", "state", "version", "eligible", "blockers", "next_action")},
                              "prompt": p["payload"]["prompt"][:160]} for p in packages],
                "thinking_or_transfer_coverage_claim_allowed": False, "cloud_transmission_allowed": False}

    def get(self, child_id, candidate_id):
        if candidate_id.startswith('first-'):
            from .curriculum_first_source import CurriculumFirstSourceService
            return CurriculumFirstSourceService(self.database).get(child_id, candidate_id)
        self.ensure_schema()
        with self.database.connect() as c:
            return self._get(c, child_id, candidate_id)

    def _get(self, c, child_id, cid):
        row = self._row(c, child_id, cid)
        candidate = c.execute("SELECT * FROM specialized_supply_candidate WHERE candidate_id=?", (cid,)).fetchone()
        blockers, eligible = json.loads(row["blockers_json"]), False
        snapshot = json.loads(row["source_snapshot_json"])
        packet = None
        if snapshot:
            packet = {"fingerprint": digest(snapshot), "scope": json.loads(snapshot["selection"]["scope_json"]),
                      "textbook": {key: snapshot["source"][key] for key in (
                          "source_id", "title", "source_ref", "version_label", "review_status", "excerpt_id", "content_text")},
                      "standard": {"source_id": snapshot["standard"]["source_id"],
                                   "official_url": snapshot["standard"]["official_url"],
                                   "page_number": snapshot["page"]["page_number"],
                                   "requirement_text": snapshot["requirement"]["requirement_text"],
                                   "quote": json.loads(row["payload_json"])["standard_quote"]},
                      "review_scope": "current_question_source_node_and_parallel_stage_not_child_mastery"}
            payload = json.loads(row["payload_json"])
            if "pet_listening" in payload:
                packet["pet_listening"] = payload["pet_listening"]
            if 'pet_writing' in payload:
                packet['pet_writing'] = payload['pet_writing']
            if 'pet_oral' in payload:
                packet['pet_oral'] = payload['pet_oral']
            if 'open_response' in payload:
                packet['open_response'] = payload['open_response']
                if payload['open_response'].get('schema') == 'reviewed_open_response_source_v2':
                    from .open_response_materials import parent_source_materials
                    packet['current_materials'] = parent_source_materials(c, child_id=child_id,
                        source=payload['open_response'], candidate_id=cid)
        if row["state"] == "published":
            try:
                validate_item(c, child_id, candidate["diagnostic_item_id"])
                eligible = True
            except LocalSupplyError as error:
                blockers = [str(error)]
        stage_context = None
        if row['state'] in {'validated', 'reviewed', 'published'}:
            from .stage_source_review import stage_review_context
            try:
                stage_context = stage_review_context(c, child_id, cid)
            except LocalSupplyError:
                pass
        return {"candidate_id": cid, "child_id": child_id, "version": row["version"], "state": row["state"],
                "eligible": eligible, "blockers": blockers, "supersedes": row["supersedes"],
                "review_packet": packet,
                "stage_review_context": stage_context,
                "diagnostic_item_id": candidate["diagnostic_item_id"], "payload": json.loads(row["payload_json"]),
                "next_action": "revise_sources_or_question" if blockers else {
                    "draft": "independent_validate", "validated": "review", "reviewed": "publish",
                    "published": "plan_selection", "withdrawn": "new_version", "rejected": "new_version"}.get(row["state"], "revise_sources_or_question"),
                "events": [{**{key: event[key] for key in ("version", "kind", "actor", "created_at")},
                            "result": json.loads(event["result_json"])} for event in c.execute(
                                "SELECT * FROM curriculum_supply_event WHERE candidate_id=? ORDER BY version", (cid,))]}
