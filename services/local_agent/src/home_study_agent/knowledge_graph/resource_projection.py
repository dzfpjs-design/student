from __future__ import annotations

import hashlib
import json

from ..database import Database
from ..repository import utc_now
from .schema import ensure_schema


def _id(prefix: str, key: str) -> str:
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


class ResourceGraphProjector:
    """Materialize real local resources as graph nodes without inventing course facts."""
    def __init__(self, database: Database):
        self.database = database

    def run(self) -> dict:
        now = utc_now(); projected = subjects = grades = stages = semesters = 0
        with self.database.connect() as connection:
            ensure_schema(connection, now); connection.commit(); connection.execute("BEGIN IMMEDIATE")
            resources = connection.execute("SELECT * FROM kg_resource").fetchall()
            for resource in resources:
                grade, subject, semester = resource["grade_scope"], resource["subject_scope"], resource["semester_scope"]
                stage_id = grade_id = subject_id = semester_id = None
                stage = self._stage_for_grade(grade)
                if stage:
                    stage_key = f"scope:stage:{stage}"
                    stage_id = _id("kg-node", stage_key)
                    connection.execute("""INSERT OR IGNORE INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,source_time,version_label,license_boundary,confidence,review_status,status,created_at,updated_at)
                    VALUES(?, 'stage', ?, ?, '', ?, 'local_metadata', NULL, 'local-2026-08', 'local_metadata', .7, 'unreviewed', 'active', ?, ?)""",
                    (stage_id,stage_key,stage,json.dumps({"stage":stage},ensure_ascii=False),now,now)); stages += connection.execute("SELECT changes()").fetchone()[0]
                if grade:
                    grade_key = f"scope:grade:{grade}"
                    grade_id = _id("kg-node", grade_key)
                    connection.execute("""INSERT OR IGNORE INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,source_time,version_label,license_boundary,confidence,review_status,status,created_at,updated_at)
                    VALUES(?, 'grade', ?, ?, '', ?, 'local_metadata', NULL, 'local-2026-08', 'local_metadata', .7, 'unreviewed', 'active', ?, ?)""",
                    (grade_id,grade_key,grade,json.dumps({"grade":grade},ensure_ascii=False),now,now)); grades += connection.execute("SELECT changes()").fetchone()[0]
                if subject:
                    subject_key = f"scope:subject:{grade or 'unknown'}:{subject}"
                    subject_id = _id("kg-node", subject_key)
                    connection.execute("""INSERT OR IGNORE INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,source_time,version_label,license_boundary,confidence,review_status,status,created_at,updated_at)
                    VALUES(?, 'subject', ?, ?, '', ?, 'local_metadata', NULL, 'local-2026-08', 'local_metadata', .7, 'unreviewed', 'active', ?, ?)""",
                    (subject_id,subject_key,subject,json.dumps({"grade":grade,"subject":subject},ensure_ascii=False),now,now)); subjects += connection.execute("SELECT changes()").fetchone()[0]
                if semester:
                    semester_key = f"scope:semester:{grade or 'unknown'}:{subject or 'unknown'}:{semester}"
                    semester_id = _id("kg-node", semester_key)
                    connection.execute("""INSERT OR IGNORE INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,source_time,version_label,license_boundary,confidence,review_status,status,created_at,updated_at)
                    VALUES(?, 'semester', ?, ?, '', ?, 'local_metadata', NULL, 'local-2026-08', 'local_metadata', .7, 'unreviewed', 'active', ?, ?)""",
                    (semester_id,semester_key,semester,json.dumps({"grade":grade,"subject":subject,"semester":semester},ensure_ascii=False),now,now)); semesters += connection.execute("SELECT changes()").fetchone()[0]
                resource_key = f"local-resource:{resource['corpus_id']}:{resource['external_id']}"
                resource_node_id = _id("kg-node", resource_key)
                connection.execute("""INSERT INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,source_time,version_label,license_boundary,confidence,review_status,status,created_at,updated_at)
                VALUES(?, 'explanation', ?, ?, ?, ?, ?, NULL, ?, ?, .55, 'restricted', 'active', ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET title=excluded.title,scope_json=excluded.scope_json,source_ref=excluded.source_ref,updated_at=excluded.updated_at""",
                (resource_node_id,resource_key,resource["title"],f"本地课程资源 {resource['external_id']}",json.dumps({"grade":grade,"subject":subject,"semester":semester,"resource_id":resource['resource_id']},ensure_ascii=False),resource["source_ref"],resource["version_label"],resource["license_boundary"],now,now)); projected += connection.execute("SELECT changes()").fetchone()[0]
                connection.execute("""INSERT OR IGNORE INTO kg_resource_node(resource_id,segment_id,node_id,relation_type,confidence,review_status,source_ref,created_at)
                VALUES(?,NULL,?,'explains',.55,'restricted',?,?)""",(resource["resource_id"],resource_node_id,resource["source_ref"],now))
                if stage_id and grade_id:
                    self._edge(connection, stage_id, grade_id, "contains", "年级对应的学段范围", now)
                if grade_id and subject_id:
                    self._edge(connection, grade_id, subject_id, "contains", "资源元数据中的年级/学科范围", now)
                if subject_id and semester_id:
                    self._edge(connection, subject_id, semester_id, "contains", "资源元数据中的学期范围", now)
                if semester_id:
                    self._edge(connection, semester_id, resource_node_id, "contains", "资源元数据中的学期范围", now)
                elif subject_id:
                    self._edge(connection, subject_id, resource_node_id, "contains", "资源元数据中的学科范围", now)
            connection.commit()
        return {"resource_nodes": projected, "stage_nodes": stages, "grade_nodes": grades, "subject_nodes": subjects, "semester_nodes": semesters}

    @staticmethod
    def _stage_for_grade(grade: str | None) -> str | None:
        if grade in {"四年级", "五年级", "六年级"}: return "小学"
        if grade in {"初一", "初二", "初三"}: return "初中"
        return None

    @staticmethod
    def _edge(connection, source: str, target: str, relation: str, rationale: str, now: str) -> None:
        key=f"{source}:{target}:{relation}:local-2026-08"
        connection.execute("""INSERT OR IGNORE INTO kg_edge(edge_id,source_node_id,target_node_id,relation_type,rationale,source_ref,version_label,confidence,review_status,created_at,updated_at)
        VALUES(?,?,?,?,?,'local_metadata','local-2026-08',.7,'unreviewed',?,?)""",(_id("kg-edge",key),source,target,relation,rationale,now,now))
