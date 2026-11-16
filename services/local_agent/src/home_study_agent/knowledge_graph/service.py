from __future__ import annotations

import json
import sqlite3
from collections import deque
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..database import Database
from ..repository import utc_now
from ..retrieval.hybrid import hybrid_search
from .schema import ensure_schema


class KnowledgeGraphChildNotFoundError(ValueError):
    """Graph mastery evidence may only be attached to an active local child profile."""


class _CatalogWriter:
    def __init__(self, service, connection):
        self.service, self.connection = service, connection

    def upsert_node(self, **values):
        if self.connection is None:
            raise RuntimeError('CATALOG_BATCH_CLOSED')
        return self.service._upsert_node(self.connection, **values)

    def link(self, *args, **values):
        if self.connection is None:
            raise RuntimeError('CATALOG_BATCH_CLOSED')
        return self.service._link(self.connection, *args, **values)


class KnowledgeGraphService:
    """Read/write facade for graph traversal, evidence, coverage and local retrieval."""

    def __init__(self, database: Database):
        self.database = database

    def ensure_ready(self) -> None:
        with self.database.connect() as connection:
            ensure_schema(connection, utc_now())
            connection.commit()

    @contextmanager
    def catalog_batch(self):
        # Only this newly owned writer can amortize initialization. No ready
        # flag survives it; restored files and every later batch run the schema.
        if type(self.database) is not Database:
            yield self
            return
        with self.database.connect() as connection:
            ensure_schema(connection, utc_now())
            connection.commit()
            connection.execute('BEGIN IMMEDIATE')
            batch = _CatalogWriter(self, connection)
            try:
                yield batch
                connection.commit()
            finally:
                batch.connection = None

    def upsert_node(self, *, canonical_key: str, node_type: str, title: str, description: str = "",
                    scope: dict | None = None, source_ref: str, version_label: str = "v1",
                    license_boundary: str = "unknown", confidence: float = 0.5,
                    review_status: str = "unreviewed", source_time: str | None = None) -> str:
        self.ensure_ready()
        with self.database.connect() as connection:
            node_id = self._upsert_node(connection, canonical_key=canonical_key, node_type=node_type,
                title=title, description=description, scope=scope, source_ref=source_ref,
                version_label=version_label, license_boundary=license_boundary, confidence=confidence,
                review_status=review_status, source_time=source_time)
            connection.commit()
        return node_id

    def _upsert_node(self, connection, *, canonical_key: str, node_type: str, title: str, description: str = "",
                     scope: dict | None = None, source_ref: str, version_label: str = "v1",
                     license_boundary: str = "unknown", confidence: float = 0.5,
                     review_status: str = "unreviewed", source_time: str | None = None) -> str:
        now = utc_now()
        existing = connection.execute("SELECT node_id FROM kg_node WHERE canonical_key=?", (canonical_key,)).fetchone()
        node_id = existing["node_id"] if existing else str(uuid4())
        connection.execute(
            """INSERT INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,source_time,version_label,license_boundary,confidence,review_status,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'active',?,?)
                   ON CONFLICT(canonical_key) DO UPDATE SET
                   title=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.title ELSE excluded.title END,
                   description=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.description ELSE excluded.description END,
                   scope_json=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.scope_json ELSE excluded.scope_json END,
                   source_ref=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.source_ref ELSE excluded.source_ref END,
                   source_time=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.source_time ELSE excluded.source_time END,
                   version_label=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.version_label ELSE excluded.version_label END,
                   license_boundary=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.license_boundary ELSE excluded.license_boundary END,
                   confidence=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN kg_node.confidence ELSE excluded.confidence END,
                   review_status=CASE WHEN kg_node.review_status='approved' AND excluded.review_status!='approved' THEN 'approved' ELSE excluded.review_status END,
                   updated_at=excluded.updated_at""",
            (node_id,node_type,canonical_key,title,description,json.dumps(scope or {},ensure_ascii=False),source_ref,source_time,version_label,license_boundary,confidence,review_status,now,now),
        )
        return node_id

    def link(self, source_node_id: str, target_node_id: str, relation_type: str, *, rationale: str,
             source_ref: str, version_label: str = "v1", confidence: float = 0.5,
             review_status: str = "unreviewed") -> None:
        self.ensure_ready()
        with self.database.connect() as connection:
            self._link(connection, source_node_id, target_node_id, relation_type, rationale=rationale,
                source_ref=source_ref, version_label=version_label, confidence=confidence, review_status=review_status)
            connection.commit()

    def _link(self, connection, source_node_id: str, target_node_id: str, relation_type: str, *, rationale: str,
              source_ref: str, version_label: str = "v1", confidence: float = 0.5,
              review_status: str = "unreviewed") -> None:
        now = utc_now()
        connection.execute(
            """INSERT INTO kg_edge(edge_id,source_node_id,target_node_id,relation_type,rationale,source_ref,version_label,confidence,review_status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_node_id,target_node_id,relation_type,version_label)
                   DO UPDATE SET rationale=excluded.rationale,source_ref=excluded.source_ref,confidence=excluded.confidence,review_status=excluded.review_status,updated_at=excluded.updated_at""",
            (str(uuid4()),source_node_id,target_node_id,relation_type,rationale,source_ref,version_label,confidence,review_status,now,now),
        )

    def map_segment(self, resource_id: str, segment_id: str | None, node_id: str, relation_type: str,
                    *, confidence: float, source_ref: str, review_status: str = "unreviewed") -> None:
        self.ensure_ready()
        with self.database.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO kg_resource_node(resource_id,segment_id,node_id,relation_type,confidence,review_status,source_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                               (resource_id,segment_id,node_id,relation_type,confidence,review_status,source_ref,utc_now()))
            connection.commit()

    def prerequisites(self, node_id: str, max_depth: int = 3, *, approved_only: bool = False) -> list[dict]:
        """Return direct/indirect prerequisites; prerequisite_of points prerequisite -> dependent."""
        self.ensure_ready()
        with self.database.connect() as connection:
            edge_filter = " AND review_status='approved'" if approved_only else ""
            node_filter = " AND n.review_status='approved' AND n.status='active'" if approved_only else ""
            rows = connection.execute(f"""WITH RECURSIVE walk(node_id,depth,path) AS (
                SELECT source_node_id,1,source_node_id FROM kg_edge WHERE target_node_id=? AND relation_type='prerequisite_of'{edge_filter}
                UNION ALL SELECT e.source_node_id,walk.depth+1,walk.path||','||e.source_node_id
                FROM kg_edge e JOIN walk ON e.target_node_id=walk.node_id
                WHERE e.relation_type='prerequisite_of'{edge_filter} AND walk.depth < ? AND instr(walk.path,e.source_node_id)=0)
                SELECT DISTINCT n.node_id,n.title,n.node_type,walk.depth FROM walk JOIN kg_node n ON n.node_id=walk.node_id WHERE 1=1{node_filter} ORDER BY walk.depth,n.title""", (node_id,max_depth)).fetchall()
        return [dict(row) for row in rows]

    def search_explanations(self, query: str, *, subject: str | None = None, grade: str | None = None, limit: int = 8) -> list[dict]:
        return self.search_explanations_with_policy(query, subject=subject, grade=grade, limit=limit)

    def search_explanations_with_policy(self, query: str, *, subject: str | None = None, grade: str | None = None,
                                        limit: int = 8, approved_only: bool = False) -> list[dict]:
        self.ensure_ready()
        with self.database.connect() as connection:
            return hybrid_search(connection, query, subject=subject, grade=grade, limit=limit, approved_only=approved_only)

    def explanation_materials(self, node_id: str, limit: int = 8, *, approved_only: bool = False) -> list[dict]:
        """Locate approved/candidate local explanation segments for a diagnosed node.

        This path intentionally uses graph mappings rather than grade metadata, so
        source material with incomplete vendor metadata remains useful after review.
        """
        self.ensure_ready()
        statuses = ("approved",) if approved_only else ("unreviewed", "approved")
        status_placeholders = ",".join("?" for _ in statuses)
        ordering = "rn.confidence DESC,r.title" if approved_only else "rn.review_status='approved' DESC,rn.confidence DESC,r.title"
        with self.database.connect() as connection:
            rows = connection.execute(f"""SELECT s.segment_id,s.text,s.start_char,s.end_char,r.resource_id,r.title,
                r.subject_scope,r.grade_scope,r.transcript_relative_path,r.video_relative_path,r.source_ref,r.license_boundary,
                c.transcript_root AS _transcript_root,c.video_root AS _video_root,
                rn.confidence,rn.review_status,rn.source_ref AS mapping_source_ref
                FROM kg_resource_node rn JOIN kg_segment s ON s.segment_id=rn.segment_id
                JOIN kg_resource r ON r.resource_id=rn.resource_id JOIN kg_corpus c ON c.corpus_id=r.corpus_id
                WHERE rn.node_id=? AND rn.relation_type IN ('covers','explains')
                AND rn.review_status IN ({status_placeholders})
                ORDER BY {ordering} LIMIT ?""", (node_id, *statuses, max(1,min(limit,30)))).fetchall()
            if not rows:
                # Scope nodes (stage/grade/subject/semester) reach resources through
                # real `contains` edges rather than pretending every segment is an
                # atomic-knowledge-point match.
                scope_status = "'approved'" if approved_only else "'unreviewed','approved'"
                scope_review_status = "'approved'" if approved_only else "'unreviewed'"
                rows = connection.execute(f"""WITH RECURSIVE walk(node_id,depth,path) AS (
                    SELECT ?,0,? UNION ALL SELECT e.target_node_id,walk.depth+1,walk.path||','||e.target_node_id
                    FROM kg_edge e JOIN walk ON e.source_node_id=walk.node_id
                    WHERE e.relation_type='contains' AND walk.depth<4 AND instr(walk.path,e.target_node_id)=0)
                    SELECT DISTINCT s.segment_id,s.text,s.start_char,s.end_char,r.resource_id,r.title,r.subject_scope,r.grade_scope,
                    r.transcript_relative_path,r.video_relative_path,r.source_ref,r.license_boundary,c.transcript_root AS _transcript_root,
                    c.video_root AS _video_root,.7 confidence,{scope_review_status} review_status,binding.source_ref AS mapping_source_ref
                    FROM walk JOIN kg_resource_node binding ON binding.node_id=walk.node_id AND binding.segment_id IS NULL
                    AND binding.review_status IN ({scope_status})
                    JOIN kg_segment s ON s.resource_id=binding.resource_id JOIN kg_resource r ON r.resource_id=s.resource_id
                    JOIN kg_corpus c ON c.corpus_id=r.corpus_id
                    ORDER BY r.title LIMIT ?""",(node_id,node_id,max(1,min(limit,30)))).fetchall()
        return [self._with_media_status(dict(row)) for row in rows]

    def similar_exercises(self, node_id: str, limit: int = 3, *, approved_only: bool = False) -> list[dict]:
        """Retrieve only graph-grounded similar exercises, never fabricated variants.

        A direct `assessed_by` edge means the item assesses the same knowledge point.
        A one-hop `related_to` edge admits a clearly labelled neighbouring concept.
        """
        self.ensure_ready()
        with self.database.connect() as connection:
            rows=connection.execute("""WITH targets(node_id,distance,basis) AS (
                    SELECT ?,0,'same_knowledge_point'
                    UNION
                    SELECT CASE WHEN e.source_node_id=? THEN e.target_node_id ELSE e.source_node_id END,1,'explicit_related_knowledge_point'
                    FROM kg_edge e WHERE e.relation_type='related_to' AND (? IN (e.source_node_id,e.target_node_id))
                      AND (?=0 OR e.review_status='approved'))
                SELECT DISTINCT exercise.node_id,exercise.canonical_key,exercise.title,exercise.description,exercise.source_ref,
                    exercise.review_status,targets.node_id AS source_knowledge_node_id,targets.distance,targets.basis
                FROM targets JOIN kg_edge assessed ON assessed.source_node_id=targets.node_id AND assessed.relation_type='assessed_by'
                JOIN kg_node exercise ON exercise.node_id=assessed.target_node_id AND exercise.node_type='exercise'
                WHERE (?=0 OR (exercise.review_status='approved' AND assessed.review_status='approved'))
                ORDER BY targets.distance,exercise.review_status='approved' DESC,exercise.title LIMIT ?""",
                (node_id,node_id,node_id,int(approved_only),int(approved_only),max(1,min(limit,10)))).fetchall()
        return [dict(row) for row in rows]

    def retest_items(self, node_id: str, limit: int = 3, *, approved_only: bool = False) -> list[dict]:
        """Backward-compatible name for graph-grounded similar/variant exercises."""
        return self.similar_exercises(node_id, limit, approved_only=approved_only)

    def record_evidence(self, *, child_id: str, node_id: str, correctness: float, independent: bool,
                        origin_type: str, idempotency_key: str, note: str = "", origin_id: str | None = None,
                        confidence: float = 1.0) -> dict:
        """Persist evidence and update target plus prerequisite weakness signals atomically."""
        self.ensure_ready(); now = utc_now(); correctness = max(0.0,min(1.0,float(correctness)))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_active_child(connection, child_id)
            host_id = self._host_attempt_id(idempotency_key)
            if host_id is not None:
                source = self.host_attempt_evidence(connection, host_id, child_id=child_id)
                command = dict(node_id=node_id, origin_type=origin_type, origin_id=origin_id,
                               correctness=correctness)
                if not source or not self._host_origin_matches(connection, command, source):
                    raise ValueError("host evidence is not currently qualified for KG mastery")
                if (int(independent) != int(source['independent']) or confidence != source['confidence']):
                    raise ValueError("host evidence conditions do not match the reliable projection")
                # Both historical keys identify one host observation, even when a mistake is attached later.
                prior_host = connection.execute("SELECT child_id,node_id FROM kg_mastery_evidence WHERE idempotency_key IN (?,?)",
                        (f"host-diagnostic:{host_id}", f"exercise:host-diagnostic:{host_id}")).fetchall()
                if any(r['child_id'] != child_id or r['node_id'] != node_id for r in prior_host):
                    raise ValueError('host action ownership or target conflicts with its original receipt')
                if prior_host:
                    return self._mastery_from_projection(connection, child_id, node_id)
            existing = connection.execute("SELECT child_id,node_id,origin_type,origin_id,correctness,independent,confidence,note FROM kg_mastery_evidence WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                replay = (existing['child_id']==child_id and existing['node_id']==node_id and existing['origin_type']==origin_type
                          and existing['origin_id']==origin_id and float(existing['correctness'])==correctness
                          and int(existing['independent'])==int(independent) and float(existing['confidence'])==float(confidence)
                          and existing['note']==note)
                if not replay:
                    raise ValueError('client action id was already used for a different mastery evidence command')
                return self._mastery_from_projection(connection, child_id, node_id)
            evidence_id = str(uuid4())
            connection.execute("INSERT INTO kg_mastery_evidence(evidence_id,child_id,node_id,origin_type,origin_id,correctness,independent,confidence,note,occurred_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (evidence_id,child_id,node_id,origin_type,origin_id,correctness,int(independent),confidence,note,now,idempotency_key))
            evidence_node = self._project_evidence_node(connection, evidence_id, child_id, node_id, origin_type, origin_id, idempotency_key, confidence, now)
            self._link_in_transaction(connection, evidence_node, node_id, "evidenced_by",
                                      rationale="本地学习证据指向该原子知识点", source_ref=f"local_evidence:{origin_type}",
                                      confidence=confidence, review_status="unreviewed", now=now)
            self._update_state(connection, child_id,node_id,correctness,independent,confidence,now)
            affected_nodes = [node_id]
            # An error is weak evidence about prerequisites, deliberately discounted.
            if correctness < .5:
                for prerequisite in self._prerequisite_ids(connection,node_id,2,approved_only=True):
                    self._update_state(connection,child_id,prerequisite,0.45,False,confidence*.35,now)
                    affected_nodes.append(prerequisite)
            for affected_node in affected_nodes:
                mastery_node = self._project_mastery_node(connection, child_id, affected_node, now)
                self._link_in_transaction(connection, mastery_node, evidence_node, "generated_from",
                                          rationale="掌握度由本地学习证据按 evidence_weighted_v1 更新", source_ref="local_mastery:evidence_weighted_v1",
                                          confidence=confidence, review_status="unreviewed", now=now)
            projection = self.host_mastery_projection(connection, child_id)
            for affected_node in affected_nodes:
                if affected_node in projection['host_affected']:
                    self._project_mastery_node(connection, child_id, affected_node, now,
                                              state=projection['states'][affected_node])
            connection.commit()
        return self.mastery(child_id,node_id)

    @staticmethod
    def _child_fingerprint(child_id: str) -> str:
        return sha256(child_id.encode("utf-8")).hexdigest()[:20]

    def _project_evidence_node(self, connection: sqlite3.Connection, evidence_id: str, child_id: str, node_id: str,
                               origin_type: str, origin_id: str | None, idempotency_key: str, confidence: float, now: str) -> str:
        evidence_type = "mistake" if origin_type == "mistake" else "diagnostic_evidence"
        fingerprint = self._child_fingerprint(child_id)
        canonical = f"evidence:{sha256(idempotency_key.encode('utf-8')).hexdigest()}"
        return self._upsert_node_in_transaction(
            connection, canonical_key=canonical, node_type=evidence_type,
            title="本地错题证据" if evidence_type == "mistake" else "本地诊断证据",
            description="保留在本机的学习证据；图节点仅存匿名学生指纹，不复制题目图片或媒体。",
            scope={"child_fingerprint":fingerprint,"origin_type":origin_type,"origin_id":origin_id or "","evidence_id":evidence_id,"knowledge_node_id":node_id},
            source_ref=f"local_evidence:{origin_type}", version_label="evidence_v1", license_boundary="child_local_private",
            confidence=confidence, review_status="unreviewed", source_time=now, now=now,
        )

    def _project_mastery_node(self, connection: sqlite3.Connection, child_id: str, knowledge_node_id: str, now: str, *, state=None) -> str:
        fingerprint = self._child_fingerprint(child_id)
        if state is None:
            state = connection.execute("SELECT mastery_probability,evidence_count,weakness_signal,algorithm_version FROM kg_mastery_state WHERE child_id=? AND node_id=?", (child_id,knowledge_node_id)).fetchone()
        return self._upsert_node_in_transaction(
            connection, canonical_key=f"mastery:{fingerprint}:{knowledge_node_id}", node_type="mastery_state",
            title="本地掌握度状态", description=json.dumps({"mastery_probability":state["mastery_probability"],"evidence_count":state["evidence_count"],"weakness_signal":state["weakness_signal"],"algorithm_version":state["algorithm_version"]}, ensure_ascii=False),
            scope={"child_fingerprint":fingerprint,"knowledge_node_id":knowledge_node_id}, source_ref="local_mastery:evidence_weighted_v1",
            version_label="evidence_weighted_v1", license_boundary="child_local_private", confidence=.8,
            review_status="unreviewed", source_time=now, now=now,
        )

    @staticmethod
    def _upsert_node_in_transaction(connection: sqlite3.Connection, *, canonical_key: str, node_type: str, title: str,
                                    description: str, scope: dict, source_ref: str, version_label: str,
                                    license_boundary: str, confidence: float, review_status: str,
                                    source_time: str, now: str) -> str:
        existing = connection.execute("SELECT node_id FROM kg_node WHERE canonical_key=?", (canonical_key,)).fetchone()
        node_id = existing["node_id"] if existing else str(uuid4())
        connection.execute(
            """INSERT INTO kg_node(node_id,node_type,canonical_key,title,description,scope_json,source_ref,source_time,version_label,license_boundary,confidence,review_status,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'active',?,?)
            ON CONFLICT(canonical_key) DO UPDATE SET title=excluded.title,description=excluded.description,scope_json=excluded.scope_json,source_ref=excluded.source_ref,source_time=excluded.source_time,version_label=excluded.version_label,license_boundary=excluded.license_boundary,confidence=excluded.confidence,review_status=excluded.review_status,updated_at=excluded.updated_at""",
            (node_id,node_type,canonical_key,title,description,json.dumps(scope,ensure_ascii=False),source_ref,source_time,version_label,license_boundary,confidence,review_status,now,now),
        )
        return node_id

    @staticmethod
    def _link_in_transaction(connection: sqlite3.Connection, source_node_id: str, target_node_id: str, relation_type: str,
                             *, rationale: str, source_ref: str, confidence: float, review_status: str, now: str) -> None:
        connection.execute(
            """INSERT INTO kg_edge(edge_id,source_node_id,target_node_id,relation_type,rationale,source_ref,version_label,confidence,review_status,created_at,updated_at)
            VALUES(?,?,?,?,?,?, 'evidence_v1',?,?,?,?) ON CONFLICT(source_node_id,target_node_id,relation_type,version_label)
            DO UPDATE SET rationale=excluded.rationale,source_ref=excluded.source_ref,confidence=excluded.confidence,review_status=excluded.review_status,updated_at=excluded.updated_at""",
            (str(uuid4()),source_node_id,target_node_id,relation_type,rationale,source_ref,confidence,review_status,now,now),
        )

    @staticmethod
    def _require_active_child(connection: sqlite3.Connection, child_id: str) -> None:
        row = connection.execute("SELECT status FROM child_profile WHERE child_id=?", (child_id,)).fetchone()
        if not row or row["status"] != "active":
            raise KnowledgeGraphChildNotFoundError("active child profile is required")

    @staticmethod
    def _prerequisite_ids(connection: sqlite3.Connection, node_id: str, max_depth: int, *, approved_only: bool = False) -> list[str]:
        found, queue = [], deque([(node_id,0)])
        seen = {node_id}
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth: continue
            filter_sql = " AND e.review_status='approved' AND source.review_status='approved' AND source.status='active'" if approved_only else ""
            for row in connection.execute(f"SELECT e.source_node_id FROM kg_edge e JOIN kg_node source ON source.node_id=e.source_node_id WHERE e.target_node_id=? AND e.relation_type='prerequisite_of'{filter_sql}", (current,)):
                candidate = row[0]
                if candidate not in seen:
                    seen.add(candidate); found.append(candidate); queue.append((candidate,depth+1))
        return found

    @staticmethod
    def _update_state(connection: sqlite3.Connection, child_id: str, node_id: str, correctness: float, independent: bool, confidence: float, now: str) -> None:
        row = connection.execute("SELECT mastery_probability,evidence_count,weakness_signal FROM kg_mastery_state WHERE child_id=? AND node_id=?", (child_id,node_id)).fetchone()
        prior = float(row["mastery_probability"]) if row else .5
        weight = (.22 if independent else .12) * confidence
        posterior = max(.02,min(.98, prior + weight * (correctness-prior)))
        weakness = max(0.,min(1., ((float(row["weakness_signal"]) if row else 0.)*.75) + (1-correctness)*confidence*.25))
        connection.execute("""INSERT INTO kg_mastery_state(child_id,node_id,mastery_probability,evidence_count,weakness_signal,algorithm_version,updated_at)
        VALUES(?,?,?,?,?,'evidence_weighted_v1',?) ON CONFLICT(child_id,node_id) DO UPDATE SET mastery_probability=excluded.mastery_probability,evidence_count=excluded.evidence_count,weakness_signal=excluded.weakness_signal,algorithm_version=excluded.algorithm_version,updated_at=excluded.updated_at""",
        (child_id,node_id,posterior,(int(row["evidence_count"]) if row else 0)+1,weakness,now))

    def mastery(self, child_id: str, node_id: str) -> dict:
        self.ensure_ready()
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            return self._mastery_from_projection(connection, child_id, node_id)

    @staticmethod
    def _host_attempt_id(key: str) -> str | None:
        for prefix in ('host-diagnostic:', 'exercise:host-diagnostic:'):
            if key.startswith(prefix):
                return key[len(prefix):]
        return None

    @staticmethod
    def host_attempt_evidence(connection, attempt_id: str, *, child_id=None, projections=None):
        from ..learning_obligations import project_mastery_evidence

        attempt = connection.execute("""SELECT da.*,di.knowledge_node_id,di.assessment_mode FROM diagnostic_attempt da
            JOIN diagnostic_item di USING(diagnostic_item_id) WHERE da.attempt_id=?""", (attempt_id,)).fetchone()
        if not attempt or attempt['assessment_mode'] != 'objective' or (child_id is not None and attempt['child_id'] != child_id):
            return None
        cache = projections if projections is not None else {}
        key = (attempt['child_id'], attempt['knowledge_node_id'])
        if key not in cache:
            cache[key] = project_mastery_evidence(connection, *key, as_of=utc_now())
        effective = next((r for r in cache[key]['effective_evidence'] if r['evidence_id'] == attempt['evidence_id']), None)
        if effective is None:
            return None
        targets = connection.execute("""SELECT n.node_id,n.scope_json,x.node_id AS exercise_id,cp.grade
            FROM kg_node x JOIN kg_edge e ON e.target_node_id=x.node_id
            JOIN kg_node n ON n.node_id=e.source_node_id
            JOIN child_profile cp ON cp.child_id=? AND cp.status='active'
            WHERE x.canonical_key=? AND x.node_type='exercise' AND x.status='active' AND x.review_status='approved'
              AND e.relation_type='assessed_by' AND e.review_status='approved'
              AND n.node_type='knowledge_point' AND n.status='active' AND n.review_status='approved'""",
            (attempt['child_id'], f"diagnostic:{attempt['diagnostic_item_id']}")).fetchall()
        if len(targets) != 1:
            return None
        target = targets[0]
        grade = str(json.loads(target['scope_json'] or '{}').get('grade') or '')
        if grade and grade != target['grade'] and not grade.startswith(f"{target['grade']}-"):
            return None
        approved = connection.execute("""SELECT 1 WHERE EXISTS(
            SELECT 1 FROM kg_resource_node WHERE node_id=? AND review_status='approved') OR EXISTS(
            SELECT 1 FROM kg_edge WHERE (source_node_id=? OR target_node_id=?) AND review_status='approved'
              AND relation_type='aligned_to' AND (source_ref LIKE 'curriculum_standard:%' OR source_ref LIKE 'product_evidence:%'))""",
            (target['node_id'], target['node_id'], target['node_id'])).fetchone()
        if not approved:
            return None
        return {**dict(attempt), **effective, 'node_id': target['node_id'], 'exercise_id': target['exercise_id']}

    @staticmethod
    def _host_origin_matches(connection, event, source):
        if event['node_id'] != source['node_id'] or event['correctness'] != source['correctness']:
            return False
        if event['origin_type'] == 'exercise':
            return event['origin_id'] == source['exercise_id']
        if event['origin_type'] != 'mistake':
            return False
        return bool(connection.execute("""SELECT 1 FROM mistake_case mc WHERE mc.mistake_id=?
            AND mc.child_id=? AND mc.knowledge_node_id=? AND mc.status!='excluded'
            AND (mc.source_evidence_id=? OR EXISTS(SELECT 1 FROM mistake_occurrence mo
                WHERE mo.mistake_id=mc.mistake_id AND mo.child_id=mc.child_id AND mo.evidence_id=?))""",
            (event['origin_id'], source['child_id'], source['knowledge_node_id'], source['evidence_id'], source['evidence_id'])).fetchone())

    def host_mastery_projection(self, connection, child_id: str) -> dict:
        """Replay host-affected states from immutable events and recorded propagation, never cached priors."""
        states = {r['node_id']: dict(r) for r in connection.execute('SELECT * FROM kg_mastery_state WHERE child_id=?', (child_id,))}
        events = [dict(r) for r in connection.execute('SELECT * FROM kg_mastery_evidence WHERE child_id=? ORDER BY rowid', (child_id,))]
        if not any(self._host_attempt_id(r['idempotency_key']) is not None for r in events):
            return dict(states=states, host_affected=set(), fingerprint=None, evidence_count=len(events))
        fingerprint = self._child_fingerprint(child_id)
        lineage = {}
        for row in connection.execute("""SELECT evidence.scope_json AS evidence_scope,mastery.scope_json AS mastery_scope
            FROM kg_edge e JOIN kg_node mastery ON mastery.node_id=e.source_node_id
            JOIN kg_node evidence ON evidence.node_id=e.target_node_id
            WHERE e.relation_type='generated_from' AND mastery.node_type='mastery_state'
              AND json_extract(mastery.scope_json,'$.child_fingerprint')=?
              AND json_extract(evidence.scope_json,'$.child_fingerprint')=?""", (fingerprint, fingerprint)):
            evidence = json.loads(row['evidence_scope'])
            node = json.loads(row['mastery_scope'])['knowledge_node_id']
            lineage.setdefault(evidence['evidence_id'], set()).add(node)
        affected, accepted, seen, cache, basis = set(), [], set(), {}, []
        for event in events:
            targets = {event['node_id']}
            if event['correctness'] < .5:
                targets.update(lineage.get(event['evidence_id'], ()))
            host_id = self._host_attempt_id(event['idempotency_key'])
            if host_id is not None:
                affected.update(targets)
                source = self.host_attempt_evidence(connection, host_id, child_id=child_id, projections=cache)
                valid = source is not None and self._host_origin_matches(connection, event, source)
                basis.append((event['evidence_id'], host_id, sorted(targets), valid))
                if not valid or host_id in seen:
                    continue
                seen.add(host_id)
                event = {**event, **{k: source[k] for k in ('correctness', 'independent', 'confidence')}}
            accepted.append((event, targets))
        replayed = {node: dict(child_id=child_id, node_id=node, mastery_probability=.5, evidence_count=0,
                              weakness_signal=0., algorithm_version='evidence_weighted_v1', updated_at=None) for node in affected}
        for event, targets in accepted:
            for node in targets & affected:
                score, independent, confidence = event['correctness'], event['independent'], event['confidence']
                if node != event['node_id']:
                    score, independent, confidence = .45, False, confidence * .35
                state = replayed[node]
                prior = state['mastery_probability']
                weight = (.22 if independent else .12) * confidence
                state.update(mastery_probability=max(.02, min(.98, prior + weight * (score - prior))),
                    weakness_signal=max(0., min(1., state['weakness_signal'] * .75 + (1 - score) * confidence * .25)),
                    evidence_count=state['evidence_count'] + 1, updated_at=event['occurred_at'])
        states.update(replayed)
        payload = dict(basis=basis, events=[(e, sorted(t)) for e, t in accepted], states=replayed)
        digest = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return dict(states=states, host_affected=affected, fingerprint=digest, evidence_count=len(accepted))

    def _mastery_from_projection(self, connection, child_id, node_id):
        return self.host_mastery_projection(connection, child_id)['states'].get(node_id,
            dict(child_id=child_id, node_id=node_id, mastery_probability=.5, evidence_count=0, weakness_signal=0.))

    def diagnose_mistake(self, child_id: str, node_id: str, *, approved_only: bool = False) -> dict:
        self.ensure_ready()
        with self.database.connect() as connection:
            status_filter = " AND e.review_status='approved' AND n.review_status='approved' AND n.status='active'" if approved_only else ""
            misconceptions=connection.execute(f"""SELECT n.node_id,n.title,e.confidence,e.rationale FROM kg_edge e JOIN kg_node n ON n.node_id=e.source_node_id WHERE e.target_node_id=? AND e.relation_type='misconception_of'{status_filter} ORDER BY e.confidence DESC""",(node_id,)).fetchall()
        prerequisites=self.prerequisites(node_id, approved_only=approved_only)
        weak=sorted(([self.mastery(child_id,node["node_id"]) for node in prerequisites]+[self.mastery(child_id,node_id)]),key=lambda item:(-item["weakness_signal"],item["mastery_probability"]))
        return {"node_id":node_id,"prerequisites":prerequisites,"misconception_candidates":[dict(row) for row in misconceptions],"weaknesses":weak,"explanation_materials":self.explanation_materials(node_id, limit=3, approved_only=approved_only),"retest_items":self.retest_items(node_id, limit=3, approved_only=approved_only)}

    def coverage(self, *, grade: str | None = None, subject: str | None = None) -> dict:
        self.ensure_ready(); clauses=[]; params=[]
        if grade: clauses.append("json_extract(n.scope_json,'$.grade')=?"); params.append(grade)
        if subject: clauses.append("json_extract(n.scope_json,'$.subject')=?"); params.append(subject)
        where=("WHERE "+" AND ".join(clauses)) if clauses else ""
        with self.database.connect() as connection:
            nodes=connection.execute(f"SELECT COUNT(*) FROM kg_node n {where} AND n.node_type='knowledge_point'" if where else "SELECT COUNT(*) FROM kg_node n WHERE n.node_type='knowledge_point'",params).fetchone()[0]
            candidate_covered=connection.execute(f"SELECT COUNT(DISTINCT rn.node_id) FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id {where}{' AND' if where else ' WHERE'} rn.review_status IN ('unreviewed','approved')",params).fetchone()[0]
            approved_covered=connection.execute(f"SELECT COUNT(DISTINCT rn.node_id) FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id {where}{' AND' if where else ' WHERE'} rn.review_status='approved'",params).fetchone()[0]
            resources=connection.execute("SELECT COUNT(*) FROM kg_resource").fetchone()[0]
            segments=connection.execute("SELECT COUNT(*) FROM kg_segment").fetchone()[0]
        candidate_ratio=round(candidate_covered/nodes,4) if nodes else 0.
        return {"knowledge_points":nodes,"candidate_covered_knowledge_points":candidate_covered,
                "approved_covered_knowledge_points":approved_covered,"candidate_coverage_ratio":candidate_ratio,
                "approved_coverage_ratio":round(approved_covered/nodes,4) if nodes else 0.,
                # Backwards-compatible alias; callers must label it as candidate coverage.
                "covered_knowledge_points":candidate_covered,"coverage_ratio":candidate_ratio,
                "resources":resources,"segments":segments}

    @staticmethod
    def _with_media_status(item: dict) -> dict:
        transcript_root, video_root = item.pop("_transcript_root", None), item.pop("_video_root", None)
        transcript_relative, video_relative = item.get("transcript_relative_path") or "", item.get("video_relative_path") or ""
        transcript_available = bool(transcript_relative and transcript_root and (Path(transcript_root) / transcript_relative).is_file())
        video_available = bool(video_relative and video_root and (Path(video_root) / video_relative).is_file())
        item["media_status"] = {"transcript_available":transcript_available,"video_available":video_available,
            "status":"available" if video_available or transcript_available else "index_only_media_unavailable"}
        return item
