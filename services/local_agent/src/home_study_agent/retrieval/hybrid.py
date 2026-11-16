from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path


class HashingVectorizer:
    """Deterministic local character n-gram embedding; it sends no content off-device."""
    algorithm = "char_ngram_hash_v1"

    def __init__(self, dimensions: int = 128):
        self.dimensions = dimensions

    def encode(self, text: str) -> bytes:
        vector = [0.0] * self.dimensions
        normalized = re.sub(r"\s+", "", text.lower())
        grams = [normalized[i:i + size] for size in (2, 3) for i in range(max(0, len(normalized) - size + 1))]
        for gram in grams:
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self.dimensions
            vector[bucket] += 1.0 if digest[0] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return struct.pack(f"<{self.dimensions}f", *(value / norm for value in vector))

    def cosine(self, left: bytes, right: bytes) -> float:
        a, b = struct.unpack(f"<{self.dimensions}f", left), struct.unpack(f"<{self.dimensions}f", right)
        return sum(x * y for x, y in zip(a, b))


def index_segment(connection: sqlite3.Connection, segment_id: str, text: str, vectorizer: HashingVectorizer) -> None:
    connection.execute(
        """INSERT INTO kg_segment_embedding(segment_id, algorithm, dimensions, vector, updated_at)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT(segment_id) DO UPDATE SET vector=excluded.vector, updated_at=excluded.updated_at""",
        (segment_id, vectorizer.algorithm, vectorizer.dimensions, vectorizer.encode(text), datetime.now(timezone.utc).isoformat()),
    )


