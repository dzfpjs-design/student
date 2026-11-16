"""Single-snapshot quality reporting for the local knowledge graph."""
from __future__ import annotations

from contextlib import closing
import json
import hashlib
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..database import Database
from .quality import GraphQualityService


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def quality_snapshot(app_db: Path) -> dict:
    """Read every displayed metric from one disposable SQLite online-backup snapshot."""
    if not app_db.is_file():
        raise FileNotFoundError(app_db)
    with tempfile.TemporaryDirectory(prefix="hsc-kg-quality-") as temporary:
        snapshot = Path(temporary).resolve(strict=True) / "quality.db"
        with closing(sqlite3.connect(app_db)) as source:
            # Pin one source view while backup incorporates committed WAL pages.
            source.execute("BEGIN")
            try:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
            finally:
                source.rollback()
        from ..runtime_data_scope import read_scope
        with closing(sqlite3.connect(snapshot)) as scope_connection:
            scope = read_scope(scope_connection)
        database = Database(snapshot, runtime_mode=scope)
        with database.connect() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"quality snapshot integrity check failed: {integrity}")
            quality = GraphQualityService(database).report()
            counts = dict(connection.execute("""SELECT
                (SELECT COUNT(*) FROM kg_resource) resources,
                (SELECT COUNT(*) FROM kg_resource WHERE transcript_relative_path!='') transcript_resources,
                (SELECT COUNT(*) FROM kg_segment) segments,
                (SELECT COUNT(*) FROM kg_segment_embedding) embeddings,
                (SELECT COUNT(*) FROM kg_node) nodes,
                (SELECT COUNT(*) FROM kg_edge) edges,
                (SELECT COUNT(*) FROM kg_ingest_failure WHERE status='open') open_failures""").fetchone())
            corpora = [dict(row) for row in connection.execute("""SELECT c.corpus_id,c.version_label,c.source_kind,COUNT(*) resources
                FROM kg_resource r JOIN kg_corpus c ON c.corpus_id=r.corpus_id
                GROUP BY c.corpus_id,c.version_label,c.source_kind ORDER BY c.corpus_id""")]
            node_types = [dict(row) for row in connection.execute("SELECT node_type,COUNT(*) count FROM kg_node GROUP BY node_type ORDER BY node_type")]
            schema_version = connection.execute("SELECT MAX(version) FROM kg_schema_migration").fetchone()[0]
            snapshot_meta = {
                "database_sha256": _sha256_file(snapshot),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "consistency": "sqlite_backup_read_transaction",
                "integrity_check": integrity,
                "schema_version": schema_version,
                "table_counts": counts,
                "corpora": corpora,
            }
    return {"snapshot":snapshot_meta,"integrity":integrity,"counts":counts,"corpora":corpora,"node_types":node_types,"quality":quality}


def render_quality_markdown(snapshot: dict) -> str:
    counts, mapping, tracks = snapshot["counts"], snapshot["quality"]["mapping"], snapshot["quality"]["target_coverage"]
    mapping_sources = snapshot["quality"]["mapping_source_breakdown"]
    corpus_text = "；".join(f"{item['source_kind']} {item['resources']}（{item['version_label']}）" for item in snapshot["corpora"])
    lines = [
        "# 知识图谱质量报告（自动生成）",
        "",
        "本文件由 `scripts/generate_knowledge_graph_quality_report.py` 从**同一个 SQLite 在线备份快照**生成。它只描述传入的数据库（隔离验证库或生产库）；不要手工修改计数；`--check` 会拒绝漂移。",
        f"快照 SHA-256：`{snapshot['snapshot']['database_sha256']}`；schema：`{snapshot['snapshot']['schema_version']}`；一致性：`{snapshot['snapshot']['consistency']}`。",
        "",
        "## 资源与结构",
        "",
        "| 项目 | 快照实测 |",
        "|---|---:|",
        f"| 资源 | {counts['resources']}（{corpus_text}） |",
        f"| 有转录资源 | {counts['transcript_resources']} |",
        f"| 去重转录片段 / 向量 | {counts['segments']} / {counts['embeddings']} |",
        f"| 图节点 / 图边 | {counts['nodes']} / {counts['edges']} |",
        f"| 导入失败队列 | {counts['open_failures']} |",
        f"| SQLite 完整性 | `{snapshot['integrity']}` |",
        "",
        "## 映射质量与覆盖",
        "",
        "| 指标 | 快照实测 |",
        "|---|---:|",
        f"| 片段—知识点映射行 | {mapping['rows']} |",
        f"| 候选或批准覆盖片段 | {mapping['candidate_or_approved_mapped_segments']} / {mapping['total_segments']} |",
        f"| 已批准覆盖片段 | {mapping['approved_mapped_segments']} |",
        f"| 未映射片段率 | {mapping['unmapped_segment_rate']:.2%} |",
        f"| 未审核 / 已批准 / 已拒绝映射行 | {mapping['unreviewed_rows']} / {mapping['approved_rows']} / {mapping['rejected_rows']} |",
        f"| 潜在冲突片段 | {mapping['potential_conflict_segments']} |",
        f"| 人工审核决策 | {mapping['human_reviewed_rows']} |",
        "| 人工映射准确率 | `unknown`（除非另有独立标注真值集） |",
        "",
        "## 目标轨道覆盖",
        "",
        "| 年级范围 | 轨道 | 原子目录 | 被候选/批准覆盖原子点 | 覆盖率 | 候选/批准片段 | 已批准片段 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in tracks:
        lines.append(f"| {item['grade_scope'] or '未标注'} | {item['track'] or '未标注'} | {item['nodes']} | {item['candidate_or_approved_covered_nodes']} | {item['candidate_atom_coverage_ratio']:.2%} | {item['candidate_or_approved_mapped_segments']} | {item['approved_mapped_segments']} |")
    lines.extend([
        "",
        "## 候选来源审计入口",
        "",
        "| 映射依据 | 审核状态 | 映射行 |",
        "|---|---|---:|",
    ])
    for item in mapping_sources:
        lines.append(f"| {item['source_ref']} | {item['review_status']} | {item['count']} |")
    lines.extend([
        "",
        "## 发布解释",
        "",
        "自动证据映射（课标锚点、资源元数据、标题/转录精确命中）只产生待审核候选；学生端只消费家长或产品审核为 `approved` 的映射。`unreviewed`、`rejected` 和 `restricted` 映射均不参与召回。候选数量不是教学准确率，也不是产品价值证明。",
        "",
    ])
    return "\n".join(lines)


def write_or_check_report(*, app_db: Path, markdown_output: Path, json_output: Path | None = None, check: bool = False) -> bool:
    snapshot = quality_snapshot(app_db)
    markdown = render_quality_markdown(snapshot)
    if check:
        if not markdown_output.is_file() or markdown_output.read_text(encoding="utf-8") != markdown:
            return False
        if not json_output:
            return True
        if not json_output.is_file():
            return False
        saved = json.loads(json_output.read_text(encoding="utf-8"))
        # Capture time is intentionally observational rather than a freshness
        # key. The immutable backup hash and all displayed data remain gated.
        for payload in (saved, snapshot):
            payload.get("snapshot", {}).pop("generated_at", None)
        return saved == snapshot
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.write_text(markdown, encoding="utf-8")
    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True
