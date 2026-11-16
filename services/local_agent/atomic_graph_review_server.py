#!/usr/bin/env python3
"""Local, read-only explorer for the full atomic knowledge-graph build.

The build database is opened with SQLite ``mode=ro`` throughout.  Human review
decisions are intentionally stored in the configured application database, in
two small append-audited tables owned by this feature.  No textbook, video or
family data is copied or uploaded by this process.
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from uuid import uuid4


DEFAULT_BUILD_DB = (Path.home() / '.home-study-companion/knowledge-build/atomic-full-v4.db')
DEFAULT_APPLICATION_DB = (Path.home() / '.home-study-companion/home-study.db')
WEB_ROOT = Path(__file__).with_name("atomic_graph_review_web")
DECISIONS = {"approved", "rejected", "merge_suggestion", "relation_correction", "revert"}
ENTITY_TYPES = {"draft", "relation"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_value(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


class AtomicGraphRepository:
    """One-way adapter: production build data in, reviewer decisions out."""

    def __init__(self, build_db: Path | str, application_db: Path | str):
        self.build_db = Path(build_db).expanduser().resolve()
        self.application_db = Path(application_db).expanduser().resolve()
        if not self.build_db.is_file():
            raise FileNotFoundError(f"构建数据库不存在：{self.build_db}")
        self.application_db.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_review_store()

    def _build_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{quote(str(self.build_db))}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ? AS review", (str(self.application_db),))
        # URI read-only mode protects the build file; this second guard makes an
        # accidental future INSERT/UPDATE fail even before SQLite reaches disk.
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _app_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.application_db)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_review_store(self) -> None:
        with self._app_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kg_atomic_review_decision (
                  entity_type TEXT NOT NULL CHECK(entity_type IN ('draft','relation')),
                  entity_key TEXT NOT NULL,
                  action TEXT NOT NULL CHECK(action IN ('approved','rejected','merge_suggestion','relation_correction','revert')),
                  reviewer TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  client_action_id TEXT NOT NULL UNIQUE,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(entity_type, entity_key)
                );
                CREATE TABLE IF NOT EXISTS kg_atomic_review_event (
                  event_id TEXT PRIMARY KEY,
                  entity_type TEXT NOT NULL,
                  entity_key TEXT NOT NULL,
                  action TEXT NOT NULL,
                  reviewer TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  previous_action TEXT,
                  client_action_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kg_atomic_review_event_entity
                  ON kg_atomic_review_event(entity_type, entity_key, created_at DESC);
                """
            )

    @staticmethod
    def _effective_status(action: str | None, fallback: str = "unreviewed") -> str:
        return {"merge_suggestion": "needs_merge", "relation_correction": "needs_correction", "revert": fallback}.get(action or "", action or fallback)

    def decision_for(self, entity_type: str, entity_key: str, fallback: str = "unreviewed") -> dict[str, Any]:
        with self._app_connection() as connection:
            row = connection.execute(
                "SELECT action,reviewer,note,payload_json,updated_at FROM kg_atomic_review_decision WHERE entity_type=? AND entity_key=?",
                (entity_type, entity_key),
            ).fetchone()
        if not row:
            return {"action": None, "effective_status": fallback, "reviewer": None, "note": "", "payload": {}, "updated_at": None}
        result = dict(row)
        result["payload"] = json_value(result.pop("payload_json"), {})
        result["effective_status"] = self._effective_status(result["action"], fallback)
        return result

    def options(self) -> dict[str, list[str]]:
        with self._build_connection() as connection:
            grades = [row[0] for row in connection.execute("SELECT DISTINCT grade_band FROM kg_atomic_definition_draft ORDER BY grade_band")]
            subjects = [row[0] for row in connection.execute("SELECT DISTINCT subject FROM kg_atomic_definition_draft ORDER BY subject")]
            chapters = [row[0] for row in connection.execute("SELECT DISTINCT unit_path FROM kg_atomic_candidate WHERE unit_path<>'' ORDER BY unit_path LIMIT 400")]
        return {"grades": grades, "subjects": subjects, "chapters": chapters,
                "statuses": ["unreviewed", "approved", "rejected", "needs_merge", "needs_correction"]}

    def graph_page(self, *, grade: str | None = None, subject: str | None = None, chapter: str | None = None,
                   status: str | None = None, cursor: str | None = None, limit: int = 50, focus: str | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        clauses, params = ["1=1"], []
        if grade:
            clauses.append("d.grade_band=?"); params.append(grade)
        if subject:
            clauses.append("d.subject=?"); params.append(subject)
        if chapter:
            clauses.append("EXISTS (SELECT 1 FROM kg_atomic_candidate c WHERE c.canonical_signature=d.canonical_signature AND c.unit_path=?)")
            params.append(chapter)
        state = "CASE r.action WHEN 'merge_suggestion' THEN 'needs_merge' WHEN 'relation_correction' THEN 'needs_correction' WHEN 'revert' THEN d.review_status ELSE COALESCE(r.action,d.review_status) END"
        if status:
            clauses.append(f"{state}=?"); params.append(status)
        after_title, after_key = self._decode_cursor(cursor)
        if after_title:
            clauses.append("(d.title > ? OR (d.title=? AND d.canonical_signature>?))")
            params.extend([after_title, after_title, after_key])
        if focus:
            clauses.append("(d.canonical_signature=? OR EXISTS (SELECT 1 FROM kg_atomic_relation_candidate f WHERE (f.source_key=d.canonical_signature OR f.target_key=d.canonical_signature) AND (f.source_key=? OR f.target_key=?)))")
            params.extend([focus, focus, focus])
        query = f"""SELECT d.canonical_signature AS key,d.title,d.grade_band,d.subject,d.atom_kind,d.confidence,d.review_status,
                {state} AS effective_status,
                (SELECT GROUP_CONCAT(DISTINCT c.unit_path) FROM kg_atomic_candidate c WHERE c.canonical_signature=d.canonical_signature AND c.unit_path<>'') AS chapters
            FROM kg_atomic_definition_draft d
            LEFT JOIN review.kg_atomic_review_decision r ON r.entity_type='draft' AND r.entity_key=d.canonical_signature
            WHERE {' AND '.join(clauses)} ORDER BY d.title,d.canonical_signature LIMIT ?"""
        with self._build_connection() as connection:
            rows = [dict(row) for row in connection.execute(query, (*params, limit + 1)).fetchall()]
            selected = rows[:limit]
            keys = [row["key"] for row in selected]
            edges = self._edges_for(connection, keys)
        for row in selected:
            row["chapters"] = (row["chapters"] or "").split(",") if row["chapters"] else []
        next_cursor = self._encode_cursor(selected[-1]["title"], selected[-1]["key"]) if len(rows) > limit and selected else None
        return {"nodes": selected, "edges": edges, "next_cursor": next_cursor, "page_size": limit}

    def _edges_for(self, connection: sqlite3.Connection, keys: list[str]) -> list[dict[str, Any]]:
        if not keys:
            return []
        marks = ",".join("?" for _ in keys)
        state = "CASE r.action WHEN 'merge_suggestion' THEN 'needs_merge' WHEN 'relation_correction' THEN 'needs_correction' WHEN 'revert' THEN rc.review_status ELSE COALESCE(r.action,rc.review_status) END"
        rows = connection.execute(f"""SELECT rc.relation_id AS key,rc.source_key,rc.target_key,rc.relation_type,rc.confidence,rc.rationale,rc.evidence_ref,
                {state} AS effective_status
            FROM kg_atomic_relation_candidate rc
            LEFT JOIN review.kg_atomic_review_decision r ON r.entity_type='relation' AND r.entity_key=rc.relation_id
            WHERE rc.source_key IN ({marks}) AND rc.target_key IN ({marks})
            ORDER BY rc.relation_type,rc.confidence DESC LIMIT 500""", (*keys, *keys)).fetchall()
        return [dict(row) for row in rows]

    def node_detail(self, key: str) -> dict[str, Any]:
        with self._build_connection() as connection:
            row = connection.execute("SELECT * FROM kg_atomic_definition_draft WHERE canonical_signature=?", (key,)).fetchone()
            if not row:
                raise KeyError("未找到原子知识点")
            node = dict(row)
            evidence = connection.execute("""SELECT e.evidence_role,e.exact_quote,e.confidence,r.title AS resource_title,
                    r.transcript_relative_path,r.video_relative_path,r.source_ref,s.ordinal,s.start_char,s.end_char
                FROM kg_atomic_draft_evidence e JOIN kg_resource r ON r.resource_id=e.resource_id
                JOIN kg_segment s ON s.segment_id=e.segment_id WHERE e.draft_id=? ORDER BY e.confidence DESC,e.evidence_role LIMIT 80""", (node["draft_id"],)).fetchall()
            textbooks = connection.execute("""SELECT a.exact_quote,a.lexical_score,a.scope_score,a.match_method,a.evidence_role,a.review_status,
                    r.title AS resource_title,r.transcript_relative_path,s.ordinal,s.start_char,s.end_char
                FROM kg_atomic_textbook_alignment a JOIN kg_resource r ON r.resource_id=a.textbook_resource_id
                JOIN kg_segment s ON s.segment_id=a.textbook_segment_id WHERE a.draft_id=? ORDER BY a.lexical_score DESC LIMIT 60""", (node["draft_id"],)).fetchall()
            standards = connection.execute("SELECT source_ref,exact_quote,confidence FROM kg_atomic_draft_standard_alignment WHERE draft_id=? ORDER BY confidence DESC LIMIT 60", (node["draft_id"],)).fetchall()
            relation_rows = connection.execute("""SELECT rc.*,source.title AS source_title,target.title AS target_title
                FROM kg_atomic_relation_candidate rc
                LEFT JOIN kg_atomic_definition_draft source ON source.canonical_signature=rc.source_key
                LEFT JOIN kg_atomic_definition_draft target ON target.canonical_signature=rc.target_key
                WHERE rc.source_key=? OR rc.target_key=? ORDER BY rc.confidence DESC LIMIT 120""", (key, key)).fetchall()
        diagnostics = {str(item.get("level", f"level_{index}")): item for index, item in enumerate(json_value(node.pop("diagnostic_blueprint_json"), []))}
        node["boundary"] = json_value(node.pop("boundary_json"), {})
        node["mastery_criteria"] = json_value(node.pop("mastery_criteria_json"), {})
        node["diagnostics"] = {level: diagnostics.get(level, {}) for level in ("recognition", "independent", "transfer")}
        node["misconceptions"] = json_value(node.pop("misconception_hypotheses_json"), [])
        node["concept_tags"] = json_value(node.pop("concept_tags_json"), [])
        node["decision"] = self.decision_for("draft", key, node["review_status"])
        relations = []
        for relation in relation_rows:
            item = dict(relation)
            item["decision"] = self.decision_for("relation", item["relation_id"], item["review_status"])
            relations.append(item)
        return {"node": node, "evidence": {"video_transcript": [dict(item) for item in evidence], "textbook": [dict(item) for item in textbooks], "standards": [dict(item) for item in standards]}, "relations": relations}

    def quality_categories(self) -> set[str]:
        return {"duplicates", "isolated", "cycles", "missing_evidence", "pending"}

    def quality_queue(self, *, category: str, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        if category not in self.quality_categories():
            raise ValueError("未知审核队列")
        limit = max(1, min(int(limit), 100))
        with self._build_connection() as connection:
            if category == "duplicates":
                rows = connection.execute("""SELECT d.canonical_signature AS key,d.title,d.grade_band,d.subject,d.confidence,'duplicate_title' AS reason,
                    COUNT(*) OVER (PARTITION BY d.title) AS duplicate_count FROM kg_atomic_definition_draft d
                    WHERE d.title IN (SELECT title FROM kg_atomic_definition_draft GROUP BY title HAVING COUNT(*)>1)
                    ORDER BY duplicate_count DESC,d.title LIMIT ?""", (limit,)).fetchall()
            elif category == "isolated":
                rows = connection.execute("""SELECT d.canonical_signature AS key,d.title,d.grade_band,d.subject,d.confidence,'no_relation_candidate' AS reason
                    FROM kg_atomic_definition_draft d WHERE NOT EXISTS (SELECT 1 FROM kg_atomic_relation_candidate r WHERE r.source_key=d.canonical_signature OR r.target_key=d.canonical_signature)
                    ORDER BY d.confidence ASC,d.title LIMIT ?""", (limit,)).fetchall()
            elif category == "missing_evidence":
                rows = connection.execute("""SELECT d.canonical_signature AS key,d.title,d.grade_band,d.subject,d.confidence,
                    CASE WHEN NOT EXISTS(SELECT 1 FROM kg_atomic_draft_evidence e WHERE e.draft_id=d.draft_id) THEN 'missing_video_transcript_evidence'
                         WHEN NOT EXISTS(SELECT 1 FROM kg_atomic_textbook_alignment t WHERE t.draft_id=d.draft_id) THEN 'missing_textbook_evidence'
                         ELSE 'missing_standard_alignment' END AS reason
                    FROM kg_atomic_definition_draft d WHERE NOT EXISTS(SELECT 1 FROM kg_atomic_draft_evidence e WHERE e.draft_id=d.draft_id)
                       OR NOT EXISTS(SELECT 1 FROM kg_atomic_textbook_alignment t WHERE t.draft_id=d.draft_id)
                       OR NOT EXISTS(SELECT 1 FROM kg_atomic_draft_standard_alignment s WHERE s.draft_id=d.draft_id)
                    ORDER BY d.confidence ASC,d.title LIMIT ?""", (limit,)).fetchall()
            elif category == "cycles":
                rows = self._cycle_items(connection, limit)
            else:
                rows = connection.execute("""SELECT d.canonical_signature AS key,d.title,d.grade_band,d.subject,d.confidence,'unreviewed_definition' AS reason
                    FROM kg_atomic_definition_draft d LEFT JOIN review.kg_atomic_review_decision r ON r.entity_type='draft' AND r.entity_key=d.canonical_signature
                    WHERE CASE r.action WHEN 'revert' THEN d.review_status ELSE COALESCE(r.action,d.review_status) END='unreviewed'
                    ORDER BY d.confidence ASC,d.title LIMIT ?""", (limit,)).fetchall()
        return {"category": category, "items": [dict(row) for row in rows], "next_cursor": None}

    def _cycle_items(self, connection: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
        edges = connection.execute("SELECT relation_id,source_key,target_key,confidence FROM kg_atomic_relation_candidate WHERE relation_type='prerequisite_of'").fetchall()
        adjacency: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for edge in edges:
            adjacency[edge["source_key"]].append(edge)
        found, stack, on_path = [], [], set()
        def visit(node: str) -> None:
            if len(found) >= limit: return
            on_path.add(node); stack.append(node)
            for edge in adjacency.get(node, []):
                target = edge["target_key"]
                if target in on_path:
                    start = stack.index(target)
                    found.append({"key": edge["relation_id"], "title": " → ".join(stack[start:] + [target]), "reason": "prerequisite_cycle", "confidence": edge["confidence"], "relation_path": stack[start:] + [target]})
                elif len(stack) < 12:
                    visit(target)
                if len(found) >= limit: break
            stack.pop(); on_path.remove(node)
        for node in list(adjacency):
            if node not in on_path: visit(node)
            if len(found) >= limit: break
        return found

    def record_decision(self, *, entity_type: str, entity_key: str, action: str, reviewer: str, note: str,
                        payload: dict[str, Any], client_action_id: str) -> dict[str, Any]:
        if entity_type not in ENTITY_TYPES or action not in DECISIONS or not reviewer.strip() or len(client_action_id) < 8:
            raise ValueError("审核请求不完整或不合法")
        self._assert_entity_exists(entity_type, entity_key)
        now = utc_now()
        with self._app_connection() as connection:
            existing_event = connection.execute("SELECT 1 FROM kg_atomic_review_event WHERE client_action_id=?", (client_action_id,)).fetchone()
            if existing_event:
                return self.decision_for(entity_type, entity_key)
            prior = connection.execute("SELECT action FROM kg_atomic_review_decision WHERE entity_type=? AND entity_key=?", (entity_type, entity_key)).fetchone()
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            connection.execute("""INSERT INTO kg_atomic_review_decision(entity_type,entity_key,action,reviewer,note,payload_json,client_action_id,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_key) DO UPDATE SET action=excluded.action,reviewer=excluded.reviewer,note=excluded.note,payload_json=excluded.payload_json,client_action_id=excluded.client_action_id,updated_at=excluded.updated_at""",
                (entity_type, entity_key, action, reviewer.strip(), note.strip(), encoded, client_action_id, now))
            connection.execute("INSERT INTO kg_atomic_review_event(event_id,entity_type,entity_key,action,reviewer,note,payload_json,previous_action,client_action_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), entity_type, entity_key, action, reviewer.strip(), note.strip(), encoded, prior["action"] if prior else None, client_action_id, now))
        return self.decision_for(entity_type, entity_key)

    def _assert_entity_exists(self, entity_type: str, entity_key: str) -> None:
        table, column = ("kg_atomic_definition_draft", "canonical_signature") if entity_type == "draft" else ("kg_atomic_relation_candidate", "relation_id")
        with self._build_connection() as connection:
            if not connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (entity_key,)).fetchone():
                raise ValueError("待审核对象不在当前构建结果中")

    @staticmethod
    def _encode_cursor(title: str, key: str) -> str:
        return base64.urlsafe_b64encode(json.dumps([title, key], ensure_ascii=False).encode()).decode()

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[str, str]:
        if not cursor: return "", ""
        try: return tuple(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode()))  # type: ignore[return-value]
        except Exception: return "", ""


class ReviewRequestHandler(SimpleHTTPRequestHandler):
    repository: AtomicGraphRepository
    web_root: Path = WEB_ROOT

    def translate_path(self, path: str) -> str:
        path = unquote(urlparse(path).path)
        requested = (self.web_root / ("index.html" if path in {"", "/"} else path.lstrip("/"))).resolve()
        return str(requested if requested.is_relative_to(self.web_root.resolve()) else self.web_root / "index.html")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/api/health": body = {"status": "ready", "build_database": "read_only", "review_store": "application_database"}
            elif parsed.path == "/api/options": body = self.repository.options()
            elif parsed.path == "/api/graph": body = self.repository.graph_page(grade=query.get("grade"), subject=query.get("subject"), chapter=query.get("chapter"), status=query.get("status"), cursor=query.get("cursor"), limit=int(query.get("limit", "50")), focus=query.get("focus"))
            elif parsed.path == "/api/node": body = self.repository.node_detail(query["key"])
            elif parsed.path == "/api/queue": body = self.repository.quality_queue(category=query.get("category", "pending"), limit=int(query.get("limit", "50")), cursor=query.get("cursor"))
            else: return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            self._json(body)
        except (KeyError, ValueError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/decision": return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            body = self.repository.record_decision(entity_type=data["entity_type"], entity_key=data["entity_key"], action=data["action"], reviewer=data["reviewer"], note=data.get("note", ""), payload=data.get("payload", {}), client_action_id=data["client_action_id"])
            self._json(body, HTTPStatus.CREATED)
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _json(self, body: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Access-Control-Allow-Origin", "null"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description="本地原子知识图谱审核器（构建库只读）")
    parser.add_argument("--build-db", type=Path, default=DEFAULT_BUILD_DB)
    parser.add_argument("--app-db", type=Path, default=DEFAULT_APPLICATION_DB)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后自动打开正确的浏览器地址")
    arguments = parser.parse_args()
    ReviewRequestHandler.repository = AtomicGraphRepository(arguments.build_db, arguments.app_db)
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), ReviewRequestHandler)
    address = f"http://127.0.0.1:{arguments.port}/"
    print(f"审核界面：{address}（构建库只读；审核写入 {arguments.app_db}）")
    if arguments.open:
        webbrowser.open(address)
    server.serve_forever()


if __name__ == "__main__": main()
