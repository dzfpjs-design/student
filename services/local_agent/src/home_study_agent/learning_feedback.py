"""Planning interpretation of the shared, source-validated evidence projection."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json

from .learning_obligations import project_child_mastery_evidence

VERSION = "reliable_learning_feedback_v8"
PRIORITY = {"recheck_needed": 0, "supported_only": 1, "insufficient_evidence": 2,
            "independent_observed": 3, "retention_observed": 4}


def school_original_observations(connection, child_id, now, *, evidence_ids=None):
    """Recent selection facts, or exact historical IDs with live authority checks."""
    from .period_written_observations import qualified_original_sources
    parameters = [child_id]
    if evidence_ids is None:
        selection = 'julianday(le.occurred_at)>=julianday(?)'
        parameters.append((now - timedelta(days=90)).isoformat())
    else:
        ids = sorted(set(evidence_ids))
        if not ids:
            return []
        selection = 'le.evidence_id IN (' + ','.join('?' for _ in ids) + ')'
        parameters.extend(ids)
    rows = connection.execute('''SELECT le.evidence_id,le.knowledge_node_id,le.occurred_at,
        kn.subject,kn.title,kn.track,da.diagnostic_item_id
        FROM learning_evidence le JOIN knowledge_node kn USING(knowledge_node_id)
        JOIN diagnostic_attempt da ON da.evidence_id=le.evidence_id
          AND da.child_id=le.child_id AND da.session_id=le.session_id
        JOIN diagnostic_item di ON di.diagnostic_item_id=da.diagnostic_item_id
          AND di.knowledge_node_id=le.knowledge_node_id
        WHERE le.child_id=? AND kn.track='school' AND di.answer_type='open_response'
          AND di.assessment_mode='open_response_rubric'
          AND ''' + selection + '''
        ORDER BY julianday(le.occurred_at),le.evidence_id''', parameters).fetchall()
    facts = []
    for row, score in qualified_original_sources(connection, child_id, rows, now=now):
        current, decision, receipt = score['current'], score['decision'], score['receipt']
        dimensions = (current['review']['dimensions'] if current.get('review')
                      else receipt['calibration_receipt']['dimensions'])
        definitions = {d['dimension_id']: d for d in current['dimensions']}
        checked = [{k: definitions[d['dimension_id']][k] for k in
                    ('dimension_id', 'criterion', 'max_score', 'pass_score')} | {'score': d['score']}
                   for d in dimensions]
        checked.sort(key=lambda d: d['dimension_id'])
        facts.append({**dict(row), 'evidence_kind': 'qualified_school_original',
            **({'response_mode':'spoken_explanation'} if score.get('assessment_modality') in
                {'reviewed_spoken_reasoning_text','calibrated_spoken_reasoning_text'} else {}),
            'assessment_id': current['assessment_id'],
            'original_binding_version': score['original_binding_version'],
            'score_authority_id': decision['authority_id'], 'score_revision': decision['review_revision'],
            'score_status': decision['status'], 'passed': decision['passed'],
            'dimensions': checked,
            'hint_level': receipt['hint_level'], 'original_independent': bool(receipt['independent']),
            'mastery_claim_allowed': False, 'cloud_transmission_allowed': False})
    return facts


def _merge_school_observations(nodes, observations, projections):
    exact_ids = {r['evidence_id'] for p in projections.values() for r in p['effective_evidence']}
    by_node = {node['knowledge_node_id']: node for node in nodes}
    for fact in observations:
        if fact['evidence_id'] in exact_ids:
            continue
        node_id = fact['knowledge_node_id']
        node = by_node.get(node_id)
        if node is None:
            node = {k: fact[k] for k in ('knowledge_node_id', 'subject', 'title', 'track')}
            node.update(evidence_refs=[], attempt_count=0, supported_attempt_count=0,
                independent_attempt_count=0, independent_correct=0, distinct_items=0,
                independent_distinct_items=0, retention_success_count=0, retention_basis_refs=[],
                latest_evidence_at=fact['occurred_at'], mastery_claim_allowed=False)
            by_node[node_id] = node
            nodes.append(node)
        node.setdefault('qualified_original_observations', []).append(fact)
        node['qualified_original_count'] = len(node['qualified_original_observations'])
        node['evidence_refs'].append(fact['evidence_id'])
        if (_time(fact['occurred_at']) >= _time(node['latest_evidence_at'])
                and not (fact['passed'] and node.get('status') in {'recheck_needed', 'supported_only'})):
            low = not fact['passed']
            original = '本次口头说明' if fact.get('response_mode') == 'spoken_explanation' else '本份原稿'
            node.update(status='recheck_needed' if low else 'insufficient_evidence',
                next_action='clarify_then_recheck' if low else 'schedule_independent_check',
                reason=(original + '经可靠量表核对仍有不足；先核实卡住的步骤，再做独立检查，不据此推断错因。'
                    if low else original + '已核对；仍需独立换题与跨天检查，不据此认证掌握、迁移或保持。'),
                latest_evidence_at=fact['occurred_at'], latest_evidence_kind='qualified_school_original')


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _mistake_retention_bases(connection, projections):
    from .diagnostic_prompt import _TransactionDatabase
    from .mistake_book import MistakeBookService
    cases = {r['mistake_id'] for projection in projections.values() for r in projection['effective_evidence']
             if r.get('receipt_kind') == 'mistake' and r.get('stage') in {'delayed_48h', 'delayed_7d'}}
    bases = {}
    book = MistakeBookService(_TransactionDatabase(connection))
    for case_id in sorted(cases):
        book._gate_summary(connection, case_id, _projections=projections, _retention_basis=bases)
    return bases


def session_report_feedback(connection, session_id: str, *, _projections: dict | None = None) -> dict:
    """Revalidate the report's own receipts without importing later learning results."""
    session = connection.execute("SELECT child_id FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
    if not session:
        raise ValueError("session required")
    rows = connection.execute("""SELECT le.evidence_id,le.knowledge_node_id,le.evidence_type,
        kn.subject,kn.title AS knowledge_title FROM learning_evidence le
        JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE le.session_id=? AND le.child_id=? ORDER BY le.occurred_at,le.rowid""",
        (session_id, session["child_id"])).fetchall()
    projections = _projections if _projections is not None else project_child_mastery_evidence(connection, session["child_id"])
    retention = _mistake_retention_bases(connection, projections)
    items, excluded = [], []
    for raw in rows:
        row = dict(raw)
        node = row["knowledge_node_id"]
        projection = projections[node]
        receipt = next((r for r in projection["effective_evidence"] if r["evidence_id"] == row["evidence_id"]), None)
        if receipt is None:
            reason = next((r["reason"] for r in projection["excluded_evidence"] if r["evidence_id"] == row["evidence_id"]), "unverified")
            excluded.append({"evidence_id": row["evidence_id"], "reason": reason})
            continue
        items.append({**row, **{key: receipt[key] for key in (
            "correctness", "hint_level", "independent", "confidence", "occurred_at", "question_identity")},
            "score_status": "verified_local", "mastery_claim_allowed": False})
        if retention.get(row['evidence_id']):
            items[-1]['retention_basis_refs'] = retention[row['evidence_id']]
            items[-1]['retention_status'] = 'retention_observed'
    payload = {"version": VERSION, "items": items, "excluded": excluded,
               "unverified_count": len(excluded), "mastery_claim_allowed": False}
    payload["evidence_fingerprint"] = hashlib.sha256(json.dumps(
        {"child_id": session["child_id"], "session_id": session_id, **payload},
        ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    return payload


def report_dependencies_match(connection, child_id: str, dependencies) -> bool:
    if not isinstance(dependencies, list) or len(dependencies) > 3:
        return False
    projections = project_child_mastery_evidence(connection, child_id) if dependencies else {}
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            return False
        session_id = dependency.get("session_id")
        owner = connection.execute("SELECT child_id FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
        if not owner or owner[0] != child_id:
            return False
        if session_report_feedback(connection, session_id, _projections=projections)["evidence_fingerprint"] != dependency.get("evidence_fingerprint"):
            return False
    return True


def build_learning_feedback(connection, child_id: str, now: datetime | None = None, *, evidence_projections: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not connection.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
        raise ValueError("active child required")
    catalog = connection.execute("""SELECT DISTINCT kn.knowledge_node_id,kn.subject,kn.title,kn.track
        FROM knowledge_node kn JOIN learning_evidence le USING(knowledge_node_id)
        WHERE le.child_id=? ORDER BY kn.knowledge_node_id""", (child_id,)).fetchall()
    nodes, fingerprints, rejected = [], [], Counter()
    # A caller may share this child's projection within the same read transaction.
    projections = evidence_projections if evidence_projections is not None else project_child_mastery_evidence(connection, child_id, as_of=now.isoformat())
    mistake_retention = _mistake_retention_bases(connection, projections)
    for raw in catalog:
        node = dict(raw)
        projection = projections[node["knowledge_node_id"]]
        fingerprints.append(projection["evidence_fingerprint"])
        rejected.update(item["reason"] for item in projection["excluded_evidence"])
        history = sorted(projection["effective_evidence"], key=lambda r: _time(r["occurred_at"]))
        # Reuse the current mistake gate's qualified prerequisites and due dates,
        # rather than treating a delayed stage label as proof of retention.
        current, baselines = [], []
        valid_ids = {r["evidence_id"] for r in history}
        for source in history:
            row = dict(source)
            occurred = _time(row["occurred_at"])
            independent = row["independent"] and row["hint_level"] == 0
            row["retention_basis"] = None
            # The statistical window must not truncate the provenance chain.
            baseline = next((r for r in reversed(baselines)
                             if (occurred - _time(r["occurred_at"])).total_seconds() >= 86400), None)
            review = connection.execute("""SELECT rt.source_evidence_id,le.occurred_at AS source_time
                FROM diagnostic_attempt da JOIN review_task rt ON rt.review_task_id=da.review_task_id
                JOIN learning_evidence le ON le.evidence_id=rt.source_evidence_id
                WHERE da.evidence_id=? AND da.child_id=? AND rt.child_id=da.child_id
                  AND rt.result_evidence_id=da.evidence_id""", (row["evidence_id"], child_id)).fetchone()
            if (row["correctness"] == 1 and independent and baseline and review
                    and review["source_evidence_id"] in valid_ids
                    and (occurred - _time(review["source_time"])).total_seconds() >= 86400):
                row["retention_basis"] = baseline["evidence_id"]
            elif row['evidence_id'] in mistake_retention:
                basis = mistake_retention[row['evidence_id']]
                if basis:
                    row['retention_basis'] = basis[0]
                    row['retention_basis_refs'] = basis
            if row["correctness"] == 1 and independent:
                baselines.append(row)
            if occurred >= now - timedelta(days=90):
                current.append(row)
        if not current:
            continue
        latest = current[-1]
        independent = [r for r in current if r["correctness"] == 1 and r["independent"] and r["hint_level"] == 0]
        retained = [r for r in independent if r["retention_basis"]]
        if latest["correctness"] < 1:
            status, action = "recheck_needed", "clarify_then_recheck"
            reason = "最近一次有效作答仍有错误；先核实卡住的步骤，不能仅凭错误推断错因。"
        elif not latest["independent"] or latest["hint_level"] > 0:
            status, action = "supported_only", "fade_hint_then_check"
            reason = "最近有效作答尚不能证明独立完成；下一步减少提示并换题检查。"
        elif latest["retention_basis"]:
            status, action = "retention_observed", "check_transfer"
            reason = "先有独立正确依据，至少24小时后又独立通过复测；这是保持证据，不代表全部掌握。"
        else:
            status, action = "independent_observed", "schedule_delayed_check"
            reason = "已有独立正确作答；仍需不同题目与跨天复测，不能由同题重复判断迁移。"
        nodes.append({**node, "status": status, "next_action": action, "reason": reason,
            "evidence_refs": [r["evidence_id"] for r in current], "attempt_count": len(current),
            "supported_attempt_count": sum(r["hint_level"] > 0 or not r["independent"] for r in current),
            "independent_attempt_count": sum(r["hint_level"] == 0 and bool(r["independent"]) for r in current),
            "independent_correct": len(independent), "distinct_items": len({r["question_identity"] for r in current}),
            "independent_distinct_items": len({r["question_identity"] for r in independent}),
            "retention_success_count": len(retained), "latest_evidence_at": latest["occurred_at"],
            "retention_basis_refs": sorted({ref for r in retained for ref in
                r.get('retention_basis_refs', [r['retention_basis']])}), "mastery_claim_allowed": False})
    _merge_school_observations(nodes, school_original_observations(connection, child_id, now), projections)
    nodes.sort(key=lambda n: (PRIORITY[n["status"]], n["latest_evidence_at"], n["knowledge_node_id"]))
    payload = {"version": VERSION, "window_days": 90, "nodes": nodes,
               "excluded": dict(sorted(rejected.items())), "status": "evidence_available" if nodes else "needs_baseline",
               "cloud_transmission_allowed": False, "mastery_claim_allowed": False}
    payload["evidence_fingerprint"] = hashlib.sha256(json.dumps(
        {"child_id": child_id, "projection": payload, "source_projections": fingerprints},
        sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return payload
