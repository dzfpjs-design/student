from __future__ import annotations

import hashlib
from contextlib import nullcontext
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
from uuid import uuid4

from .database import Database
from .repository import UnknownChildError, utc_now


ALLOWED_SOURCE_TYPES = {"curriculum", "textbook_index", "school_material", "family_note"}
ALLOWED_LICENSES = {"public", "family_owned_index", "private_note", "unknown"}


def _source_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidKnowledgeSourceError("结构化题源包含重复字段，整批未保存。")
        result[key] = value
    return result


def _typed_document(content):
    try:
        structured = json.loads(content, object_pairs_hook=_source_object)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(structured, dict) or 'schema' not in structured:
        return False
    schema = structured['schema']
    if schema in ('reviewed_open_response_source_v1', 'reviewed_open_response_source_v2', 'reviewed_open_response_source_v3'):
        from .open_response_source import parse
    elif schema == 'pet_writing_source_v1':
        from .pet_writing_source import parse
    elif schema == 'pet_speaking_source_v1':
        from .pet_oral_source import parse
    elif schema in {'pet_listening_source_v1', 'school_listening_source_v1','pet_listening_source_v2','school_listening_source_v2'}:
        from .pet_reviewed_source import parse_listening_source as parse
    elif schema == 'curriculum_objective_source_v1':
        from .curriculum_objective_source import parse
    else:
        raise InvalidKnowledgeSourceError("不支持这种结构化题源版本，整批未保存。")
    from .local_supply import LocalSupplyError
    try:
        parse(content)
    except LocalSupplyError as error:
        raise InvalidKnowledgeSourceError("结构化题源不符合原题材料规范，未保存截断或无效资料。") from error
    return True


def source_lines(content):
    content = content.strip()
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        raise InvalidKnowledgeSourceError("资料内容不能为空。")
    if _typed_document(content):
        lines = [content]
    else:
        typed = [_typed_document(line) for line in lines]
        if any(typed) and not all(typed):
            raise InvalidKnowledgeSourceError("结构化题源不可混入缺损题目或普通索引。")
        if not any(typed) and content.startswith(('{', '[')):
            try:
                json.loads(content)
            except json.JSONDecodeError as error:
                raise InvalidKnowledgeSourceError("结构化资料JSON不完整，整批未保存。") from error
    if len(lines) > 500:
        raise InvalidKnowledgeSourceError("每批最多500条，整批未保存，不截断资料。")
    from .pet_reviewed_source import LISTENING_SCHEMAS,parse_listening_source,validate_listening_units
    from .local_supply import LocalSupplyError
    packets=[]
    for line in lines:
        try:
            packet=json.loads(line)
        except (ValueError,TypeError):
            continue
        if isinstance(packet,dict) and packet.get('schema') in LISTENING_SCHEMAS:
            packets.append(parse_listening_source(line))
    try:
        validate_listening_units(packets)
    except LocalSupplyError as error:
        raise InvalidKnowledgeSourceError('听力原题组声明缺失、重复或不一致，整批未保存。') from error
    return content, lines


