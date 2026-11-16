"""Portable, text-only curriculum delivery. Never copies a family database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .schema import ensure_schema, sync_segment_occurrences


FORMAT = "hsc-curriculum-text-v1"
CORPORA = ("seyk-local-v1", "bili-local-v1", "nanjing-textbooks-v1")
FIELDS = {
    "kg_corpus": "corpus_id title source_kind transcript_root video_root metadata_db_path source_ref license_boundary review_status version_label created_at updated_at",
    "kg_resource": "resource_id corpus_id external_id title grade_scope subject_scope semester_scope transcript_relative_path video_relative_path transcript_sha256 video_sha256 video_fingerprint metadata_json source_ref license_boundary confidence review_status version_label ingested_at updated_at",
    "kg_document_page": "page_id resource_id document_kind page_number image_relative_path image_sha256 image_fingerprint ocr_engine ocr_confidence source_ref license_boundary version_label created_at updated_at",
    "kg_segment": "segment_id resource_id ordinal start_char end_char text text_sha256 source_ref confidence review_status created_at",
    "kg_segment_occurrence": "resource_id segment_id ordinal start_char end_char",
    "kg_node": "node_id node_type canonical_key title description scope_json source_ref source_time version_label license_boundary confidence review_status status created_at updated_at",
    "kg_edge": "edge_id source_node_id target_node_id relation_type rationale source_ref version_label confidence review_status created_at updated_at",
    "kg_resource_node": "resource_id segment_id node_id relation_type confidence review_status source_ref created_at",
}
KEYS = {table: fields.split()[0:1] for table, fields in FIELDS.items()}
KEYS["kg_segment_occurrence"] = ["resource_id", "segment_id"]
KEYS["kg_resource_node"] = ["resource_id", "segment_id", "node_id", "relation_type"]
SAFE_NODE_TYPES = ("standard_capability", "stage", "grade", "semester", "subject", "unit", "knowledge_point", "misconception", "explanation", "exercise")
META_FIELDS = {"page_id", "page_number", "ocr_confidence", "manual_review_required", "formula_review_required", "book_id", "book_title", "stage", "grade", "subject", "semester", "volume", "edition", "title", "unit_path", "source_ref", "bvid", "aid", "cid", "duration", "external_id"}


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _portable(row: dict, table: str) -> dict:
    row = dict(row)
    if table == "kg_corpus":
        row.update(transcript_root=f"bundle:{row['corpus_id']}", video_root=None, metadata_db_path=None)
    if table == "kg_resource":
        metadata = json.loads(row["metadata_json"])
        row["metadata_json"] = json.dumps({key: value for key, value in metadata.items() if key in META_FIELDS}, ensure_ascii=False)
        row.update(video_relative_path=None, video_fingerprint=None)
    if table == "kg_document_page":
        row.update(image_relative_path=None, image_fingerprint=None)
    return row


def export_content(source: Path, output: Path) -> dict:
    """Export only existing curriculum. No model calls, extraction or approval."""
    if output.exists():
        raise ValueError("content output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                "cloud_transmission_allowed": False, "media_included": False, "tables": {}}
    try:
        with closing(_ro(source)) as db, zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            corpora = {row[0] for row in db.execute("SELECT corpus_id FROM kg_corpus") if row[0] in CORPORA}
            resources = {row[0] for row in db.execute(
                "SELECT resource_id,corpus_id,grade_scope FROM kg_resource")
                if row[1] in corpora and "高中" not in (row[2] or "") and "高一" not in (row[2] or "")}
            segments = {row[0] for row in db.execute("SELECT segment_id,resource_id FROM kg_segment") if row[1] in resources}
            nodes = {row[0] for row in db.execute("SELECT node_id,node_type,scope_json FROM kg_node")
                     if row[1] in SAFE_NODE_TYPES and "高中" not in row[2]}
            def include(table, row):
                if table == "kg_corpus": return row["corpus_id"] in corpora
                if table == "kg_node": return row["node_id"] in nodes
                if table == "kg_edge": return row["source_node_id"] in nodes and row["target_node_id"] in nodes
                if row["resource_id"] not in resources: return False
                if table == "kg_resource_node":
                    return row["node_id"] in nodes and (row["segment_id"] is None or row["segment_id"] in segments)
                if table == "kg_segment_occurrence": return row["segment_id"] in segments
                return True
            for table, fields in FIELDS.items():
                count, digest = 0, hashlib.sha256()
                with archive.open(table + ".jsonl", "w") as stream:
                    for record in db.execute(f"SELECT {','.join(fields.split())} FROM {table}"):
                        if not include(table, record): continue
                        line = (json.dumps(_portable(dict(record), table), ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                        stream.write(line)
                        digest.update(line)
                        count += 1
                manifest["tables"][table] = {"rows": count, "sha256": digest.hexdigest()}
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        return manifest
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _validated_rows(archive, table, manifest):
    digest, count = hashlib.sha256(), 0
    with archive.open(table + ".jsonl") as stream:
        for line in stream:
            if len(line) > 4_000_000: raise ValueError("oversized content record")
            digest.update(line)
            row = json.loads(line)
            if set(row) != set(FIELDS[table].split()): raise ValueError("unexpected content fields")
            if table == "kg_corpus" and (row["corpus_id"] not in CORPORA or row["transcript_root"] != f"bundle:{row['corpus_id']}" or row["video_root"] or row["metadata_db_path"]):
                raise ValueError("nonportable corpus")
            if table == "kg_node" and row["node_type"] not in SAFE_NODE_TYPES: raise ValueError("non-curriculum node")
            if table == "kg_resource" and (row["video_relative_path"] or set(json.loads(row["metadata_json"])) - META_FIELDS):
                raise ValueError("media or private metadata in content")
            if table == "kg_document_page" and (row["image_relative_path"] or row["document_kind"] != "textbook"):
                raise ValueError("non-curriculum document")
            if table == "kg_segment" and hashlib.sha256(row["text"].encode()).hexdigest() != row["text_sha256"]:
                raise ValueError("segment evidence hash mismatch")
            count += 1
            yield row
    if count != manifest["tables"][table]["rows"] or digest.hexdigest() != manifest["tables"][table]["sha256"]:
        raise ValueError("content checksum mismatch")


def import_content(bundle: Path, target: Path, *, expected_sha256: str) -> dict:
    """Additive atomic import, preserving local decisions and existing family rows."""
    digest = hashlib.sha256()
    with bundle.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
    bundle_hash = digest.hexdigest()
    if bundle_hash != expected_sha256.lower(): raise ValueError("bundle checksum mismatch")
    if not target.is_file(): raise ValueError("target application database must already exist")
    with zipfile.ZipFile(bundle) as archive, closing(sqlite3.connect(target, timeout=60)) as db:
        db.row_factory = sqlite3.Row
        expected = {table + ".jsonl" for table in FIELDS} | {"manifest.json"}
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected): raise ValueError("unexpected archive members")
        if sum(info.file_size for info in archive.infolist()) > 2_000_000_000: raise ValueError("oversized content bundle")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != FORMAT or manifest.get("cloud_transmission_allowed") is not False or manifest.get("media_included") is not False or set(manifest.get("tables", {})) != set(FIELDS):
            raise ValueError("unsupported content manifest")
        db.execute("PRAGMA foreign_keys=ON")
        ensure_schema(db, datetime.now(timezone.utc).isoformat())
        db.execute("CREATE TABLE IF NOT EXISTS kg_content_delivery (bundle_sha256 TEXT PRIMARY KEY, imported_at TEXT NOT NULL, result_json TEXT NOT NULL)")
        db.commit()
        existing = db.execute("SELECT result_json FROM kg_content_delivery WHERE bundle_sha256=?", (bundle_hash,)).fetchone()
        if existing: return {**json.loads(existing[0]), "already_imported": True}
        counts, node_ids = {}, {}
        canonical_occurrences = []
        occurrence_fields = FIELDS["kg_segment_occurrence"].split()
        try:
            db.execute("BEGIN IMMEDIATE")
            for table, fields in FIELDS.items():
                inserted = preserved = 0
                columns = fields.split()
                for row in _validated_rows(archive, table, manifest):
                    if table == "kg_node":
                        canonical = db.execute("SELECT node_id FROM kg_node WHERE canonical_key=?", (row["canonical_key"],)).fetchone()
                        if canonical:
                            node_ids[row["node_id"]] = canonical[0]
                            preserved += 1
                            continue
                        node_ids[row["node_id"]] = row["node_id"]
                    elif table == "kg_edge":
                        endpoints = (node_ids[row["source_node_id"]], node_ids[row["target_node_id"]])
                        if endpoints != (row["source_node_id"], row["target_node_id"]):
                            row["edge_id"] = "delivered-" + hashlib.sha256((row["edge_id"] + repr(endpoints)).encode()).hexdigest()[:32]
                        row["source_node_id"], row["target_node_id"] = endpoints
                        old_relation = db.execute("SELECT 1 FROM kg_edge WHERE source_node_id=? AND target_node_id=? AND relation_type=? AND version_label=?", (*endpoints, row["relation_type"], row["version_label"])).fetchone()
                        if old_relation:
                            preserved += 1
                            continue
                    elif table == "kg_resource_node":
                        row["node_id"] = node_ids[row["node_id"]]
                    # An existing local approval/rejection is never overwritten.
                    keys = KEYS[table]
                    old = db.execute(f"SELECT * FROM {table} WHERE " + " AND ".join(f"{key} IS ?" for key in keys), [row[key] for key in keys]).fetchone()
                    if old:
                        if table == "kg_node" and old["canonical_key"] != row["canonical_key"]:
                            raise ValueError("conflicting existing node identity")
                        if table == "kg_segment" and (old["text_sha256"] != row["text_sha256"] or old["resource_id"] != row["resource_id"]):
                            raise ValueError("conflicting existing segment")
                        if table == "kg_segment":
                            canonical_occurrences.append(tuple(old[field] for field in occurrence_fields))
                        if table == "kg_resource" and old["transcript_sha256"] != row["transcript_sha256"]:
                            raise ValueError("conflicting existing resource")
                        preserved += 1
                        continue
                    db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [row[key] for key in columns])
                    if table == "kg_segment":
                        canonical_occurrences.append(tuple(row[field] for field in occurrence_fields))
                        db.execute("INSERT INTO kg_segment_fts(segment_id,text,title,subject_scope,grade_scope) SELECT ?,?,title,subject_scope,grade_scope FROM kg_resource WHERE resource_id=?", (row["segment_id"], row["text"], row["resource_id"]))
                    inserted += 1
                counts[table] = {"inserted": inserted, "preserved": preserved}
            # Legacy bundles can omit canonical occurrences. Complete only the
            # imported segments here, before the delivery receipt commits, and
            # never overwrite a producer's explicit offsets or deduplication.
            backfilled = db.executemany("""INSERT OR IGNORE INTO kg_segment_occurrence
                (resource_id,segment_id,ordinal,start_char,end_char) VALUES(?,?,?,?,?)""", canonical_occurrences).rowcount
            sync_segment_occurrences(db, datetime.now(timezone.utc).isoformat())
            for resource_id, segment_id, *_ in canonical_occurrences:
                if not db.execute("SELECT 1 FROM kg_segment_occurrence WHERE resource_id=? AND segment_id=?",
                                  (resource_id, segment_id)).fetchone():
                    raise ValueError("missing imported canonical occurrence")
            if db.execute("PRAGMA foreign_key_check").fetchone(): raise ValueError("content referential integrity failure")
            result = {"bundle_sha256": bundle_hash, "tables": counts, "already_imported": False,
                      "canonical_occurrences_backfilled": backfilled,
                      "cloud_transmission_allowed": False, "review_statuses_preserved": True}
            db.execute("INSERT INTO kg_content_delivery VALUES(?,?,?)", (bundle_hash, datetime.now(timezone.utc).isoformat(), json.dumps(result)))
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
