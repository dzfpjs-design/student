"""Persistent human calibration packs that never change mapping approval."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from ..database import Database
from ..repository import utc_now
from .schema import ensure_schema


class MappingAuditPackService:
    criteria_version = "strict-lexical-proxy-v2"

    def __init__(self, database: Database):
        self.database = database

    def create(self, *, per_stratum: int = 5) -> dict:
        now = utc_now()
        sample_size = max(1, min(int(per_stratum), 50))
        with self.database.connect() as connection:
            ensure_schema(connection, now)
            rows = connection.execute(
                """WITH ranked AS (
                       SELECT rn.resource_id, rn.segment_id, rn.node_id,
                              rn.relation_type, rn.source_ref, rn.confidence,
                              n.title, s.text,
                              r.subject_scope AS resource_subject,
                              r.grade_scope AS resource_grade,
                              json_extract(n.scope_json, '$.grade') AS grade,
                              json_extract(n.scope_json, '$.subject') AS subject,
                              json_extract(n.scope_json, '$.track') AS track,
                              ROW_NUMBER() OVER (
                                  PARTITION BY json_extract(n.scope_json, '$.grade'),
                                               json_extract(n.scope_json, '$.subject'),
                                               json_extract(n.scope_json, '$.track')
                                  ORDER BY n.title, rn.confidence DESC, s.segment_id
                              ) AS position
                       FROM kg_resource_node rn
                       JOIN kg_node n ON n.node_id = rn.node_id
                       JOIN kg_segment s ON s.segment_id = rn.segment_id
                       JOIN kg_resource r ON r.resource_id = rn.resource_id
                       WHERE rn.review_status = 'unreviewed'
                   )
                   SELECT * FROM ranked
                   WHERE position <= ?
                   ORDER BY grade, subject, track, position""",
                (sample_size,),
            ).fetchall()
            fingerprint = hashlib.sha256(
                json.dumps([tuple(row) for row in rows], ensure_ascii=False).encode()
            ).hexdigest()
            existing = connection.execute(
                """SELECT audit_pack_id FROM kg_mapping_audit_pack
                   WHERE snapshot_sha256=? AND criteria_version=?
                   ORDER BY created_at DESC LIMIT 1""",
                (fingerprint, self.criteria_version),
            ).fetchone()
            if existing:
                return self.summary(existing["audit_pack_id"])

            pack_id = str(uuid4())
            connection.execute(
                """INSERT INTO kg_mapping_audit_pack(
                       audit_pack_id,snapshot_sha256,criteria_version,status,
                       item_count,created_at,updated_at
                   ) VALUES(?,?,?,'open',?,?,?)""",
                (pack_id, fingerprint, self.criteria_version, len(rows), now, now),
            )
            for row in rows:
                scope_match = (
                    "exact_or_missing_metadata"
                    if (
                        (not row["resource_subject"] or row["resource_subject"] == row["subject"])
                        and (not row["resource_grade"] or row["resource_grade"] == row["grade"])
                    )
                    else "metadata_conflict"
                )
                connection.execute(
                    """INSERT INTO kg_mapping_audit_item(
                           audit_item_id,audit_pack_id,resource_id,segment_id,node_id,
                           relation_type,grade_scope,subject_scope,track,lexical_evidence,
                           scope_match,mapping_source_ref,precision_proxy,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()),
                        pack_id,
                        row["resource_id"],
                        row["segment_id"],
                        row["node_id"],
                        row["relation_type"],
                        row["grade"],
                        row["subject"],
                        row["track"],
                        row["title"],
                        scope_match,
                        row["source_ref"],
                        float(row["confidence"]),
                        now,
                    ),
                )
            connection.commit()
        return self.summary(pack_id)

    def record_review(
        self,
        audit_item_id: str,
        *,
        verdict: str,
        reviewer: str,
        error_category: str | None = None,
        note: str | None = None,
    ) -> dict:
        if verdict not in {"accept", "reject", "needs_split"}:
            raise ValueError("unsupported audit verdict")
        if not reviewer.strip():
            raise ValueError("reviewer is required")
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT audit_pack_id FROM kg_mapping_audit_item WHERE audit_item_id=?",
                (audit_item_id,),
            ).fetchone()
            if not row:
                raise KeyError("audit item not found")
            pack_id = row["audit_pack_id"]
            connection.execute(
                """UPDATE kg_mapping_audit_item
                   SET human_verdict=?,error_category=?,reviewer=?,note=?,reviewed_at=?
                   WHERE audit_item_id=?""",
                (verdict, error_category, reviewer.strip(), note, now, audit_item_id),
            )
            remaining = connection.execute(
                """SELECT COUNT(*) FROM kg_mapping_audit_item
                   WHERE audit_pack_id=? AND human_verdict IS NULL""",
                (pack_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE kg_mapping_audit_pack SET status=?,updated_at=? WHERE audit_pack_id=?",
                ("completed" if remaining == 0 else "open", now, pack_id),
            )
            connection.commit()
        return self.summary(pack_id)

    def items(self, pack_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT audit_item_id,resource_id,segment_id,node_id,relation_type,
                          grade_scope,subject_scope,track,lexical_evidence,scope_match,
                          mapping_source_ref,precision_proxy AS candidate_confidence,
                          human_verdict,error_category,reviewer,note,reviewed_at,created_at
                   FROM kg_mapping_audit_item
                   WHERE audit_pack_id=?
                   ORDER BY grade_scope,subject_scope,track,created_at,audit_item_id""",
                (pack_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self, pack_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM kg_mapping_audit_pack WHERE audit_pack_id=?", (pack_id,)
            ).fetchone()
            if not row:
                raise KeyError("audit pack not found")
            pack = dict(row)
            breakdown = [
                dict(item)
                for item in connection.execute(
                    """SELECT grade_scope,subject_scope,track,human_verdict,error_category,
                              COUNT(*) AS count
                       FROM kg_mapping_audit_item
                       WHERE audit_pack_id=?
                       GROUP BY grade_scope,subject_scope,track,human_verdict,error_category""",
                    (pack_id,),
                )
            ]
            aggregate = connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(human_verdict IS NOT NULL) AS reviewed,
                          SUM(human_verdict='accept') AS accepted,
                          SUM(human_verdict='reject') AS rejected,
                          SUM(human_verdict='needs_split') AS needs_split,
                          AVG(precision_proxy) AS mean_candidate_confidence
                   FROM kg_mapping_audit_item WHERE audit_pack_id=?""",
                (pack_id,),
            ).fetchone()
        accepted = int(aggregate["accepted"] or 0)
        rejected = int(aggregate["rejected"] or 0)
        denominator = accepted + rejected
        metrics = {
            "total": int(aggregate["total"] or 0),
            "reviewed": int(aggregate["reviewed"] or 0),
            "accepted": accepted,
            "rejected": rejected,
            "needs_split": int(aggregate["needs_split"] or 0),
            "review_coverage": (
                float(aggregate["reviewed"] or 0) / float(aggregate["total"])
                if aggregate["total"]
                else 0.0
            ),
            "human_precision": accepted / denominator if denominator else None,
            "mean_candidate_confidence": aggregate["mean_candidate_confidence"],
        }
        return {
            "pack": pack,
            "breakdown": breakdown,
            "metrics": metrics,
            "metric_boundary": (
                "human_precision is available only after accept/reject review; "
                "candidate confidence is not precision"
            ),
        }