class KnowledgeBaseService:
    def __init__(self, database: Database):
        self.database = database

    def list_sources(self, child_id: str) -> list[dict]:
        with self.database.connect() as connection:
            self._require_child(connection, child_id)
            rows = connection.execute(
                """
                SELECT ks.source_id, ks.child_id, ks.source_type, ks.title, ks.subject,
                       ks.grade_band, ks.publisher, ks.version_label, ks.source_ref,
                       ks.effective_from, ks.effective_to,
                       ks.license_status, ks.review_status, ks.content_hash, ks.imported_at,
                       ks.current_version, ks.predecessor_id,
                       COUNT(ke.excerpt_id) AS excerpt_count
                FROM knowledge_source ks
                LEFT JOIN knowledge_excerpt ke ON ke.source_id = ks.source_id
                WHERE ks.child_id = ?
                GROUP BY ks.source_id ORDER BY ks.imported_at DESC
                """,
                (child_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def import_source(self, child_id: str, values: dict, *, _connection=None) -> dict:
        source_type = values["source_type"]
        license_status = values["license_status"]
        if source_type not in ALLOWED_SOURCE_TYPES:
            raise InvalidKnowledgeSourceError("不支持这种资料类型。")
        if license_status not in ALLOWED_LICENSES:
            raise InvalidKnowledgeSourceError("不支持这种许可状态。")
        effective_from = values.get("effective_from")
        effective_to = values.get("effective_to")
        if effective_from and effective_to and effective_from > effective_to:
            raise InvalidKnowledgeSourceError("资料失效日期不能早于生效日期。")
        content, lines = source_lines(values["content_text"])
        review_status = "approved" if values.get("parent_reviewed") and license_status != "unknown" else "quarantined"
        now = utc_now()
        source_id = str(uuid4())
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with (self.database.connect() if _connection is None else nullcontext(_connection)) as connection:
            if _connection is None:
                connection.execute('BEGIN IMMEDIATE')
            self._require_child(connection, child_id)
            duplicate = connection.execute(
                "SELECT source_id FROM knowledge_source WHERE child_id = ? AND content_hash = ? AND current_version=1",
                (child_id, digest),
            ).fetchone()
            if duplicate:
                from .knowledge_source_revision import source_state, complete_excerpts
                _, excerpts, _ = source_state(connection, child_id, duplicate['source_id'])
                if not complete_excerpts(excerpts, lines):
                    error = InvalidKnowledgeSourceError('KNOWLEDGE_SOURCE_REVISION_REQUIRED')
                    error.source_id = duplicate['source_id']
                    raise error
                return self.get_source(duplicate["source_id"], _connection=connection)
            connection.execute(
                """
                INSERT INTO knowledge_source(
                  source_id, child_id, source_type, title, subject, grade_band, publisher,
                  version_label, source_ref, effective_from, effective_to, license_status,
                  review_status, content_hash, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id, child_id, source_type, values["title"].strip(), values["subject"],
                    values["grade_band"], (values.get("publisher") or "").strip() or None,
                    (values.get("version_label") or "").strip() or None,
                    (values.get("source_ref") or "").strip() or "family_manual_import",
                    effective_from, effective_to, license_status, review_status, digest, now,
                ),
            )
            for sequence, line in enumerate(lines[:500], start=1):
                stored_text = line
                connection.execute(
                    """
                    INSERT INTO knowledge_excerpt(
                      excerpt_id, source_id, sequence, heading, content_text, content_hash
                    ) VALUES (?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        str(uuid4()), source_id, sequence, stored_text,
                        hashlib.sha256(stored_text.encode("utf-8")).hexdigest(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'knowledge_source_imported', ?, ?, ?)
                """,
                (
                    str(uuid4()), child_id,
                    json.dumps({"source_id": source_id, "review_status": review_status, "line_count": min(len(lines), 500)}, ensure_ascii=False),
                    now,
                ),
            )
            if _connection is None:
                connection.commit()
            return self.get_source(source_id, _connection=connection)

    def get_source(self, source_id: str, *, _connection=None) -> dict:
        with (self.database.connect() if _connection is None else nullcontext(_connection)) as connection:
            row = connection.execute(
                """
                SELECT ks.*, COUNT(ke.excerpt_id) AS excerpt_count
                FROM knowledge_source ks LEFT JOIN knowledge_excerpt ke ON ke.source_id = ks.source_id
                WHERE ks.source_id = ? GROUP BY ks.source_id
                """,
                (source_id,),
            ).fetchone()
        if not row:
            raise UnknownKnowledgeSourceError(source_id)
        return dict(row)

    def list_corpora(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT cc.corpus_id, cc.title, cc.corpus_type, cc.source_ref,
                       cc.license_status, cc.review_status, cc.status,
                       cc.resource_count, cc.transcript_count, cc.local_video_count,
                       cc.last_error_code, cc.imported_at, cc.updated_at,
                       (SELECT COUNT(*) FROM curriculum_topic ct WHERE ct.corpus_id = cc.corpus_id) AS topic_count,
                       (SELECT COUNT(*) FROM curriculum_capability cap WHERE cap.corpus_id = cc.corpus_id AND cap.maturity = 'model_drafted') AS model_drafted_topic_count,
                       (SELECT COUNT(*) FROM curriculum_capability cap WHERE cap.corpus_id = cc.corpus_id AND cap.maturity = 'verified') AS verified_topic_count,
                       cij.status AS import_status, cij.total_count,
                       cij.processed_count, cij.error_code AS import_error_code
                FROM curriculum_corpus cc
                LEFT JOIN curriculum_corpus_import_job cij ON cij.job_id = (
                  SELECT job_id FROM curriculum_corpus_import_job
                  WHERE corpus_id = cc.corpus_id ORDER BY started_at DESC LIMIT 1
                )
                ORDER BY cc.imported_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def import_corpus(self, values: dict) -> dict:
        database_path = Path(values["database_path"]).expanduser().resolve()
        transcript_root = Path(values["transcript_root"]).expanduser().resolve()
        video_root = Path(values["video_root"]).expanduser().resolve()
        self._validate_corpus_paths(database_path, transcript_root, video_root)

        client_action_id = values["client_action_id"]
        with self.database.connect() as connection:
            existing_job = connection.execute(
                "SELECT corpus_id, status FROM curriculum_corpus_import_job WHERE client_action_id = ?",
                (client_action_id,),
            ).fetchone()
            if existing_job and existing_job["status"] == "completed":
                return self._get_corpus(existing_job["corpus_id"])
            existing_corpus = connection.execute(
                "SELECT corpus_id FROM curriculum_corpus WHERE source_database_path = ?",
                (str(database_path),),
            ).fetchone()
            corpus_id = existing_corpus["corpus_id"] if existing_corpus else str(uuid4())
            now = utc_now()
            if not existing_corpus:
                connection.execute(
                    """
                    INSERT INTO curriculum_corpus(
                      corpus_id, title, corpus_type, source_database_path,
                      transcript_root_path, video_root_path, source_ref,
                      license_status, review_status, status, imported_at, updated_at
                    ) VALUES (?, ?, 'seyk', ?, ?, ?, ?, 'public_personal_use',
                              'approved_baseline', 'importing', ?, ?)
                    """,
                    (
                        corpus_id, values["title"].strip(), str(database_path),
                        str(transcript_root), str(video_root),
                        "江苏中小学智慧教育平台/苏e优课公开课程", now, now,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE curriculum_corpus
                    SET title = ?, transcript_root_path = ?, video_root_path = ?,
                        status = 'importing', last_error_code = NULL, updated_at = ?
                    WHERE corpus_id = ?
                    """,
                    (values["title"].strip(), str(transcript_root), str(video_root), now, corpus_id),
                )
            if existing_job:
                job_id = connection.execute(
                    "SELECT job_id FROM curriculum_corpus_import_job WHERE client_action_id = ?",
                    (client_action_id,),
                ).fetchone()["job_id"]
                connection.execute(
                    "UPDATE curriculum_corpus_import_job SET status = 'processing', error_code = NULL WHERE job_id = ?",
                    (job_id,),
                )
            else:
                job_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO curriculum_corpus_import_job(
                      job_id, corpus_id, client_action_id, status, started_at
                    ) VALUES (?, ?, ?, 'processing', ?)
                    """,
                    (job_id, corpus_id, client_action_id, now),
                )
            connection.commit()

        try:
            source = self._open_source_database(database_path)
            try:
                source.row_factory = sqlite3.Row
                self._validate_source_schema(source)
                total = source.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE curriculum_corpus_import_job SET total_count = ? WHERE job_id = ?",
                        (total, job_id),
                    )
                    connection.commit()
                cursor = source.execute(
                    """
                    SELECT r.resource_id, COALESCE(r.title, '') AS title,
                           COALESCE(r.description, '') AS description, r.teacher,
                           COALESCE(t.stage_title, '') AS stage_title,
                           COALESCE(t.grade_title, '') AS grade_title,
                           COALESCE(t.subject_title, '') AS subject_title,
                           t.version_title, t.volumn_title,
                           COALESCE(t.unit_path, '') AS unit_path,
                           COALESCE(r.transcript, '') AS transcript_text,
                           r.video_duration
                    FROM resources r
                    LEFT JOIN resource_tree t ON t.resource_id = r.resource_id
                    ORDER BY CAST(r.resource_id AS INTEGER), r.resource_id
                    """
                )
                processed = 0
                while True:
                    rows = cursor.fetchmany(100)
                    if not rows:
                        break
                    with self.database.connect() as connection:
                        for row in rows:
                            self._upsert_curriculum_resource(
                                connection, corpus_id, job_id, row, transcript_root, video_root
                            )
                        processed += len(rows)
                        connection.execute(
                            "UPDATE curriculum_corpus_import_job SET processed_count = ? WHERE job_id = ?",
                            (processed, job_id),
                        )
                        connection.commit()
            finally:
                source.close()

            self._rebuild_curriculum_topics(corpus_id)
            source_hash = self._sha256_file(database_path)
            with self.database.connect() as connection:
                connection.execute(
                    "DELETE FROM curriculum_resource WHERE corpus_id = ? AND import_job_id != ?",
                    (corpus_id, job_id),
                )
                counts = connection.execute(
                    """
                    SELECT COUNT(*) AS resources,
                           SUM(CASE WHEN transcript_text != '' THEN 1 ELSE 0 END) AS transcripts,
                           SUM(local_video_available) AS videos
                    FROM curriculum_resource WHERE corpus_id = ?
                    """,
                    (corpus_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    UPDATE curriculum_corpus
                    SET status = 'ready', source_hash = ?, resource_count = ?,
                        transcript_count = ?, local_video_count = ?,
                        last_error_code = NULL, updated_at = ? WHERE corpus_id = ?
                    """,
                    (
                        source_hash, counts["resources"], counts["transcripts"] or 0,
                        counts["videos"] or 0, now, corpus_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE curriculum_corpus_import_job
                    SET status = 'completed', processed_count = ?, completed_at = ?
                    WHERE job_id = ?
                    """,
                    (counts["resources"], now, job_id),
                )
                connection.execute(
                    """
                    INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                    VALUES (?, 'parent', 'curriculum_corpus_imported', NULL, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        json.dumps(
                            {
                                "corpus_id": corpus_id,
                                "resource_count": counts["resources"],
                                "transcript_count": counts["transcripts"] or 0,
                                "local_video_count": counts["videos"] or 0,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                connection.commit()
            from .curriculum_standards import CurriculumStandardService
            CurriculumStandardService(self.database).rebuild_topic_candidates(corpus_id)
        except (OSError, sqlite3.Error, InvalidKnowledgeSourceError) as error:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE curriculum_corpus SET status = 'failed', last_error_code = 'INVALID_CURRICULUM_CORPUS', updated_at = ? WHERE corpus_id = ?",
                    (utc_now(), corpus_id),
                )
                connection.execute(
                    "UPDATE curriculum_corpus_import_job SET status = 'failed', error_code = 'INVALID_CURRICULUM_CORPUS', completed_at = ? WHERE job_id = ?",
                    (utc_now(), job_id),
                )
                connection.commit()
            if isinstance(error, InvalidKnowledgeSourceError):
                raise
            raise InvalidKnowledgeSourceError("课程库无法读取或结构不受支持。") from error
        return self._get_corpus(corpus_id)

    def search_corpus(
        self, child_id: str, query: str, subject: str | None = None, limit: int = 10
    ) -> list[dict]:
        query = query.strip()
        if not query:
            return []
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            clauses = ["cc.status = 'ready'", "cr.grade_title = ?"]
            parameters: list[object] = [child["grade"]]
            from .curriculum_selection import current_curriculum_predicate
            current, current_params = current_curriculum_predicate(connection, child_id, "cr")
            clauses.append(current)
            parameters.extend(current_params)
            if subject:
                clauses.append("cr.subject_title LIKE ?")
                parameters.append(f"%{subject.strip()}%")
            if len(query) >= 3:
                clauses.append("curriculum_resource_fts MATCH ?")
                parameters.append(f'"{query.replace(chr(34), chr(34) + chr(34))}"')
                ranking = "bm25(curriculum_resource_fts)"
                join = "JOIN curriculum_resource_fts ON curriculum_resource_fts.rowid = cr.row_id"
            else:
                clauses.append("(cr.title LIKE ? OR cr.description LIKE ? OR cr.transcript_text LIKE ? OR cr.unit_path LIKE ?)")
                parameters.extend([f"%{query}%"] * 4)
                ranking = "0"
                join = ""
            parameters.append(max(1, min(limit, 30)))
            rows = connection.execute(
                f"""
                SELECT cr.external_resource_id, cr.title, cr.description, cr.teacher,
                       cr.stage_title, cr.grade_title, cr.subject_title,
                       cr.version_title, cr.volume_title, cr.unit_path,
                       cr.transcript_text, cr.video_relative_path, cr.video_duration,
                       cr.local_video_available, cc.corpus_id, cc.title AS corpus_title,
                       cc.source_ref, cc.video_root_path
                FROM curriculum_resource cr
                JOIN curriculum_corpus cc ON cc.corpus_id = cr.corpus_id
                {join}
                WHERE {' AND '.join(clauses)}
                ORDER BY {ranking}, cr.title
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._serialize_search_result(row, query) for row in rows]

    def grounding_context(
        self, child_id: str, subject: str, text: str, limit: int = 2
    ) -> list[dict]:
        grams = self._text_ngrams(text)
        if not grams:
            return []
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            from .curriculum_selection import current_curriculum_predicate
            current, current_params = current_curriculum_predicate(connection, child_id, "cr")
            rows = connection.execute(
                f"""
                SELECT cr.corpus_id, cr.external_resource_id, cr.title, cr.description,
                       cr.grade_title, cr.subject_title, cr.version_title, cr.volume_title,
                       cr.unit_path, substr(cr.transcript_text, 1, 2600) AS transcript_text,
                       cc.source_ref
                FROM curriculum_resource cr
                JOIN curriculum_corpus cc ON cc.corpus_id = cr.corpus_id
                WHERE cc.status = 'ready' AND cr.grade_title = ?
                  AND cr.subject_title LIKE ?
                  AND {current}
                ORDER BY cr.title
                """,
                (child["grade"], f"%{subject.strip()}%", *current_params),
            ).fetchall()
        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            title = str(row["title"] or "")
            searchable = " ".join(
                str(row[key] or "")
                for key in ("title", "description", "unit_path", "transcript_text")
            )
            matches = [gram for gram in grams if len(gram) >= 3 and gram in searchable]
            score = sum((6 if gram in title else 2) + len(gram) for gram in matches)
            if any(len(gram) == 4 for gram in matches) and score >= 7:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["title"]))
        results = []
        for score, row in scored[:max(1, min(limit, 5))]:
            evidence = row["transcript_text"] or row["description"] or row["title"]
            matching = next((gram for gram in sorted(grams, key=len, reverse=True) if gram in evidence), "")
            index = evidence.find(matching) if matching else 0
            excerpt = evidence[max(0, index - 80): index + max(len(matching), 1) + 420]
            results.append({
                "evidence_ref": f"curriculum:{row['corpus_id']}:{row['external_resource_id']}",
                "title": row["title"],
                "grade_title": row["grade_title"],
                "subject_title": row["subject_title"],
                "version_title": row["version_title"],
                "volume_title": row["volume_title"],
                "unit_path": row["unit_path"],
                "excerpt": excerpt,
                "source_ref": row["source_ref"],
                "match_score": score,
            })
        return results

    def coverage(self, corpus_id: str) -> list[dict]:
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM curriculum_corpus WHERE corpus_id = ?", (corpus_id,)
            ).fetchone():
                raise UnknownKnowledgeSourceError(corpus_id)
            rows = connection.execute(
                """
                SELECT grade_title, subject_title, COALESCE(version_title, '未标版本') AS version_title,
                       semester, COUNT(*) AS topic_count,
                       SUM(source_count) AS resource_count,
                       SUM(transcript_count) AS transcript_count,
                       (SELECT COUNT(*) FROM curriculum_capability cap
                        WHERE cap.corpus_id = ? AND cap.grade_title = ct.grade_title
                          AND cap.subject_title = ct.subject_title
                          AND COALESCE(cap.version_title, '') = COALESCE(ct.version_title, '')
                          AND cap.semester = ct.semester AND cap.maturity = 'model_drafted') AS model_drafted_topic_count,
                       (SELECT COUNT(*) FROM curriculum_capability cap
                        WHERE cap.corpus_id = ? AND cap.grade_title = ct.grade_title
                          AND cap.subject_title = ct.subject_title
                          AND COALESCE(cap.version_title, '') = COALESCE(ct.version_title, '')
                          AND cap.semester = ct.semester AND cap.maturity = 'verified') AS verified_topic_count
                FROM curriculum_topic ct WHERE corpus_id = ?
                GROUP BY grade_title, subject_title, version_title, semester
                ORDER BY CASE grade_title
                  WHEN '四年级' THEN 4 WHEN '五年级' THEN 5 WHEN '六年级' THEN 6
                  WHEN '初一' THEN 7 WHEN '初二' THEN 8 WHEN '初三' THEN 9 ELSE 99 END,
                  subject_title, version_title, semester
                """,
                (corpus_id, corpus_id, corpus_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_topics(
        self, child_id: str, query: str | None = None, subject: str | None = None, limit: int = 50
    ) -> list[dict]:
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            clauses = ["ct.grade_title = ?", "cc.status = 'ready'"]
            parameters: list[object] = [child["grade"]]
            from .curriculum_selection import current_curriculum_predicate
            current, current_params = current_curriculum_predicate(connection, child_id, "ct")
            clauses.append(current)
            parameters.extend(current_params)
            if subject:
                clauses.append("ct.subject_title LIKE ?")
                parameters.append(f"%{subject.strip()}%")
            if query:
                clauses.append("(ct.title LIKE ? OR ct.learning_objective LIKE ? OR ct.unit_title LIKE ?)")
                parameters.extend([f"%{query.strip()}%"] * 3)
            parameters.append(max(1, min(limit, 200)))
            rows = connection.execute(
                f"""
                SELECT ct.topic_id, ct.grade_title, ct.subject_title, ct.version_title,
                       ct.semester, ct.unit_title, ct.title, ct.lesson_kind,
                       ct.learning_objective, ct.maturity, ct.knowledge_json,
                       ct.source_count, ct.transcript_count, cc.source_ref
                FROM curriculum_topic ct
                JOIN curriculum_corpus cc ON cc.corpus_id = ct.corpus_id
                WHERE {' AND '.join(clauses)}
                ORDER BY ct.subject_title, ct.semester, ct.unit_title, ct.title LIMIT ?
                """,
                parameters,
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["knowledge"] = json.loads(item.pop("knowledge_json") or "{}")
            items.append(item)
        return items

    def delete_corpus(self, corpus_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT resource_count FROM curriculum_corpus WHERE corpus_id = ?", (corpus_id,)
            ).fetchone()
            if not row:
                raise UnknownKnowledgeSourceError(corpus_id)
            resource_count = row["resource_count"]
            connection.execute("DELETE FROM curriculum_corpus WHERE corpus_id = ?", (corpus_id,))
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'parent', 'curriculum_corpus_deleted', NULL, ?, ?)
                """,
                (
                    str(uuid4()), json.dumps({"corpus_id": corpus_id, "resource_count": resource_count}), utc_now(),
                ),
            )
            connection.commit()
        return {"status": "deleted", "resource_count": resource_count}

    def _get_corpus(self, corpus_id: str) -> dict:
        items = self.list_corpora()
        for item in items:
            if item["corpus_id"] == corpus_id:
                return item
        raise UnknownKnowledgeSourceError(corpus_id)

    @staticmethod
    def _validate_corpus_paths(database_path: Path, transcript_root: Path, video_root: Path) -> None:
        if not database_path.is_file() or database_path.suffix.lower() != ".db":
            raise InvalidKnowledgeSourceError("请选择有效的课程库数据库。")
        if not transcript_root.is_dir() or not video_root.is_dir():
            raise InvalidKnowledgeSourceError("转录或视频目录不存在。")

    @staticmethod
    def _open_source_database(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)

    @staticmethod
    def _validate_source_schema(connection: sqlite3.Connection) -> None:
        required = {
            "resources": {"resource_id", "title", "description", "teacher", "transcript", "video_duration"},
            "resource_tree": {
                "resource_id", "stage_title", "grade_title", "subject_title",
                "version_title", "volumn_title", "unit_path",
            },
        }
        for table, columns in required.items():
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(actual):
                raise InvalidKnowledgeSourceError("课程库结构不受支持。")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise InvalidKnowledgeSourceError("课程库完整性检查失败。")

    @staticmethod
    def _upsert_curriculum_resource(
        connection: sqlite3.Connection,
        corpus_id: str,
        job_id: str,
        row: sqlite3.Row,
        transcript_root: Path,
        video_root: Path,
    ) -> None:
        resource_id = str(row["resource_id"])
        transcript = str(row["transcript_text"] or "").strip()
        semester, _ = KnowledgeBaseService._semester_and_unit(
            str(row["unit_path"] or ""), str(row["volumn_title"] or "")
        )
        transcript_relative = f"{resource_id}.txt" if (transcript_root / f"{resource_id}.txt").is_file() else None
        video_relative = f"{resource_id}.mp4" if (video_root / f"{resource_id}.mp4").is_file() else None
        connection.execute(
            """
            INSERT INTO curriculum_resource(
              corpus_id, external_resource_id, title, description, teacher,
              stage_title, grade_title, subject_title, version_title, volume_title, semester,
              unit_path, transcript_text, transcript_hash, transcript_relative_path,
              video_relative_path, video_duration, local_video_available, import_job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(corpus_id, external_resource_id) DO UPDATE SET
              title = excluded.title, description = excluded.description, teacher = excluded.teacher,
              stage_title = excluded.stage_title, grade_title = excluded.grade_title,
              subject_title = excluded.subject_title, version_title = excluded.version_title,
              volume_title = excluded.volume_title, semester = excluded.semester, unit_path = excluded.unit_path,
              transcript_text = excluded.transcript_text, transcript_hash = excluded.transcript_hash,
              transcript_relative_path = excluded.transcript_relative_path,
              video_relative_path = excluded.video_relative_path, video_duration = excluded.video_duration,
              local_video_available = excluded.local_video_available, import_job_id = excluded.import_job_id
            """,
            (
                corpus_id, resource_id, str(row["title"] or "未命名课程"),
                str(row["description"] or ""), row["teacher"], row["stage_title"],
                row["grade_title"], row["subject_title"], row["version_title"],
                row["volumn_title"], semester, row["unit_path"], transcript,
                hashlib.sha256(transcript.encode("utf-8")).hexdigest() if transcript else None,
                transcript_relative, video_relative, row["video_duration"],
                int(video_relative is not None), job_id,
            ),
        )

    def _rebuild_curriculum_topics(self, corpus_id: str) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT row_id, grade_title, subject_title, version_title, volume_title,
                       semester, unit_path, title, description,
                       CASE WHEN transcript_text != '' THEN 1 ELSE 0 END AS has_transcript
                FROM curriculum_resource WHERE corpus_id = ? ORDER BY row_id
                """,
                (corpus_id,),
            ).fetchall()
            connection.execute(
                "DELETE FROM curriculum_resource_topic WHERE row_id IN (SELECT row_id FROM curriculum_resource WHERE corpus_id = ?)",
                (corpus_id,),
            )
            for row in rows:
                semester, unit_title = self._semester_and_unit(row["unit_path"], row["volume_title"])
                normalized_title = self._normalize_topic_title(row["title"])
                lesson_kind = self._lesson_kind(row["title"])
                key = "|".join(
                    str(value or "")
                    for value in (
                        corpus_id, row["grade_title"], row["subject_title"], row["version_title"],
                        semester, unit_title, normalized_title, lesson_kind,
                    )
                )
                topic_id = f"topic-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"
                objective = str(row["description"] or "").strip() or f"学习并能说明：{normalized_title}。"
                connection.execute(
                    """
                    INSERT INTO curriculum_topic(
                      topic_id, corpus_id, grade_title, subject_title, version_title,
                      semester, unit_title, normalized_title, title, lesson_kind,
                      learning_objective, maturity, knowledge_json,
                      source_count, transcript_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'source_structured', '{}', 0, 0, ?, ?)
                    ON CONFLICT(topic_id) DO UPDATE SET
                      title = excluded.title,
                      learning_objective = CASE
                        WHEN curriculum_topic.maturity = 'source_structured' THEN excluded.learning_objective
                        ELSE curriculum_topic.learning_objective END,
                      updated_at = excluded.updated_at
                    """,
                    (
                        topic_id, corpus_id, row["grade_title"], row["subject_title"],
                        row["version_title"], semester, unit_title, normalized_title,
                        normalized_title, lesson_kind, objective, now, now,
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO curriculum_resource_topic(row_id, topic_id) VALUES (?, ?)",
                    (row["row_id"], topic_id),
                )
            connection.execute(
                """
                UPDATE curriculum_topic
                SET source_count = (
                      SELECT COUNT(*) FROM curriculum_resource_topic crt WHERE crt.topic_id = curriculum_topic.topic_id
                    ),
                    transcript_count = (
                      SELECT COALESCE(SUM(CASE WHEN cr.transcript_text != '' THEN 1 ELSE 0 END), 0)
                      FROM curriculum_resource_topic crt
                      JOIN curriculum_resource cr ON cr.row_id = crt.row_id
                      WHERE crt.topic_id = curriculum_topic.topic_id
                    ),
                    updated_at = ?
                WHERE corpus_id = ?
                """,
                (now, corpus_id),
            )
            connection.execute(
                "DELETE FROM curriculum_topic WHERE corpus_id = ? AND source_count = 0",
                (corpus_id,),
            )
            connection.commit()

    @staticmethod
    def _semester_and_unit(unit_path: str, volume_title: str) -> tuple[str, str]:
        parts = [part.strip() for part in str(unit_path or "").split("/") if part.strip()]
        if parts and parts[0] in {"上册", "下册", "全册"}:
            semester = parts[0]
            unit_title = parts[1] if len(parts) > 1 else parts[0]
        else:
            semester = volume_title if volume_title in {"上册", "下册", "全册"} else "未知学期"
            unit_title = parts[0] if parts else "未标单元"
        return semester, unit_title

    @staticmethod
    def _normalize_topic_title(title: str) -> str:
        value = re.sub(r"[（(]?第?[一二三四五六七八九十0-9]+课时[)）]?", "", str(title or ""))
        value = re.sub(r"\s+", " ", value).strip(" -—·：:（）()")
        return value or str(title or "未命名课程").strip() or "未命名课程"

    @staticmethod
    def _lesson_kind(title: str) -> str:
        value = str(title or "")
        if any(word in value for word in ("实验", "探究")):
            return "experiment"
        if any(word in value for word in ("活动", "综合实践", "制作")):
            return "activity"
        if any(word in value for word in ("复习", "整理")):
            return "review"
        if any(word in value for word in ("练习", "测试")):
            return "practice"
        return "instruction"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _serialize_search_result(row: sqlite3.Row, query: str) -> dict:
        evidence = row["transcript_text"] or row["description"] or row["title"]
        index = evidence.find(query)
        if index < 0:
            excerpt = evidence[:240]
        else:
            excerpt = evidence[max(0, index - 90): index + len(query) + 150]
        video_path = None
        video_available = bool(row["local_video_available"] and row["video_relative_path"])
        if video_available:
            candidate = Path(row["video_root_path"]) / row["video_relative_path"]
            video_available = candidate.is_file()
            if video_available:
                video_path = str(candidate)
        return {
            key: row[key]
            for key in (
                "corpus_id", "corpus_title", "external_resource_id", "title", "description",
                "teacher", "stage_title", "grade_title", "subject_title", "version_title",
                "volume_title", "unit_path", "video_duration", "source_ref",
            )
        } | {
            "excerpt": excerpt,
            "video_available": video_available,
            "video_path": video_path,
        }

    @staticmethod
    def _text_ngrams(text: str) -> set[str]:
        compact = "".join(character for character in text if character.isalnum())
        for phrase in ("为什么", "怎么样", "怎么", "怎样", "如何", "请问", "是不是", "能不能", "是否", "这道题"):
            compact = compact.replace(phrase, "")
        compact = "".join(character for character in compact if character not in "的了呢吗吧呀啊把要时")
        ignored = {"今天", "老师", "学习", "讲了", "内容", "怎么", "这道", "题目", "请问"}
        return {
            compact[index:index + size]
            for size in (2, 3, 4)
            for index in range(max(0, len(compact) - size + 1))
            if compact[index:index + size] not in ignored
        }

    @staticmethod
    def _require_child(connection, child_id: str) -> None:
        if not connection.execute("SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)).fetchone():
            raise UnknownChildError(child_id)


class InvalidKnowledgeSourceError(Exception):
    pass


class UnknownKnowledgeSourceError(Exception):
    pass
