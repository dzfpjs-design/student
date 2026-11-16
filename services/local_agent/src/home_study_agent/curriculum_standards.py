from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .database import Database
from .repository import utc_now


ALLOWED_FORMAT = "hsc_official_curriculum_sources_v1"
ALLOWED_DOMAINS = {"www.moe.gov.cn", "moe.gov.cn", "jyt.jiangsu.gov.cn", "edu.sh.gov.cn"}
ALLOWED_JURISDICTIONS = {"CN", "JS"}
ALLOWED_SOURCE_TYPES = {
    "national_curriculum_plan", "national_standard", "jiangsu_implementation",
    "jiangsu_textbook_catalog", "jiangsu_local_requirement",
}
TOPIC_ALIGNMENT_METHOD = "title_stage_heuristic_v1"


class InvalidCurriculumStandardSourceError(Exception):
    pass


class CurriculumStandardService:
    def __init__(self, database: Database):
        self.database = database

    @contextmanager
    def _import_transaction(self, source_key: str, generated_id: str):
        # Decide under the SQLite write lock: otherwise a source could acquire a
        # new requirement between a lock-free check and the overwrite. Release
        # SQLite before waiting for the external-effect guard (global lock order).
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT source_id FROM curriculum_standard_source WHERE source_key=?', (source_key,)).fetchone()
            source_id = row['source_id'] if row else generated_id
            standard_prefix = f'curriculum_standard:{source_id}:page:'
            graph_reference = bool(row and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_atomic_draft_standard_alignment'"
            ).fetchone() and connection.execute(
                'SELECT 1 FROM kg_atomic_draft_standard_alignment WHERE substr(source_ref,1,?)=? LIMIT 1',
                (len(standard_prefix), standard_prefix)).fetchone())
            referenced = bool(row and (
                connection.execute('SELECT 1 FROM curriculum_standard_requirement WHERE source_id=? LIMIT 1', (source_id,)).fetchone()
                or connection.execute('SELECT 1 FROM curriculum_topic_standard_candidate WHERE source_id=? LIMIT 1', (source_id,)).fetchone()
                or graph_reference))
            if not referenced:
                yield connection, source_id
                return
            connection.rollback()
        with self.database.external_effect_guard(timeout=2), self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT source_id FROM curriculum_standard_source WHERE source_key=?', (source_key,)).fetchone()
            yield connection, row['source_id'] if row else generated_id

    def import_root(self, root_value: str) -> dict:
        root = Path(root_value).expanduser().resolve()
        manifest_path = root / "manifest.json"
        if not root.is_dir() or not manifest_path.is_file():
            raise InvalidCurriculumStandardSourceError("请选择包含manifest.json的官方课标目录。")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InvalidCurriculumStandardSourceError("官方课标清单无法读取。") from error
        if manifest.get("format") != ALLOWED_FORMAT or not isinstance(manifest.get("sources"), list):
            raise InvalidCurriculumStandardSourceError("官方课标清单格式不受支持。")

        imported_sources = 0
        imported_pages = 0
        for source in manifest["sources"]:
            imported_pages += self._import_source(root, source)
            imported_sources += 1
        self.rebuild_topic_candidates()
        return {
            "status": "imported",
            "source_count": imported_sources,
            "page_count": imported_pages,
        }

    def rebuild_topic_candidates(self, corpus_id: str | None = None) -> dict:
        clauses = ["cc.status = 'ready'"]
        parameters: list[object] = []
        if corpus_id:
            clauses.append("ct.corpus_id = ?")
            parameters.append(corpus_id)
        with self.database.connect() as connection:
            topics = connection.execute(
                f"""
                SELECT ct.topic_id, ct.corpus_id, ct.grade_title, ct.subject_title,
                       ct.unit_title, ct.title, ct.learning_objective
                FROM curriculum_topic ct
                JOIN curriculum_corpus cc ON cc.corpus_id = ct.corpus_id
                WHERE {' AND '.join(clauses)}
                ORDER BY ct.topic_id
                """,
                parameters,
            ).fetchall()
            page_rows = connection.execute(
                """
                SELECT css.source_id, css.subject_title, csp.page_number, csp.content_text
                FROM curriculum_standard_page csp
                JOIN curriculum_standard_source css ON css.source_id = csp.source_id
                WHERE css.status IN ('ocr_ready', 'structured')
                  AND css.source_type = 'national_standard'
                ORDER BY css.subject_title, csp.page_number
                """
            ).fetchall()
            pages_by_subject: dict[str, list[dict]] = {}
            for row in page_rows:
                pages_by_subject.setdefault(row["subject_title"], []).append(dict(row))

            if corpus_id:
                connection.execute(
                    "DELETE FROM curriculum_topic_standard_candidate WHERE topic_id IN (SELECT topic_id FROM curriculum_topic WHERE corpus_id = ?)",
                    (corpus_id,),
                )
            else:
                connection.execute("DELETE FROM curriculum_topic_standard_candidate")
            now = utc_now()
            for topic in topics:
                subject = self._standard_subject(topic["subject_title"])
                subject_pages = pages_by_subject.get(subject, [])
                candidate = self._best_topic_page(dict(topic), subject_pages)
                if candidate:
                    values = (
                        topic["topic_id"], candidate["source_id"], candidate["page_number"],
                        candidate["matched_term"], "candidate", candidate["confidence"],
                        TOPIC_ALIGNMENT_METHOD, now,
                    )
                else:
                    values = (
                        topic["topic_id"], None, None, None,
                        "unresolved" if subject_pages else "no_subject_standard",
                        0.0, TOPIC_ALIGNMENT_METHOD, now,
                    )
                connection.execute(
                    """
                    INSERT INTO curriculum_topic_standard_candidate(
                      topic_id, source_id, page_number, matched_term, alignment_status,
                      confidence, method_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            connection.commit()
        return self.alignment_summary(corpus_id)

    def alignment_summary(self, corpus_id: str | None = None) -> dict:
        clause = ""
        parameters: list[object] = []
        if corpus_id:
            clause = " WHERE ct.corpus_id = ?"
            parameters.append(corpus_id)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total_topic_count,
                       SUM(CASE WHEN ctsc.alignment_status = 'candidate' THEN 1 ELSE 0 END) AS candidate_topic_count,
                       SUM(CASE WHEN ctsc.alignment_status = 'unresolved' THEN 1 ELSE 0 END) AS unresolved_topic_count,
                       SUM(CASE WHEN ctsc.alignment_status = 'no_subject_standard' THEN 1 ELSE 0 END) AS no_standard_topic_count
                FROM curriculum_topic ct
                LEFT JOIN curriculum_topic_standard_candidate ctsc ON ctsc.topic_id = ct.topic_id
                {clause}
                """,
                parameters,
            ).fetchone()
        return {
            "total_topic_count": int(row["total_topic_count"] or 0),
            "candidate_topic_count": int(row["candidate_topic_count"] or 0),
            "unresolved_topic_count": int(row["unresolved_topic_count"] or 0),
            "no_standard_topic_count": int(row["no_standard_topic_count"] or 0),
            "method_version": TOPIC_ALIGNMENT_METHOD,
        }

    def list_sources(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT source_id, source_key, jurisdiction, source_type, subject_title,
                       title, version_label, official_url, pdf_sha256, text_sha256,
                       page_count, extracted_page_count, status, imported_at, updated_at,
                       (SELECT COUNT(*) FROM curriculum_standard_requirement csr
                        WHERE csr.source_id = css.source_id AND csr.maturity = 'model_drafted') AS drafted_requirement_count,
                       (SELECT COUNT(*) FROM curriculum_standard_requirement csr
                        WHERE csr.source_id = css.source_id AND csr.maturity = 'verified') AS verified_requirement_count
                FROM curriculum_standard_source css
                ORDER BY jurisdiction, source_type, subject_title
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def search_pages(self, query: str, subject: str | None = None, limit: int = 20) -> list[dict]:
        terms = " ".join(part for part in query.strip().split() if len(part) >= 2)
        if not terms:
            return []
        parameters: list[object] = [terms]
        subject_clause = ""
        if subject:
            subject_clause = " AND css.subject_title = ?"
            parameters.append(subject)
        parameters.append(max(1, min(limit, 50)))
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT css.source_id, css.subject_title, css.title, css.version_label,
                       css.official_url, csp.page_number, csp.content_text,
                       bm25(curriculum_standard_page_fts) AS rank
                FROM curriculum_standard_page_fts
                JOIN curriculum_standard_page csp
                  ON csp.source_id = curriculum_standard_page_fts.source_id
                 AND csp.page_number = CAST(curriculum_standard_page_fts.page_number AS INTEGER)
                JOIN curriculum_standard_source css ON css.source_id = csp.source_id
                WHERE curriculum_standard_page_fts MATCH ?{subject_clause}
                ORDER BY rank LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [{
            "source_id": row["source_id"], "subject_title": row["subject_title"],
            "title": row["title"], "version_label": row["version_label"],
            "official_url": row["official_url"], "page_number": row["page_number"],
            "excerpt": self._excerpt(row["content_text"], query),
        } for row in rows]

    def _import_source(self, root: Path, source: dict) -> int:
        required = {
            "source_key", "jurisdiction", "source_type", "subject", "title", "version",
            "official_page_url", "page_count",
        }
        if not isinstance(source, dict) or not required.issubset(source):
            raise InvalidCurriculumStandardSourceError("课标来源字段不完整。")
        if source["jurisdiction"] not in ALLOWED_JURISDICTIONS or source["source_type"] not in ALLOWED_SOURCE_TYPES:
            raise InvalidCurriculumStandardSourceError("课标来源类型不受支持。")
        official_url = str(source["official_page_url"])
        if urlparse(official_url).hostname not in ALLOWED_DOMAINS:
            raise InvalidCurriculumStandardSourceError("课标来源不是允许的政府官网。")
        pdf_filename = source.get("pdf_filename")
        pdf_sha = source.get("pdf_sha256")
        if pdf_filename:
            pdf_path = (root / str(pdf_filename)).resolve()
            if pdf_path.parent != root or not pdf_path.is_file():
                raise InvalidCurriculumStandardSourceError("课标PDF不存在或路径越界。")
            if not pdf_sha or self._sha256_file(pdf_path) != pdf_sha:
                raise InvalidCurriculumStandardSourceError(f"{source['title']}的PDF哈希不一致。")
        content_filename = source.get("content_filename")
        if not pdf_filename and not content_filename:
            raise InvalidCurriculumStandardSourceError("课标来源缺少PDF或官方网页正文。")
        if content_filename:
            content_path = (root / str(content_filename)).resolve()
            if content_path.parent != root or not content_path.is_file():
                raise InvalidCurriculumStandardSourceError("官方网页正文不存在或路径越界。")
            if self._sha256_file(content_path) != source.get("content_sha256"):
                raise InvalidCurriculumStandardSourceError(f"{source['title']}的网页正文哈希不一致。")

        page_items: list[dict] = []
        pages_filename = source.get("ocr_pages_filename") or source.get("pages_filename")
        text_sha = None
        status = "downloaded"
        if source.get("ocr_status") == "completed" and pages_filename:
            pages_path = (root / str(pages_filename)).resolve()
            if pages_path.parent != root or not pages_path.is_file():
                raise InvalidCurriculumStandardSourceError("OCR逐页文件不存在或路径越界。")
            for line in pages_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as error:
                    raise InvalidCurriculumStandardSourceError("OCR逐页文件包含无效JSON。") from error
                if not isinstance(item.get("page"), int) or not isinstance(item.get("text"), str):
                    raise InvalidCurriculumStandardSourceError("OCR逐页记录字段无效。")
                page_items.append(item)
            if len(page_items) != int(source["page_count"]):
                raise InvalidCurriculumStandardSourceError("OCR页数与官方PDF不一致。")
            text_path = root / str(source.get("ocr_text_filename") or source.get("content_filename") or "")
            text_sha = self._sha256_file(text_path) if text_path.is_file() else None
            status = "ocr_ready"

        now = utc_now()
        generated_id = f"standard-{hashlib.sha256(str(source['source_key']).encode('utf-8')).hexdigest()[:24]}"
        with self._import_transaction(source['source_key'], generated_id) as (connection, source_id):
            connection.execute(
                """
                INSERT INTO curriculum_standard_source(
                  source_id, source_key, jurisdiction, source_type, subject_title, title,
                  version_label, official_url, local_root_path, pdf_filename, pages_filename,
                  pdf_sha256, text_sha256, page_count, extracted_page_count, status,
                  imported_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                  jurisdiction = excluded.jurisdiction, source_type = excluded.source_type,
                  subject_title = excluded.subject_title, title = excluded.title,
                  version_label = excluded.version_label, official_url = excluded.official_url,
                  local_root_path = excluded.local_root_path, pdf_filename = excluded.pdf_filename,
                  pages_filename = excluded.pages_filename, pdf_sha256 = excluded.pdf_sha256,
                  text_sha256 = excluded.text_sha256, page_count = excluded.page_count,
                  extracted_page_count = excluded.extracted_page_count, status = excluded.status,
                  updated_at = excluded.updated_at
                """,
                (
                    source_id, source["source_key"], source["jurisdiction"], source["source_type"],
                    source["subject"], source["title"], source["version"], official_url,
                    str(root), pdf_filename, pages_filename, pdf_sha,
                    text_sha, int(source["page_count"]), len(page_items), status, now, now,
                ),
            )
            connection.execute("DELETE FROM curriculum_standard_page WHERE source_id = ?", (source_id,))
            connection.execute("DELETE FROM curriculum_standard_page_fts WHERE source_id = ?", (source_id,))
            for item in page_items:
                text = item["text"].strip()
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT INTO curriculum_standard_page(source_id, page_number, content_text, content_hash, char_count) VALUES (?, ?, ?, ?, ?)",
                    (source_id, item["page"], text, digest, len(text)),
                )
                connection.execute(
                    "INSERT INTO curriculum_standard_page_fts(source_id, page_number, content_text) VALUES (?, ?, ?)",
                    (source_id, item["page"], text),
                )
            connection.execute(
                """
                INSERT INTO audit_event(audit_id, actor, action, child_id, summary_json, occurred_at)
                VALUES (?, 'system', 'official_curriculum_source_imported', NULL, ?, ?)
                """,
                (str(uuid4()), json.dumps({
                    "source_id": source_id, "jurisdiction": source["jurisdiction"],
                    "status": status, "page_count": len(page_items),
                }, ensure_ascii=False), now),
            )
            connection.commit()
        return len(page_items)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _standard_subject(value: str) -> str:
        subject = str(value or "").replace("小学", "").replace("初中", "").strip()
        aliases = {
            "生物": "生物学", "信息技术": "信息科技", "体育": "体育与健康",
            "音乐": "艺术", "美术": "艺术", "劳动技术": "劳动", "劳动科技": "劳动",
            "品德与社会": "道德与法治", "思想品德": "道德与法治",
        }
        return aliases.get(subject, subject)

    @staticmethod
    def _topic_terms(topic: dict) -> list[str]:
        title = str(topic.get("title") or "").strip()
        unit = str(topic.get("unit_title") or "").strip()
        objective = str(topic.get("learning_objective") or "").strip()
        terms = [title]
        replacements = {
            "加法": "加减", "减法": "加减", "乘法": "乘除", "除法": "乘除",
            "图像": "图象", "信息技术": "信息科技",
        }
        for old, new in replacements.items():
            if old in title:
                terms.append(title.replace(old, new))
        ignored = {"练习", "复习", "整理", "活动", "实验", "测试", "入门", "应用", "单元"}
        for source in (title, unit):
            compact = "".join(character for character in source if character.isalnum())
            for size in (5, 4, 3, 2):
                for index in range(max(0, len(compact) - size + 1)):
                    term = compact[index:index + size]
                    if term not in ignored:
                        terms.append(term)
        for phrase in objective.replace("，", "。").split("。"):
            value = phrase.strip()
            if 3 <= len(value) <= 18:
                terms.append(value)
        return list(dict.fromkeys(term for term in terms if len(term) >= 2))[:18]

    @classmethod
    def _best_topic_page(cls, topic: dict, pages: list[dict]) -> dict | None:
        if not pages:
            return None
        stage = {
            "四年级": "第二学段", "五年级": "第三学段", "六年级": "第三学段",
            "初一": "第四学段", "初二": "第四学段", "初三": "第四学段",
        }.get(str(topic.get("grade_title") or ""), "")
        best: dict | None = None
        for term_index, term in enumerate(cls._topic_terms(topic)):
            for page in pages:
                content = page["content_text"]
                if term not in content:
                    continue
                score = min(0.78, 0.42 + min(len(term), 8) * 0.045)
                if term_index == 0:
                    score += 0.06
                if stage and stage in content:
                    score += 0.08
                score = round(min(score, 0.88), 3)
                if best is None or score > best["confidence"]:
                    best = {
                        "source_id": page["source_id"], "page_number": page["page_number"],
                        "matched_term": term, "confidence": score,
                    }
        return best

    @staticmethod
    def _excerpt(text: str, query: str) -> str:
        index = text.find(query.strip())
        if index < 0:
            return text[:500]
        return text[max(0, index - 150):index + len(query.strip()) + 300]
