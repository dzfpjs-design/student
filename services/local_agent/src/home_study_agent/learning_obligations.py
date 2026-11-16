"""Small transactional helpers for withdrawing source-derived learning work."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from uuid import uuid4


MASTERY_REPLAY_VERSION = "mastery_evidence_replay_v3"
QUESTION_IDENTITY_VERSION = "exact_question_v1"
NEW_TRANSFER_STAGES = frozenset({'near_transfer', 'representation_transfer', 'discrimination', 'far_transfer'})


def question_identity(item: Mapping, *, connection=None) -> str:
    """Exact visible-content identity, not semantic or answer equivalence."""
    value = dict(item)
    from .visual_question_evidence import frozen_visual
    visual = frozen_visual(connection, value)
    fields = {}
    for key in ("prompt", "stimulus_text", "stimulus_asset"):
        text = value.get(key)
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError("question content must be text")
        fields[key] = " ".join(text.split()) if key != "stimulus_asset" else text
    if not fields["prompt"]:
        raise ValueError("question prompt is required")
    version = QUESTION_IDENTITY_VERSION
    if visual is not None:
        from .visual_question_evidence import _digest
        fields = {'prompt': fields['prompt'], 'visual_content': _digest(visual['visible'])}
        version = 'exact_visual_question_v1'
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode()).hexdigest()
    return version + ":" + digest


def _source_question(connection, item, *, _lookups=None):
    query = connection.execute if _lookups is None else _lookups.execute
    expected = item.get('expected_answer_json')
    expected = json.loads(expected) if isinstance(expected, str) else expected
    if isinstance(expected, dict) and expected.get('material_schema') == 'reviewed_open_response_source_v3':
        from .visual_question_evidence import frozen_visual
        visual = frozen_visual(connection, item)
        if visual is None:
            raise ValueError('reviewed visual identity unavailable')
        return {**item, 'stimulus_text': visual['stimulus_text'], 'stimulus_asset': None}
    source = item.get("source_ref") or ""
    if source.startswith('parent_approved_first:'):
        from .curriculum_first_source import source_question
        return source_question(connection, item)
    if source.startswith("hsc_original"):
        from .learning_state import ITEMS, SPECIALIZED_ITEMS, EXTENDED_ITEMS
        original = next((r for r in (*ITEMS, *SPECIALIZED_ITEMS, *EXTENDED_ITEMS)
                         if r[0] == item["diagnostic_item_id"]), None)
        if original:
            from .specialized_catalog import SCHOOL_LISTENING_STIMULI
            stimulus = SCHOOL_LISTENING_STIMULI.get(item['diagnostic_item_id'], original[7] if len(original) > 7 else None)
            return {"prompt": original[2], "stimulus_text": stimulus,
                    "stimulus_asset": original[8] if len(original) > 8 else None}
    elif source.startswith(("parent_approved_", "local_verified_")):
        variant = source.startswith(("parent_approved_variant:", "local_verified_variant:"))
        table = "variant_candidate" if variant else "specialized_supply_candidate"
        if query("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            row = query(f"SELECT prompt FROM {table} WHERE candidate_id=? AND diagnostic_item_id=?",
                (source.partition(":")[2], item["diagnostic_item_id"])).fetchone()
            if row:
                if not variant and query("SELECT 1 FROM sqlite_master WHERE name='curriculum_supply_package'").fetchone():
                    package = query('SELECT payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?',
                        (source.partition(':')[2],)).fetchone()
                    if package:
                        payload, proof = json.loads(package[0]), json.loads(package[1])
                        from .pet_reviewed_source import VALIDATOR, parse_listening_source
                        if isinstance(payload, dict) and ('pet_listening' in payload or payload.get('validator') == VALIDATOR):
                            frozen = proof.get('source') if isinstance(proof, dict) else None
                            text = frozen.get('content_text') if isinstance(frozen, dict) else None
                            if (payload.get('validator') != VALIDATOR or not isinstance(text, str)
                                    or frozen.get('excerpt_hash') != hashlib.sha256(text.encode('utf-8')).hexdigest()
                                    or not frozen.get('excerpt_id') or frozen['excerpt_id'] != payload.get('excerpt_id')):
                                raise ValueError('reviewed listening material identity unavailable')
                            original = parse_listening_source(text)
                            if (payload.get('pet_listening') != original or row[0] != original['prompt']
                                    or payload.get('prompt') != original['prompt']):
                                raise ValueError('reviewed listening material identity unavailable')
                            return {'prompt': original['prompt'], 'stimulus_text': original['stimulus_text'],
                                    'stimulus_asset': None}
                        if isinstance(payload, dict) and 'pet_oral' in payload:
                            from .pet_oral_source import parse
                            original = parse(json.dumps(payload['pet_oral']))
                            materials = proof.get('oral_materials') if isinstance(proof, dict) else None
                            expected = {
                                'stimulus_text': original['stimulus_text'],
                                'stimulus_asset': (original['stimulus_binding'] or {}).get('stimulus_asset'),
                            }
                            if (row[0] != original['prompt'] or not isinstance(materials, dict)
                                    or any(key not in materials or materials[key] != value
                                           for key, value in expected.items())):
                                raise ValueError('reviewed oral material identity unavailable')
                            return {'prompt': row[0], **expected}
                        original = payload.get('open_response') if isinstance(payload, dict) else None
                        if isinstance(original, dict) and original.get('schema') == 'reviewed_open_response_source_v3':
                            from .visual_question_evidence import frozen_visual
                            visual = frozen_visual(connection, item)
                            if visual is None:
                                raise ValueError('reviewed visual identity unavailable')
                            return {**item, 'prompt': row[0], 'stimulus_text': visual['stimulus_text'], 'stimulus_asset': None}
                        if isinstance(original, dict) and original.get('schema') == 'reviewed_open_response_source_v2':
                            materials = proof.get('open_response_materials') if isinstance(proof, dict) else None
                            if not isinstance(materials, dict) or not isinstance(materials.get('stimulus_text'), str) or not materials['stimulus_text']:
                                raise ValueError('reviewed material identity unavailable')
                            return {'prompt': row[0], 'stimulus_text': materials['stimulus_text'], 'stimulus_asset': None}
                return {"prompt": row[0], "stimulus_text": None, "stimulus_asset": None}
    return None


def _question_catalog(connection):
    from .question_source_rows import QuestionSourceRows
    items, identities = {}, {}
    rows = [dict(raw) for raw in connection.execute("SELECT * FROM diagnostic_item")]
    lookups = QuestionSourceRows(connection, rows)
    for item in rows:
        items[item["diagnostic_item_id"]] = item
        try:
            identities[item["diagnostic_item_id"]] = question_identity(
                _source_question(connection, item, _lookups=lookups) or item, connection=connection)
        except (TypeError, ValueError):
            pass
    lookups.current()
    return items, identities


def _evidence_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("evidence time must have a timezone")
    return parsed.astimezone(timezone.utc)


def had_recent_item_exposure(connection, child_id: str, item_id: str, now: str) -> bool:
    exposure = project_item_exposure(connection, child_id, item_id, as_of=now)
    return exposure["answer_repeated"] or exposure["tutor_assisted"]


def _typed_tutor_identity(connection, turn, occurred, identities, cache):
    # Only the host's persisted local audit can identify a material question.
    # Do not resolve a current task, node or free-form model field on this read.
    try:
        envelope = json.loads(turn['output_envelope_json'] or '{}')
        control = envelope.get('local_teaching_control') if isinstance(envelope, dict) else None
        target = control.get('question_target') if isinstance(control, dict) else None
        binding = target.get('binding') if isinstance(target, dict) else None
        if not isinstance(binding, dict):
            return None
        from .teaching_question_target import VERSION, _digest, _hash, _question
        if (target.get('version') != VERSION or target.get('status') != 'resolved'
                or target.get('fingerprint') != _digest(target)
                or target.get('child_id') != turn['child_id']
                or target.get('session_id') != turn['session_id']
                or target.get('subject') != turn['subject']
                or target.get('question_hash') != _hash(_question(turn['question']))):
            return None
        item_id, prompt_id, version = (binding.get(k) for k in
            ('diagnostic_item_id', 'prompt_id', 'item_version'))
        if not all(isinstance(value, str) and value for value in (item_id, prompt_id, version)) or item_id not in identities:
            return None
        if prompt_id not in cache:
            present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnostic_prompt'").fetchone()
            cache[prompt_id] = connection.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
                (prompt_id,)).fetchone() if present else None
        prompt = cache[prompt_id]
        if (not prompt or prompt['child_id'] != turn['child_id'] or prompt['origin_session_id'] != turn['session_id']
                or prompt['diagnostic_item_id'] != item_id or prompt['item_version'] != version
                or binding.get('review_task_id') != prompt['review_task_id']
                or binding.get('ocr_confirmation_id') != turn['ocr_confirmation_id']
                or _evidence_time(prompt['created_at']) > occurred):
            return None
        if target.get('expires_at') is not None and occurred >= _evidence_time(target['expires_at']):
            return None
        from .diagnostic_prompt import _ITEM_FIELDS, _version
        item = connection.execute('''SELECT di.*,kn.track,kn.subject,kn.title,kn.grade_band,kn.knowledge_version,
            kn.source_ref AS node_source_ref FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id)
            WHERE diagnostic_item_id=?''', (item_id,)).fetchone()
        if (not item or _version(dict(item)) != version
                or json.loads(prompt['item_json']) != {key: item[key] for key in _ITEM_FIELDS}
                or item['subject'] != turn['subject']
                or target.get('knowledge_node_id') != item['knowledge_node_id']
                or _question(item['prompt']) != _question(turn['question'])
                or question_identity(dict(item), connection=connection) != identities[item_id]):
            return None
        return identities[item_id]
    except (ValueError, TypeError, KeyError):
        return None


def _tutor_exposure_index(connection, child_id, catalog, identities):
    exact, bound = {}, {}
    typed_cache = {}
    turns = connection.execute("""SELECT t.turn_id,t.question,t.created_at,t.child_id,t.session_id,t.subject,
            t.safety_status,t.output_envelope_json,t.ocr_confirmation_id,rt.diagnostic_item_id AS bound_item_id,
            rt.knowledge_node_id AS bound_node,
            CASE WHEN dp.child_id=t.child_id AND dp.session_id=t.session_id AND rt.child_id=t.child_id
              THEN 1 ELSE 0 END AS bound_item
        FROM tutor_turn t JOIN daily_session ds ON ds.session_id=t.session_id AND ds.child_id=t.child_id
        LEFT JOIN plan_item pi ON pi.plan_item_id=t.plan_item_id AND pi.source_type='due_review'
        LEFT JOIN daily_plan dp ON dp.plan_id=pi.plan_id
        LEFT JOIN review_task rt ON rt.review_task_id=pi.source_ref
        WHERE t.child_id=? AND t.status='completed' AND t.safety_status IN ('passed','answer_leak_replaced')
          AND t.response_kind IN ('hint','full_explanation') AND t.answer IS NOT NULL AND trim(t.answer)<>''
        ORDER BY julianday(t.created_at),t.rowid""", (child_id,)).fetchall()
    for position, turn in enumerate(turns):
        try:
            occurred = _evidence_time(turn["created_at"])
        except (TypeError, ValueError):
            continue
        entry = (position, occurred, dict(turn))
        if turn['safety_status'] == 'passed':
            exact.setdefault(" ".join(turn["question"].split()), []).append(entry)
        item_id = turn["bound_item_id"]
        if (turn['safety_status'] == 'passed' and turn["bound_item"] and item_id in identities
                and catalog[item_id]["knowledge_node_id"] == turn["bound_node"]):
            bound.setdefault(identities[item_id], []).append(entry)
        identity = _typed_tutor_identity(connection, turn, occurred, identities, typed_cache)
        if identity:
            bound.setdefault(identity, []).append((position, occurred,
                {**dict(turn), 'typed_question_reference': True}))
    return exact, bound


def _tutor_refs(catalog, identities, tutor_index, item_id, now, *, window=timedelta(hours=24)):
    item = catalog.get(item_id)
    if not item:
        return []
    exact, bound = tutor_index
    matches = {}
    prompt = " ".join(item["prompt"].split())
    if prompt and not item["stimulus_text"] and not item["stimulus_asset"]:
        for position, occurred, turn in exact.get(prompt, []):
            matches[position] = (occurred, turn, "exact_question")
    for position, occurred, turn in bound.get(identities.get(item_id), []):
        matches[position] = (occurred, turn, "exact_persisted_question_reference"
            if turn.get('typed_question_reference') else "bound_review_item")
    return [{"turn_id": turn["turn_id"], "occurred_at": turn["created_at"], "matched_by": matched_by}
            for _, (occurred, turn, matched_by) in sorted(matches.items())
            if occurred <= now and (window is None or now - window < occurred)]


def tutor_question_history(connection, child_id, catalog, identities, *, as_of):
    """Earliest exact teaching exposure; new transfer and spaced reuse differ.

    Reuse the typed/material matching rules, without the short assistance
    window. A whole knowledge node or an unbound material prompt is not a match.
    """
    now = _evidence_time(as_of)
    index = _tutor_exposure_index(connection, child_id, catalog, identities)
    first = {}
    for item_id, identity in identities.items():
        for ref in _tutor_refs(catalog, identities, index, item_id, now, window=None):
            entry = (_evidence_time(ref['occurred_at']), 'tutor:' + ref['turn_id'])
            if identity not in first or entry < first[identity]:
                first[identity] = entry
    return first


def project_item_exposure(connection, child_id: str, item_id: str, *, as_of: str) -> dict:
    """Separate answer repetition from same-question assistance; never match a whole node."""
    return project_item_exposures(connection, child_id, item_id, as_of=(as_of,))[0]


def diagnostic_media_scope(connection, child_id: str, item_id: str, evidence_id: str | None, *, as_of=None) -> dict:
    if not evidence_id or not connection.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone():
        return dict(evidence_id=evidence_id, as_of=as_of) if evidence_id else {}
    rows = connection.execute("""SELECT p.prompt_id,p.item_version,p.origin_session_id,a.attempt_id
        FROM diagnostic_prompt p JOIN diagnostic_attempt a ON a.attempt_id=p.attempt_id AND a.child_id=p.child_id
        AND a.session_id=p.origin_session_id AND a.diagnostic_item_id=p.diagnostic_item_id
        WHERE p.child_id=? AND p.diagnostic_item_id=? AND a.evidence_id=? AND p.status='answered'""",
        (child_id, item_id, evidence_id)).fetchall()
    if len(rows) != 1:
        # A missing historical origin must not become an unscoped scoring query.
        return dict(evidence_id=evidence_id, as_of=as_of)
    row = rows[0]
    return dict(session_id=row['origin_session_id'], prompt_id=row['prompt_id'], item_version=row['item_version'],
                attempt_id=row['attempt_id'], evidence_id=evidence_id, as_of=as_of)


def qualified_exact_score(connection, child_id: str, item_id: str, answer: str, *, qualification_context=None, **scope):
    """Same source/score authority as learning_feedback; no keyword certification."""
    from .local_supply import LocalSupplyError, score_local_item, validate_item
    from .pet_skill_gate import pet_skill_gate
    from .database import local_media_scope
    try:
        item = validate_item(connection, child_id, item_id, **scope)
        context = {**(qualification_context or {}), **local_media_scope(connection)}
        modality = pet_skill_gate(connection, child_id, item_id, answer=answer, **context)
        if not modality["skill_scoring_allowed"]:
            return None
        if modality['modality'] == 'listening':
            from .pet_listening_scoring import qualified_listening_score
            return qualified_listening_score(connection, child_id, item_id, answer, **context)
        reference = _source_question(connection, item)
        if not reference or any((item.get(key) or "") != (reference.get(key) or "")
                                for key in ("stimulus_text", "stimulus_asset")):
            return None
        rubric = json.loads(item["expected_answer_json"])
        if (rubric.get("local_rule") != "local_exact_v1" or rubric.get("keyword_groups")
                or rubric.get("task_points") or item.get("assessment_mode") == "pet_reference_rubric"):
            return None
        if rubric.get('answer_format') == 'reviewed_spoken_numeric_v1' and context.get('evidence_id') and not context.get('prompt_id'):
            return None
        if rubric.get('answer_format') == 'reviewed_spoken_numeric_v1' and context.get('prompt_id'):
            origin = connection.execute('SELECT authorized_turn_id FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND diagnostic_item_id=? AND origin_session_id=? AND item_version=?',
                (context['prompt_id'], child_id, item_id, context.get('session_id'), context.get('item_version'))).fetchone()
            if origin is None:
                return None
            if origin['authorized_turn_id']:
                from .historical_answer_source import validate_historical_diagnostic_answer
                validate_historical_diagnostic_answer(connection, origin['authorized_turn_id'],
                    child_id=child_id, session_id=context['session_id'],
                    prompt_id=context['prompt_id'], item_version=context['item_version'], item_id=item_id, answer=answer)
        score = score_local_item(item, answer)
        return (item, score[0]) if score is not None else None
    except (LocalSupplyError, ValueError, TypeError, KeyError):
        return None


def project_mastery_evidence(connection, child_id: str, knowledge_node_id: str, *, as_of: str | None = None) -> dict:
    """Compatibility wrapper. Multi-node readers should use the child batch once."""
    result = project_child_mastery_evidence(connection, child_id, as_of=as_of)
    return result.get(knowledge_node_id) or _empty_mastery_projection(child_id, knowledge_node_id)


def _empty_mastery_projection(child_id, knowledge_node_id):
    return _node_projection(child_id, knowledge_node_id, [], [], [], {}, [])


def project_child_mastery_evidence(connection, child_id: str, *, as_of: str | None = None) -> dict[str, dict]:
    """Read-only node_id -> projection, with all qualification shared for this call only.

    No state or cache survives the call. Rejected-only nodes are included; nodes
    without any evidence are omitted. Existing history and fingerprints are preserved.
    """
    now = _evidence_time(as_of) if as_of else datetime.now(timezone.utc)
    withdrawn = withdrawn_evidence_ids(connection, child_id)
    from .learning_evidence_withdrawal import replacement_map, terminal_replacement
    replacements = replacement_map(connection, child_id)
    # Resolve aliases across this child's nodes before selecting the requested node.
    rows = connection.execute("SELECT * FROM learning_evidence WHERE child_id=? ORDER BY rowid", (child_id,)).fetchall()
    catalog, identities = _question_catalog(connection)
    tutor_index = _tutor_exposure_index(connection, child_id, catalog, identities)
    effective, excluded, basis = [], [], []
    candidates, exposures = {}, []
    for position, raw in enumerate(rows):
        row = dict(raw)
        row_node = row["knowledge_node_id"]
        evidence_id = row["evidence_id"]
        receipts = [dict(r) for r in connection.execute("SELECT * FROM diagnostic_attempt WHERE evidence_id=?", (evidence_id,))]
        mistakes = [dict(r) for r in connection.execute("SELECT * FROM mistake_attempt WHERE evidence_id=?", (evidence_id,))]
        source = {"evidence": row, "diagnostic_receipts": receipts, "mistake_receipts": mistakes}
        basis.append(source)
        reason = None
        receipt = (receipts or mistakes or [None])[0]
        source["question_identity"] = identities.get(receipt["diagnostic_item_id"]) if receipt else None
        try:
            occurred = _evidence_time(row["occurred_at"])
        except (TypeError, ValueError):
            occurred = None
        # Withdrawal removes score authority, not the fact that feedback was seen.
        if (occurred and occurred <= now and receipt and len(receipts) + len(mistakes) == 1
                and receipt["child_id"] == child_id):
            try:
                if _evidence_time(receipt["created_at"]) == occurred:
                    exposures.append((occurred, position, row, receipt))
            except (TypeError, ValueError):
                pass
        if evidence_id in withdrawn:
            reason = "withdrawn"
        elif not occurred or occurred > now:
            reason = "invalid_or_future_timestamp"
        elif len(receipts) + len(mistakes) != 1:
            reason = "missing_or_ambiguous_receipt"
        elif (receipt["child_id"] != child_id or row["independent"] not in (0, 1)
              or type(row["hint_level"]) is not int or not 0 <= row["hint_level"] <= 5):
            reason = "ownership_or_conditions_mismatch"
        elif (row["correctness"] not in (0, 1) or row["correctness"] != receipt["correctness"]
              or not isinstance(row["confidence"], (int, float))
              or not math.isfinite(row["confidence"]) or not .8 <= row["confidence"] <= 1):
            reason = "unreliable_score"
        else:
            try:
                if _evidence_time(receipt["created_at"]) != occurred:
                    reason = "receipt_timestamp_mismatch"
            except (TypeError, ValueError):
                reason = "receipt_timestamp_mismatch"
        scope = {}
        if not reason and receipts:
            session = connection.execute("SELECT child_id FROM daily_session WHERE session_id=?", (row["session_id"],)).fetchone()
            if (row["evidence_type"] != "diagnostic_answer" or receipt["session_id"] != row["session_id"]
                    or not session or session[0] != child_id):
                reason = "ownership_or_type_mismatch"
            if not reason and receipt["review_task_id"]:
                review = connection.execute("""SELECT rt.*,le.child_id AS source_child,le.knowledge_node_id AS source_node,
                    le.occurred_at AS source_time FROM review_task rt JOIN learning_evidence le ON le.evidence_id=rt.source_evidence_id
                    WHERE rt.review_task_id=?""", (receipt["review_task_id"],)).fetchone()
                source["review"] = dict(review) if review else None
                if (not review or review["child_id"] != child_id or review["source_child"] != child_id
                        or review["knowledge_node_id"] != row_node or review["source_node"] != row_node
                        or review["diagnostic_item_id"] != receipt["diagnostic_item_id"]
                        or review["status"] != "completed" or review["result_evidence_id"] != evidence_id
                        or review["source_evidence_id"] in withdrawn):
                    reason = "review_receipt_unavailable"
                else:
                    try:
                        if occurred < max(_evidence_time(review["due_at"]), _evidence_time(review["source_time"])):
                            reason = "review_not_due"
                    except (TypeError, ValueError):
                        reason = "review_not_due"
        if not reason and mistakes:
            case = connection.execute("SELECT child_id,knowledge_node_id,status,diagnostic_item_id FROM mistake_case WHERE mistake_id=?", (receipt["mistake_id"],)).fetchone()
            source["mistake"] = dict(case) if case else None
            if (not case or case["child_id"] != child_id or case["knowledge_node_id"] != row_node
                    or case["status"] == "excluded" or row["evidence_type"] != f"mistake_{receipt['stage']}"
                    or receipt["hint_level"] != row["hint_level"] or receipt["independent"] != row["independent"]):
                reason = "mistake_receipt_unavailable"
            scope = {"stage": receipt["stage"], "mistake_id": receipt["mistake_id"]}
        if not reason:
            qualification_context = diagnostic_media_scope(connection, child_id, receipt['diagnostic_item_id'],
                                                           evidence_id, as_of=now.isoformat()) if receipts else {}
            qualified = qualified_exact_score(connection, child_id, receipt["diagnostic_item_id"], receipt["submitted_answer"],
                                               qualification_context=qualification_context, **scope)
            source["qualified_item"] = qualified[0] if qualified else None
            if not qualified or qualified[0]["knowledge_node_id"] != row_node or not source["question_identity"]:
                reason = "source_unqualified_or_reference_only"
            elif qualified[1] != row["correctness"]:
                reason = "score_receipt_mismatch"
            elif (receipts and qualification_context.get('prompt_id')
                    and json.loads(qualified[0]['expected_answer_json']).get('answer_format') == 'reviewed_spoken_numeric_v1'):
                from .numeric_clarification import history_reason
                reason = history_reason(connection, qualification_context['prompt_id'])
        if reason:
            excluded.append({"evidence_id": evidence_id, "reason": reason, "knowledge_node_id": row_node,
                             "question_identity": source["question_identity"], "identity_version": QUESTION_IDENTITY_VERSION})
        else:
            candidates[row["evidence_id"]] = source
    identity_nodes = {}
    for source in candidates.values():
        identity_nodes.setdefault(source["question_identity"], set()).add(source["evidence"]["knowledge_node_id"])
    seen = {}
    for occurred, _, row, receipt in sorted(exposures, key=lambda value: (value[0], value[1])):
        # A corrected transcript is one answer opportunity. The immutable old
        # receipt remains visible, but only the terminal replacement occupies
        # that opportunity in scoring and repetition rules.
        if terminal_replacement(replacements, row['evidence_id']) != row['evidence_id']:
            continue
        day = occurred.astimezone(timezone(timedelta(hours=8))).date().isoformat()
        item_id = receipt["diagnostic_item_id"]
        identity = identities.get(item_id)
        key = identity or ("unavailable_item", item_id)
        previous = seen.get(key)
        seen[key] = occurred
        source = candidates.get(row["evidence_id"])
        if source is None:
            continue
        conflict = len(identity_nodes.get(identity, ())) > 1
        if conflict or previous is not None and occurred - previous < timedelta(hours=24):
            excluded.append({"evidence_id": row["evidence_id"],
                             "reason": "question_identity_node_conflict" if conflict else "same_item_within_24h",
                             "knowledge_node_id": row["knowledge_node_id"], "question_identity": identity,
                             "identity_version": QUESTION_IDENTITY_VERSION,
                             "diagnostic_item_id": item_id, "occurred_at": row["occurred_at"]})
            continue
        effective.append({k: row[k] for k in ("evidence_id", "correctness", "hint_level", "independent", "confidence", "occurred_at")})
        review = source.get("review") or {}
        effective[-1].update(diagnostic_item_id=item_id, learning_date=day, knowledge_node_id=row["knowledge_node_id"],
            question_identity=identity, identity_version=QUESTION_IDENTITY_VERSION,
            receipt_kind="diagnostic" if source["diagnostic_receipts"] else "mistake",
            review_task_id=receipt.get("review_task_id"), stage=receipt.get("stage"),
            mistake_id=receipt.get("mistake_id"), review_source_evidence_id=review.get("source_evidence_id"),
            review_due_at=review.get("due_at"), evidence_type=row["evidence_type"],
            independence_reason="reported_hint" if row["hint_level"] else (None if row["independent"] else "not_independent"))
        tutor_refs = _tutor_refs(catalog, identities, tutor_index, item_id, occurred)
        effective[-1]["tutor_refs"] = tutor_refs
        if tutor_refs:
            effective[-1].update(independent=0, independence_reason="tutor_assistance_within_24h")
        if source['mistake_receipts'] and receipt.get('stage') in NEW_TRANSFER_STAGES:
            historical_refs = _tutor_refs(catalog, identities, tutor_index, item_id, occurred, window=None)
            effective[-1]['transfer_tutor_refs'] = historical_refs
            if historical_refs and not tutor_refs:
                effective[-1].update(independent=0, independence_reason='transfer_question_previously_taught')
            elif not tutor_refs and (previous is not None or identity == identities.get(
                    source['mistake']['diagnostic_item_id'])):
                effective[-1].update(independent=0, independence_reason='transfer_question_previously_seen')
    pending_reviews = [dict(row) for row in connection.execute("""SELECT knowledge_node_id,review_task_id,source_evidence_id,due_at FROM review_task
        WHERE child_id=? AND status='pending' ORDER BY due_at,review_task_id""", (child_id,))]
    return {node: _node_projection(child_id, node, effective, excluded, basis, identity_nodes, pending_reviews)
            for node in sorted({row["knowledge_node_id"] for row in rows})}


def _node_projection(child_id, knowledge_node_id, effective, excluded, basis, identity_nodes, pending_reviews):
    node_identities = {source["question_identity"] for source in basis
        if source["evidence"]["knowledge_node_id"] == knowledge_node_id and source["question_identity"]}
    basis = [source for source in basis if source["evidence"]["knowledge_node_id"] == knowledge_node_id
             or source["question_identity"] in node_identities]
    related_nodes = sorted({source["evidence"]["knowledge_node_id"] for source in basis})
    effective = [row for row in effective if row["knowledge_node_id"] == knowledge_node_id]
    excluded = [row for row in excluded if row["knowledge_node_id"] == knowledge_node_id]
    accepted_ids = {r["evidence_id"] for r in effective}
    reviews = [{key: r[key] for key in ("review_task_id", "source_evidence_id", "due_at")} for r in pending_reviews
               if r["knowledge_node_id"] == knowledge_node_id and r["source_evidence_id"] in accepted_ids]
    valid_due = []
    for review in reviews:
        try:
            valid_due.append(_evidence_time(review["due_at"]))
        except (TypeError, ValueError):
            pass
    payload = {"version": MASTERY_REPLAY_VERSION, "child_id": child_id, "knowledge_node_id": knowledge_node_id,
               "identity_version": QUESTION_IDENTITY_VERSION, "related_knowledge_node_ids": related_nodes,
               "identity_conflicts": [{"question_identity": identity, "knowledge_node_ids": sorted(identity_nodes[identity])}
                   for identity in sorted(node_identities) if len(identity_nodes.get(identity, ())) > 1],
               "effective_evidence": effective, "all_effective_receipts": effective,
               "excluded_evidence": sorted(excluded, key=lambda r: r["evidence_id"]),
               "next_review_at": min(valid_due).isoformat() if valid_due else None, "mastery_claim_allowed": False}
    payload["evidence_fingerprint"] = hashlib.sha256(json.dumps({"projection": payload, "basis": basis, "reviews": reviews},
        sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return payload


def cancel_unstarted_plans(connection, child_id: str, source_type: str, source_ref: str, now: str) -> None:
    rows = connection.execute("""SELECT pi.plan_item_id FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        WHERE dp.child_id=? AND pi.source_type=? AND pi.source_ref=?
          AND pi.status IN ('pending','rescheduled') AND pi.started_at IS NULL""",
        (child_id, source_type, source_ref)).fetchall()
    for row in rows:
        connection.execute("UPDATE plan_item SET status='cancelled' WHERE plan_item_id=?", (row[0],))
        connection.execute("""INSERT OR IGNORE INTO plan_item_event
            (event_id,plan_item_id,event_type,occurred_at,payload_json,client_action_id)
            VALUES (?,?,'source_withdrawn',?,'{}',?)""",
            (str(uuid4()), row[0], now, "source-withdrawn:" + row[0]))


def withdrawn_evidence_ids(connection, child_id: str) -> set[str]:
    # Privacy withdrawal is permanent for these exact receipts, not for a shared
    # question or a later explicit source import. Reading never initializes DDL.
    privacy_seed = ''
    params = (child_id, child_id, child_id, child_id)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='privacy_source_evidence_withdrawal'").fetchone():
        privacy_seed = '''SELECT w.evidence_id FROM privacy_source_evidence_withdrawal w
            JOIN learning_evidence le USING(evidence_id)
            WHERE w.child_id=? AND le.child_id=w.child_id UNION '''
        params = (child_id,) + params
    correction_seed = ''
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_evidence_withdrawal'").fetchone():
        correction_seed = '''SELECT w.evidence_id FROM learning_evidence_withdrawal w
            JOIN learning_evidence le USING(evidence_id)
            WHERE w.child_id=? AND le.child_id=w.child_id UNION '''
        params = (child_id,) + params
    rows = connection.execute("""WITH RECURSIVE withdrawn(evidence_id) AS (""" + correction_seed + privacy_seed + """
        SELECT le.evidence_id FROM mistake_case mc JOIN learning_evidence le ON le.evidence_id=mc.source_evidence_id
          WHERE mc.child_id=? AND mc.status='excluded' AND le.child_id=mc.child_id AND le.knowledge_node_id=mc.knowledge_node_id
        UNION SELECT le.evidence_id FROM mistake_occurrence mo JOIN mistake_case mc USING(mistake_id)
          JOIN learning_evidence le ON le.evidence_id=mo.evidence_id
          WHERE mc.child_id=? AND mc.status='excluded' AND mo.child_id=mc.child_id
            AND le.child_id=mc.child_id AND le.knowledge_node_id=mc.knowledge_node_id
        UNION SELECT le.evidence_id FROM mistake_attempt ma JOIN mistake_case mc USING(mistake_id)
          JOIN learning_evidence le ON le.evidence_id=ma.evidence_id
          WHERE mc.child_id=? AND mc.status='excluded' AND ma.child_id=mc.child_id
            AND le.child_id=mc.child_id AND le.knowledge_node_id=mc.knowledge_node_id
        UNION SELECT da.evidence_id FROM withdrawn w JOIN review_task rt ON rt.source_evidence_id=w.evidence_id
          JOIN learning_evidence source ON source.evidence_id=w.evidence_id
          JOIN diagnostic_attempt da ON da.review_task_id=rt.review_task_id
          JOIN learning_evidence result ON result.evidence_id=da.evidence_id
          WHERE rt.child_id=? AND da.child_id=rt.child_id AND source.child_id=rt.child_id
            AND result.child_id=rt.child_id AND source.knowledge_node_id=rt.knowledge_node_id
            AND result.knowledge_node_id=rt.knowledge_node_id)
        SELECT evidence_id FROM withdrawn WHERE evidence_id IS NOT NULL""", params).fetchall()
    return {row[0] for row in rows}


def withdraw_excluded_reviews(connection, child_id: str, now: str) -> None:
    # Withdraw descendants, but not unrelated evidence of the same node.
    withdrawn = withdrawn_evidence_ids(connection, child_id)
    rows = connection.execute("SELECT review_task_id,source_evidence_id FROM review_task WHERE child_id=? AND status IN ('pending','cancelled')", (child_id,)).fetchall()
    for row in rows:
        if row["source_evidence_id"] not in withdrawn:
            continue
        connection.execute("UPDATE review_task SET status='cancelled' WHERE review_task_id=? AND status='pending'", (row[0],))
        cancel_unstarted_plans(connection, child_id, "due_review", row[0], now)
    for row in connection.execute("SELECT mistake_id FROM mistake_case WHERE child_id=? AND status='excluded'", (child_id,)):
        cancel_unstarted_plans(connection, child_id, "mistake_review", row[0], now)


def _recent_answer_rows(connection, child_id, now, *, item_id=None):
    sql = """SELECT diagnostic_item_id,created_at FROM (
        SELECT da.diagnostic_item_id,da.child_id,da.created_at FROM diagnostic_attempt da
          JOIN daily_session ds ON ds.session_id=da.session_id AND ds.child_id=da.child_id
          JOIN learning_evidence le ON le.evidence_id=da.evidence_id AND le.child_id=da.child_id AND le.session_id=da.session_id
        UNION ALL SELECT ma.diagnostic_item_id,ma.child_id,ma.created_at FROM mistake_attempt ma
          JOIN mistake_case mc ON mc.mistake_id=ma.mistake_id AND mc.child_id=ma.child_id
          JOIN learning_evidence le ON le.evidence_id=ma.evidence_id AND le.child_id=ma.child_id
        ) WHERE child_id=? AND julianday(created_at)>julianday(?)
          AND julianday(created_at)<=julianday(?)"""
    parameters = (child_id, (now - timedelta(hours=24)).isoformat(), now.isoformat())
    if item_id is not None:
        sql += ' AND diagnostic_item_id=?'
        parameters += (item_id,)
    return connection.execute(sql, parameters).fetchall()


def recent_same_item_answer(connection, child_id, item_id, as_of):
    """Necessary repeated-answer rejection only; a miss grants no admission."""
    now = _evidence_time(as_of)
    for row in _recent_answer_rows(connection, child_id, now, item_id=item_id):
        try:
            if now - timedelta(hours=24) < _evidence_time(row['created_at']) <= now:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _item_exposure(connection, child_id, item_id, as_of, catalog, identities):
    now = _evidence_time(as_of)
    identity = identities.get(item_id)
    aliases = {key for key, value in identities.items() if value == identity} if identity else {item_id}
    prior = _recent_answer_rows(connection, child_id, now)
    repeated = False
    for row in prior:
        try:
            if row["diagnostic_item_id"] in aliases and now - timedelta(hours=24) < _evidence_time(row["created_at"]) <= now:
                repeated = True
                break
        except (TypeError, ValueError):
            pass
    refs = _tutor_refs(catalog, identities, _tutor_exposure_index(connection, child_id, catalog, identities), item_id, now)
    return {"answer_repeated": repeated, "tutor_assisted": bool(refs), "tutor_refs": refs,
            "question_identity": identity, "identity_version": QUESTION_IDENTITY_VERSION}


def project_item_exposures(connection, child_id: str, item_id: str, *, as_of) -> list[dict]:
    """One catalog of frozen question content, independent time-window decisions.

    Only this synchronous call shares the catalog. Current source eligibility
    is not stored here; tutor evidence is checked separately for each window.
    """
    import sqlite3
    if not 1 <= len(as_of) <= 2:
        raise ValueError('EXPOSURE_WINDOW_COUNT_INVALID')
    for at in as_of:
        _evidence_time(at)
    changes = getattr(connection, 'total_changes', None)
    if (not getattr(connection, 'in_transaction', False) or type(changes) is not int
            or getattr(connection, 'row_factory', None) is not sqlite3.Row):
        return [_item_exposure(connection, child_id, item_id, at, *_question_catalog(connection)) for at in as_of]
    catalog, identities = _question_catalog(connection)
    result = [_item_exposure(connection, child_id, item_id, at, catalog, identities) for at in as_of]
    if not connection.in_transaction or connection.total_changes != changes:
        raise ValueError('QUESTION_SOURCE_READ_CHANGED')
    return result