def hybrid_search(connection: sqlite3.Connection, query: str, *, subject: str | None = None,
                  grade: str | None = None, limit: int = 8, approved_only: bool = False,
                  vectorizer: HashingVectorizer | None = None) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    vectorizer = vectorizer or HashingVectorizer()
    # Quote user text as one FTS phrase so punctuation (for example `a-b`) cannot
    # become FTS syntax or a column reference.
    clauses, params = ["f.text MATCH ?"], [f'"{query.replace(chr(34), chr(34) * 2)}"']
    # FTS is de-duplicated by text, so its stored scope belongs only to the
    # first resource.  Visibility must be determined through a matching
    # occurrence resource; for student searches, approval must be attached to
    # that same resource occurrence.
    occurrence_clauses = ["o.segment_id=f.segment_id"]
    occurrence_params: list[object] = []
    if subject:
        occurrence_clauses.append("r_occ.subject_scope = ?")
        occurrence_params.append(subject)
    if grade:
        occurrence_clauses.append("r_occ.grade_scope = ?")
        occurrence_params.append(grade)
    if approved_only:
        occurrence_clauses.append("EXISTS(SELECT 1 FROM kg_resource_node rn_occ WHERE rn_occ.resource_id=o.resource_id AND rn_occ.segment_id=f.segment_id AND rn_occ.review_status='approved')")
    clauses.append("EXISTS(SELECT 1 FROM kg_segment_occurrence o JOIN kg_resource r_occ ON r_occ.resource_id=o.resource_id WHERE " + " AND ".join(occurrence_clauses) + ")")
    lexical = connection.execute(
        f"""SELECT f.segment_id, bm25(kg_segment_fts) AS lexical_rank
             FROM kg_segment_fts f WHERE {' AND '.join(clauses)} ORDER BY lexical_rank LIMIT ?""",
        (*params, *occurrence_params, max(limit * 8, 40)),
    ).fetchall()
    # A Unicode query may have no FTS term after tokenizer processing. Vector-only
    # fallback still offers local, deterministic similarity over a bounded candidate set.
    candidates = {row["segment_id"]: float(row["lexical_rank"]) for row in lexical}
    if not candidates:
        fallback_clauses, fallback_params = [], []
        if subject:
            fallback_clauses.append("r_occ.subject_scope = ?"); fallback_params.append(subject)
        if grade:
            fallback_clauses.append("r_occ.grade_scope = ?"); fallback_params.append(grade)
        if approved_only:
            fallback_clauses.append("EXISTS(SELECT 1 FROM kg_resource_node rn WHERE rn.resource_id=o.resource_id AND rn.segment_id=s.segment_id AND rn.review_status='approved')")
        fallback_where = f"WHERE {' AND '.join(fallback_clauses)}" if fallback_clauses else ""
        rows = connection.execute(
            f"SELECT DISTINCT s.segment_id FROM kg_segment s JOIN kg_segment_occurrence o ON o.segment_id=s.segment_id JOIN kg_resource r_occ ON r_occ.resource_id=o.resource_id {fallback_where} ORDER BY s.created_at DESC LIMIT 400",
            fallback_params,
        ).fetchall()
        candidates = {row["segment_id"]: 0.0 for row in rows}
    query_vector = vectorizer.encode(query)
    ids = tuple(candidates)
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""SELECT s.segment_id,s.text,s.start_char,s.end_char,r.resource_id,r.title,r.subject_scope,r.grade_scope,
                   r.transcript_relative_path,r.video_relative_path,r.source_ref,r.license_boundary,c.transcript_root AS _transcript_root,c.video_root AS _video_root,
                   (SELECT json_group_array(json_object('resource_id',r2.resource_id,'title',r2.title,'transcript_relative_path',r2.transcript_relative_path,'source_ref',r2.source_ref,'license_boundary',r2.license_boundary))
                    FROM kg_segment_occurrence o JOIN kg_resource r2 ON r2.resource_id=o.resource_id
                    WHERE o.segment_id=s.segment_id
                    AND r2.subject_scope IS r.subject_scope AND r2.grade_scope IS r.grade_scope
                    {"AND EXISTS(SELECT 1 FROM kg_resource_node visible_rn WHERE visible_rn.resource_id=o.resource_id AND visible_rn.segment_id=s.segment_id AND visible_rn.review_status='approved')" if approved_only else ""}) AS source_occurrences_json,
                   (SELECT json_group_array(json_object('knowledge_point',n.title,'mapping_source_ref',rn.source_ref,'mapping_review_status',rn.review_status,'license_boundary',r.license_boundary))
                    FROM kg_resource_node rn JOIN kg_node n ON n.node_id=rn.node_id
                    WHERE rn.resource_id=selected_occ.resource_id AND rn.segment_id=s.segment_id
                      AND rn.review_status='approved') AS approvals_json,
                   e.vector,e.dimensions
              FROM kg_segment s
              JOIN kg_segment_occurrence selected_occ ON selected_occ.segment_id=s.segment_id
              JOIN kg_resource r ON r.resource_id=selected_occ.resource_id
              JOIN kg_corpus c ON c.corpus_id=r.corpus_id
              LEFT JOIN kg_segment_embedding e ON e.segment_id=s.segment_id
             WHERE s.segment_id IN ({placeholders})
             {"AND EXISTS(SELECT 1 FROM kg_resource_node rn WHERE rn.resource_id=selected_occ.resource_id AND rn.segment_id=s.segment_id AND rn.review_status='approved')" if approved_only else ""}
             {"AND r.subject_scope=?" if subject else ""}
             {"AND r.grade_scope=?" if grade else ""}
             GROUP BY s.segment_id
             ORDER BY r.resource_id""", (*ids, *((subject,) if subject else ()), *((grade,) if grade else ()))).fetchall()
    worst = max((abs(v) for v in candidates.values()), default=1.0) or 1.0
    scored = []
    for row in rows:
        lexical_score = 1.0 - min(1.0, abs(candidates[row["segment_id"]]) / worst)
        semantic = vectorizer.cosine(query_vector, row["vector"]) if row["vector"] and row["dimensions"] == vectorizer.dimensions else 0.0
        result = dict(row)
        result.pop("vector", None); result.pop("dimensions", None)
        transcript_root, video_root = result.pop("_transcript_root", None), result.pop("_video_root", None)
        transcript, video = result.get("transcript_relative_path") or "", result.get("video_relative_path") or ""
        transcript_available = bool(transcript and transcript_root and (Path(transcript_root) / transcript).is_file())
        video_available = bool(video and video_root and (Path(video_root) / video).is_file())
        result["media_status"] = {"transcript_available": transcript_available, "video_available": video_available,
                                  "status": "available" if transcript_available or video_available else "index_only_media_unavailable"}
        # A deduplicated segment may originate in several videos.  Returning every
        # local provenance record keeps the search result auditable and lets the UI
        # choose an allowed source rather than silently treating one copy as canonical.
        result["source_occurrences"] = json.loads(result.pop("source_occurrences_json") or "[]")
        result["approvals"] = json.loads(result.pop("approvals_json") or "[]")
        result["score"] = round(0.55 * lexical_score + 0.45 * max(0.0, semantic), 4)
        scored.append(result)
    return sorted(scored, key=lambda item: (-item["score"], item["title"]))[:limit]
