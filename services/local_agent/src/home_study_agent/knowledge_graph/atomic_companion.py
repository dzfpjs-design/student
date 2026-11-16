"""Read-through access from the companion agent to the reviewed atomic build.

The build database is always opened read-only.  The application database only
stores a privacy-minimised audit trail: neither the child id nor the raw query
is persisted.  Unreviewed graph content is explicitly constrained to question
generation and weakness hypotheses; it cannot become mastery evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
from contextlib import closing
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4


def ensure_usage_schema(connection):
    """Create the local audit store without committing the caller's transaction."""
    connection.execute('''CREATE TABLE IF NOT EXISTS kg_companion_graph_meta(
        key TEXT PRIMARY KEY,value TEXT NOT NULL,created_at TEXT NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS kg_companion_graph_context_event(
        event_id TEXT PRIMARY KEY,child_fingerprint TEXT NOT NULL,query_fingerprint TEXT NOT NULL,
        grade_scope_json TEXT NOT NULL,subject_scope TEXT,result_count INTEGER NOT NULL,
        approved_count INTEGER NOT NULL,hypothesis_count INTEGER NOT NULL,
        selected_keys_json TEXT NOT NULL,build_label TEXT NOT NULL,created_at TEXT NOT NULL)''')
    connection.execute('''CREATE INDEX IF NOT EXISTS idx_kg_companion_context_created
        ON kg_companion_graph_context_event(created_at DESC)''')
    connection.execute('''INSERT OR IGNORE INTO kg_companion_graph_meta(key,value,created_at)
        VALUES('privacy_salt',?,?)''', (secrets.token_hex(32), datetime.now(timezone.utc).isoformat()))


BUILD_DB_CANDIDATES = (
    (Path.home() / '.home-study-companion/knowledge-build/atomic-full-v6.db'),
    (Path.home() / '.home-study-companion/knowledge-build/atomic-full-v5.db'),
    (Path.home() / '.home-study-companion/knowledge-build/atomic-full-v4.db'),
)


class AtomicGraphUnavailableError(RuntimeError):
    """The local atomic build is absent or cannot be queried."""


class AtomicGraphChildNotFoundError(LookupError):
    """The requested active child profile does not exist."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_value(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _effective_status(action: str | None, fallback: str) -> str:
    return {
        "merge_suggestion": "needs_merge",
        "relation_correction": "needs_correction",
        "revert": fallback,
    }.get(action or "", action or fallback)


def compatible_grade_bands(grade: str) -> tuple[str, ...]:
    normalized = re.sub(r"\s+", "", grade or "")
    if any(token in normalized for token in ("初一", "初二", "初三", "七年级", "八年级", "九年级", "初中一年级", "初中二年级", "初中三年级")):
        return ("初中7-9",)
    if any(token in normalized for token in ("一年级", "二年级", "小学一", "小学二")):
        return ("小学1-2", "小学1-6")
    if any(token in normalized for token in ("三年级", "四年级", "小学三", "小学四")):
        return ("小学3-4", "小学3-6", "小学1-6")
    if any(token in normalized for token in ("五年级", "六年级", "小学五", "小学六")):
        return ("小学5-6", "小学3-6", "小学1-6")
    if any(token in normalized for token in ("高一", "高二", "高三", "十年级", "十一年级", "十二年级")):
        return ("高中10-12",)
    return ()


class AtomicGraphCompanionService:
    """Bounded graph retrieval for companion questioning and diagnosis support."""

    _generic_phrases = (
        "孩子", "学生", "最近", "不会", "不理解", "不明白", "怎么计算", "如何计算",
        "怎么算", "如何算", "怎么求", "如何求", "怎样求", "是什么意思", "是什么", "有什么", "是多少", "可以吗", "什么", "怎么做", "怎么", "如何",
        "为什么", "知识点", "题目", "做题", "需要", "请帮忙", "请", "帮忙", "辅导",
    )
    _generic_ngrams = {"说明", "运用", "认识", "掌握", "理解", "关系", "进行", "能够", "学生"}
    _generic_anchors = {"意义", "关系", "计算", "方法", "概念", "题目", "知识"}

    def __init__(self, application_db: Path | str, build_db: Path | str | None = None, *, audit_usage: bool = True):
        self.application_db = Path(application_db).expanduser().resolve()
        self.build_db = self._resolve_build_db(build_db)
        self._audit_usage = audit_usage
        if audit_usage:
            self.application_db.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_usage_store()

    @staticmethod
    def _resolve_build_db(build_db: Path | str | None) -> Path | None:
        if build_db is not None:
            candidate = Path(build_db).expanduser().resolve()
            return candidate if candidate.is_file() else None
        return next((path.resolve() for path in BUILD_DB_CANDIDATES if path.is_file()), None)

    @property
    def available(self) -> bool:
        return bool(self.build_db and self.build_db.is_file())

    def _app_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.application_db, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def packaged_identity(self) -> tuple[str, str] | None:
        """Identify a frozen shipped snapshot, keeping editable builds in normal WAL mode."""
        build = self.build_db
        if build is None or build.name != 'atomic-knowledge.db':
            return None
        try:
            manifest_path = build.with_name('atomic-knowledge-manifest.json')
            if manifest_path.stat().st_size > 16_384:
                return None
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            wal = build.with_name(build.name + '-wal')
            if (wal.exists() and wal.stat().st_size > 0) or not isinstance(manifest, dict):
                return None
            source = manifest.get('source_label')
            checksum = manifest.get('sha256')
            if (manifest.get('schema') == 'hsc.atomic-knowledge.package.v1'
                    and manifest.get('packaged_name') == build.name
                    and manifest.get('read_only_runtime') is True
                    and manifest.get('bytes') == build.stat().st_size
                    and isinstance(source, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,48}', source)
                    and isinstance(checksum, str) and re.fullmatch(r'[a-fA-F0-9]{64}', checksum)):
                return source, checksum.lower()
        except (OSError, ValueError):
            return None
        return None

    def _build_connection(self) -> sqlite3.Connection:
        if not self.available or self.build_db is None:
            raise AtomicGraphUnavailableError("本机尚未找到可用的原子知识图谱构建库。")
        # The shipped v6 file has a WAL-mode header but was checkpointed before
        # packaging. Plain mode=ro still needs writable sidecars on some hosts.
        immutable = '&immutable=1' if self.packaged_identity() else ''
        connection = sqlite3.connect(f"file:{quote(str(self.build_db))}?mode=ro{immutable}", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _ensure_usage_store(self) -> None:
        with self._app_connection() as connection:
            ensure_usage_schema(connection)

    def _child_grade(self, child_id: str) -> str:
        with closing(self._app_connection()) as connection:
            try:
                row = connection.execute(
                    "SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)
                ).fetchone()
            except sqlite3.OperationalError as error:
                raise AtomicGraphChildNotFoundError("应用知识库中没有孩子档案表。") from error
        if not row:
            raise AtomicGraphChildNotFoundError("找不到可用的孩子档案。")
        return str(row["grade"])

    def _privacy_fingerprint(self, value: str) -> str:
        with self._app_connection() as connection:
            salt = connection.execute(
                "SELECT value FROM kg_companion_graph_meta WHERE key='privacy_salt'"
            ).fetchone()[0]
        return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()

    @classmethod
    def _query_fragments(cls, query: str) -> list[str]:
        lowered = query.lower()
        reduced = re.sub(
            r"\s+",
            lambda match: ' ' if (match.start() > 0 and match.end() < len(lowered)
                                  and re.fullmatch(r'[a-z0-9]', lowered[match.start() - 1])
                                  and re.fullmatch(r'[a-z0-9]', lowered[match.end()])) else '',
            lowered,
        )
        for phrase in cls._generic_phrases:
            reduced = reduced.replace(phrase, "")
        chunks = re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,}", reduced)
        fragments: list[str] = []
        for chunk in chunks:
            if re.fullmatch(r"[a-z0-9]+", chunk):
                fragments.append(chunk)
                continue
            if len(chunk) <= 4:
                fragments.append(chunk)
            else:
                fragments.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
                fragments.extend(chunk[index:index + 3] for index in range(0, len(chunk) - 2, 2))
        unique = []
        for fragment in fragments:
            if fragment not in cls._generic_ngrams and fragment not in unique:
                unique.append(fragment)
        return unique[:18]

    @classmethod
    def _query_anchors(cls, query: str, fallback: list[str]) -> list[str]:
        reduced = re.sub(r"\s+", "", query.lower())
        for phrase in cls._generic_phrases:
            reduced = reduced.replace(phrase, "")
        anchors = [
            cls._normalized_text(item)
            for item in re.split(r"[的和与及、，,。；;：:]+", reduced)
            if len(item) >= 2 and item not in cls._generic_anchors
        ]
        # Natural child speech often wraps the real concept in a longer phrase,
        # for example "数学里的小数为什么这样表示". Keep its meaningful
        # two/three-character fragments as alternative anchors instead of
        # requiring the whole spoken clause to appear in a concise atom title.
        for fragment in fallback:
            if fragment not in anchors and fragment not in cls._generic_ngrams:
                anchors.append(fragment)
        return anchors[:18]

    @staticmethod
    def _normalized_text(value: str) -> str:
        return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.lower())

    @staticmethod
    def _sign_conditions(value: str) -> frozenset[str]:
        """Read explicit sign conditions; do not infer a sign from a number's spelling alone."""
        text = unicodedata.normalize('NFKC', value).replace(' ', '')
        signs = set()
        if re.search(r'(?<!非)(?<!不)(?<!不是)正数|(?<!不)大于(?:零|0)(?![\d.]|除外|以外)', text):
            signs.add('positive')
        if re.search(r'(?<!非)(?<!不)(?<!不是)负数|(?<!不)小于(?:零|0)(?![\d.]|除外|以外)', text):
            signs.add('negative')
        if re.search(r'(?<!不)(?<![!≠])(?:乘以|除以|乘|除|等于|为|是|=)(?:零|0)(?![\d.]|除外|以外)', text):
            signs.add('zero')
        return frozenset(signs)

    @staticmethod
    def _unknown_sign(value: str) -> bool:
        return bool(re.search(r'(?:符号|正负)(?:不确定|未知|不明|不清楚|无法确定)|'
                              r'(?:不确定|未知|不明)(?:其|数的|的)?(?:符号|正负)', value))

    @classmethod
    def _title_core(cls, value: str) -> str:
        return re.sub(r"^(说明|运用|识别|解释|认识|掌握|理解|说出)", "", cls._normalized_text(value))

    @classmethod
    def _is_near_duplicate(cls, title: str, subject: str,
                           seen: list[tuple[str, str]]) -> bool:
        core = cls._title_core(title)
        if not core or (len(core) <= 4 and core.endswith(("的", "与", "和", "为", "是"))):
            return True
        for seen_core, seen_subject in seen:
            if subject != seen_subject:
                continue
            if core == seen_core:
                return True
            short, long = sorted((core, seen_core), key=len)
            if len(long) - len(short) <= 2 and long.startswith(short):
                return True
            if SequenceMatcher(a=core, b=seen_core, autojunk=False).ratio() >= 0.9:
                return True
        return False

    @classmethod
    def _relevance(cls, query: str, title: str, fragments: list[str], confidence: float) -> float:
        for phrase in cls._generic_phrases:
            query = query.replace(phrase, "")
        query_text = cls._normalized_text(query)
        query_variants = {query_text, query_text.replace('产生', '生成')}
        title_text = cls._normalized_text(title)
        title_core = re.sub(r"^(说明|运用|识别|解释|认识|掌握)", "", title_text)
        matched = [fragment for fragment in fragments if fragment in title_core]
        longest = max((len(item) for item in matched), default=0)
        fragment_coverage = len(matched) / max(1, len(fragments))
        containment = 1.0 if any(
            variant and (variant in title_core or (title_core in variant and len(title_core) >= .7 * len(variant)))
            for variant in query_variants
        ) else 0.0
        specificity = min(1.0, longest / 4)
        return round(0.52 * fragment_coverage + 0.22 * specificity + 0.16 * containment + 0.10 * confidence, 4)

    def _review_actions(self, entity_type: str, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        result: dict[str, str] = {}
        try:
            with closing(self._app_connection()) as connection:
                for start in range(0, len(keys), 500):
                    batch = keys[start:start + 500]
                    marks = ",".join("?" for _ in batch)
                    rows = connection.execute(
                        f"SELECT entity_key,action FROM kg_atomic_review_decision WHERE entity_type=? AND entity_key IN ({marks})",
                        (entity_type, *batch),
                    ).fetchall()
                    result.update({str(row["entity_key"]): str(row["action"]) for row in rows})
        except sqlite3.OperationalError:
            return {}
        return result

    @staticmethod
    def _policy(status: str) -> dict[str, Any]:
        if status == "approved":
            return {
                "mode": "reviewed_context",
                "may_explain": True,
                "may_generate_diagnostic_question": True,
                "may_update_mastery_without_answer_evidence": False,
            }
        return {
            "mode": "hypothesis_only",
            "may_explain": False,
            "may_generate_diagnostic_question": True,
            "may_update_mastery_without_answer_evidence": False,
        }

    @staticmethod
    def _draft_evidence(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, dict[str, str]]:
        missing = [str(row['draft_id']) for row in rows if not row['source_ref'] and not row['evidence_text']]
        if not missing:
            return {}
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('kg_atomic_draft_evidence','kg_resource')"
        )}
        if tables != {'kg_atomic_draft_evidence', 'kg_resource'}:
            return {}
        evidence_columns = {row[1] for row in connection.execute('PRAGMA table_info(kg_atomic_draft_evidence)')}
        resource_columns = {row[1] for row in connection.execute('PRAGMA table_info(kg_resource)')}
        if not {'draft_id', 'exact_quote', 'resource_id', 'confidence', 'evidence_id'} <= evidence_columns:
            return {}
        if not {'resource_id', 'source_ref', 'review_status'} <= resource_columns:
            return {}
        result: dict[str, dict[str, str]] = {}
        for start in range(0, len(missing), 500):
            batch = missing[start:start + 500]
            marks = ','.join('?' for _ in batch)
            evidence_rows = connection.execute(
                f'''SELECT e.draft_id,e.exact_quote,r.source_ref
                    FROM kg_atomic_draft_evidence e JOIN kg_resource r ON r.resource_id=e.resource_id
                    WHERE e.draft_id IN ({marks}) AND r.review_status <> 'rejected'
                    ORDER BY e.confidence DESC,e.evidence_id''', batch,
            ).fetchall()
            for evidence in evidence_rows:
                result.setdefault(str(evidence['draft_id']), {
                    'quote': str(evidence['exact_quote'] or '')[:240],
                    'source_ref': evidence['source_ref'],
                })
        return result

    @staticmethod
    def _audited_evidence(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, dict[str, str | None]]:
        """Apply explicit per-node source decisions without deleting historic evidence."""
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'kg_atomic_evidence_review' not in tables or not rows:
            return {}
        required = {'draft_id', 'evidence_kind', 'evidence_id', 'action', 'updated_at'}
        columns = {row[1] for row in connection.execute('PRAGMA table_info(kg_atomic_evidence_review)')}
        unavailable = {'quote': '', 'source_ref': None, 'review_status': 'unavailable'}
        if not required <= columns:
            return {str(row['draft_id']): unavailable for row in rows}
        by_id = {str(row['draft_id']): row for row in rows}
        result: dict[str, dict[str, str | None]] = {}
        for start in range(0, len(by_id), 500):
            batch = list(by_id)[start:start + 500]
            marks = ','.join('?' for _ in batch)
            decisions = connection.execute(f'''SELECT draft_id,evidence_kind,evidence_id,action,updated_at
                FROM kg_atomic_evidence_review WHERE draft_id IN ({marks})
                ORDER BY updated_at DESC,evidence_id''', batch).fetchall()
            for decision in decisions:
                draft_id = str(decision['draft_id'])
                result.setdefault(draft_id, unavailable)
                if decision['action'] != 'approved' or result[draft_id]['review_status'] == 'source_checked':
                    continue
                if not {'kg_resource', 'kg_segment'} <= tables:
                    continue
                try:
                    if decision['evidence_kind'] == 'draft_evidence' and 'kg_atomic_draft_evidence' in tables:
                        evidence = connection.execute('''SELECT e.exact_quote AS quote,e.quote_sha256 AS digest,
                            r.source_ref,s.text FROM kg_atomic_draft_evidence e
                            JOIN kg_resource r ON r.resource_id=e.resource_id
                            JOIN kg_segment s ON s.segment_id=e.segment_id AND s.resource_id=e.resource_id
                            WHERE e.evidence_id=? AND e.draft_id=? AND r.review_status<>'rejected' ''',
                            (decision['evidence_id'], draft_id)).fetchone()
                    elif decision['evidence_kind'] == 'candidate' and 'kg_atomic_candidate' in tables:
                        evidence = connection.execute('''SELECT c.evidence_text AS quote,NULL AS digest,
                            COALESCE(c.source_ref,r.source_ref) AS source_ref,r.source_ref AS resource_ref,s.text FROM kg_atomic_candidate c
                            JOIN kg_resource r ON r.resource_id=c.resource_id
                            JOIN kg_segment s ON s.segment_id=c.segment_id AND s.resource_id=c.resource_id
                            WHERE c.candidate_id=? AND c.canonical_signature=? AND r.review_status<>'rejected' ''',
                            (decision['evidence_id'], by_id[draft_id]['canonical_signature'])).fetchone()
                    else:
                        evidence = None
                except sqlite3.Error:
                    evidence = None
                if not evidence:
                    continue
                quote = str(evidence['quote'] or '')
                if decision['evidence_kind'] == 'candidate':
                    source, resource = str(evidence['source_ref'] or ''), str(evidence['resource_ref'] or '')
                    if not resource or not (source == resource or source.startswith(resource + ':') or source.startswith(resource + '#')):
                        continue
                if (not quote or not evidence['source_ref'] or quote not in str(evidence['text'] or '')
                        or decision['evidence_kind'] == 'draft_evidence' and
                        hashlib.sha256(quote.encode('utf-8')).hexdigest() != evidence['digest']):
                    continue
                result[draft_id] = {'quote': quote[:240], 'source_ref': str(evidence['source_ref']),
                                    'review_status': 'source_checked'}
        return result

    @staticmethod
    def _node(row: sqlite3.Row, *, role: str, relevance: float, status: str,
              fallback_evidence: dict[str, str] | None = None,
              audited_evidence: dict[str, str | None] | None = None) -> dict[str, Any]:
        diagnostics = json_value(row["diagnostic_blueprint_json"], [])
        diagnostic_layers = {
            str(item.get("level", f"level_{index}")): item
            for index, item in enumerate(diagnostics) if isinstance(item, dict)
        }
        evidence = audited_evidence if audited_evidence is not None else fallback_evidence or {
            'quote': str(row['evidence_text'] or '')[:240], 'source_ref': row['source_ref'],
        }
        evidence_review_status = (str(audited_evidence['review_status']) if audited_evidence is not None
                                  else 'unverified' if evidence.get('quote') and evidence.get('source_ref')
                                  else 'unavailable')
        return {
            "key": row["canonical_signature"],
            "title": row["title"],
            "grade_band": row["grade_band"],
            "subject": row["subject"],
            "chapter_paths": json_value(row["chapters_json"], []),
            "atom_kind": row["atom_kind"],
            "definition": row["observable_outcome"],
            "capability_boundary": json_value(row["boundary_json"], {}),
            "diagnostic_layers": {
                level: diagnostic_layers.get(level, {})
                for level in ("recognition", "independent", "transfer")
            },
            "mastery_criteria": json_value(row["mastery_criteria_json"], {}),
            "misconception_hypotheses": json_value(row["misconception_hypotheses_json"], []),
            "standard_alignments": json_value(row["standard_alignments_json"], []),
            "evidence": {'quote': evidence.get('quote') or '', 'source_ref': evidence.get('source_ref')},
            "evidence_review_status": evidence_review_status,
            "confidence": round(float(row["confidence"]), 4),
            "review_status": status,
            "graph_role": role,
            "relevance": relevance,
            "usage_policy": AtomicGraphCompanionService._policy(status),
        }

    def context_for_child(self, *, child_id: str, query: str, subject: str | None = None,
                          limit: int = 6) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ValueError("检索问题不能为空。")
        if len(query) > 240:
            raise ValueError("检索问题不能超过 240 个字符。")
        if subject and len(subject) > 40:
            raise ValueError("学科名称过长。")
        limit = max(1, min(int(limit), 8))
        grade = self._child_grade(child_id)
        grade_bands = compatible_grade_bands(grade)
        if not grade_bands:
            raise ValueError(f"暂不支持孩子年级：{grade}")
        fragments = self._query_fragments(query)
        if not fragments:
            raise ValueError("问题中没有可用于知识图谱检索的关键词。")
        anchors = self._query_anchors(query, fragments)

        grade_marks = ",".join("?" for _ in grade_bands)
        normalized_english_title = "replace(replace(replace(lower(d.title), ' ', ''), '.', ''), '…', '')"
        fragment_clause = " OR ".join(
            f"{normalized_english_title} LIKE ? ESCAPE '\\'"
            if re.fullmatch(r"[a-z0-9]+", fragment) else "d.title LIKE ? ESCAPE '\\'"
            for fragment in fragments
        )
        params: list[Any] = [*grade_bands]
        clauses = [f"d.grade_band IN ({grade_marks})", f"({fragment_clause})"]
        escaped_fragments = [
            fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            for fragment in fragments
        ]
        params.extend("%" + fragment + "%" for fragment in escaped_fragments)
        if subject:
            clauses.append("d.subject=?")
            params.append(subject)
        query_sql = f"""
            SELECT d.*,
              COALESCE((SELECT json_group_array(DISTINCT c.unit_path) FROM kg_atomic_candidate c
                        WHERE c.canonical_signature=d.canonical_signature AND c.unit_path<>''),'[]') AS chapters_json,
              (SELECT c.source_ref FROM kg_atomic_candidate c WHERE c.canonical_signature=d.canonical_signature
                       ORDER BY c.confidence DESC LIMIT 1) AS source_ref,
              (SELECT c.evidence_text FROM kg_atomic_candidate c WHERE c.canonical_signature=d.canonical_signature
                       ORDER BY c.confidence DESC LIMIT 1) AS evidence_text
            FROM kg_atomic_definition_draft d
            WHERE {' AND '.join(clauses)}
            LIMIT 30000
        """
        with closing(self._build_connection()) as connection:
            candidate_rows = connection.execute(query_sql, params).fetchall()
            fallback_parts: tuple[str, str] | None = None
            fallback_variants: tuple[tuple[str, ...], tuple[str, ...]] | None = None
            fallback_uses_tags = False
            if not candidate_rows and len(fragments) == 1 and re.fullmatch(r"[\u3400-\u9fff]{4}", fragments[0]):
                # A short spoken query may omit a particle present in the title,
                # such as 圆锥体积 versus 圆锥的体积. Require both halves in
                # the same title so a match on only one broad concept is not enough.
                fallback_parts = (fragments[0][:2], fragments[0][2:])
                equivalents = {'生殖': ('生殖', '繁殖'), '繁殖': ('繁殖', '生殖'),
                               '产生': ('产生', '生成'), '生成': ('生成', '产生')}
                fallback_variants = tuple(equivalents.get(part, (part,)) for part in fallback_parts)
                columns = {row[1] for row in connection.execute('PRAGMA table_info(kg_atomic_definition_draft)')}
                fallback_uses_tags = 'concept_tags_json' in columns
                fallback_columns = ('d.title', 'd.concept_tags_json') if fallback_uses_tags else ('d.title',)
                fallback_clause = ' AND '.join(
                    '(' + ' OR '.join(f"{column} LIKE ? ESCAPE '\\'" for _ in variants for column in fallback_columns) + ')'
                    for variants in fallback_variants
                )
                fallback_params: list[Any] = [*grade_bands, *(
                    f'%{variant}%' for variants in fallback_variants for variant in variants for _ in fallback_columns
                )]
                if subject:
                    fallback_params.append(subject)
                candidate_rows = connection.execute(query_sql.replace(fragment_clause, fallback_clause), fallback_params).fetchall()

            draft_actions = self._review_actions(
                "draft", [str(row["canonical_signature"]) for row in candidate_rows]
            )
            ranked: list[tuple[float, sqlite3.Row, str]] = []
            for row in candidate_rows:
                status = _effective_status(draft_actions.get(row["canonical_signature"]), row["review_status"])
                if status == "rejected":
                    continue
                title_core = self._title_core(str(row["title"]))
                fallback_text = title_core
                if fallback_uses_tags:
                    fallback_text += self._normalized_text(str(row['concept_tags_json'] or ''))
                if not any(anchor in title_core for anchor in anchors) and not (
                    fallback_variants and all(any(variant in fallback_text for variant in variants)
                                              for variants in fallback_variants)
                ):
                    continue
                score = self._relevance(query, row["title"], fragments, float(row["confidence"]))
                ranked.append((score, row, status))
            normalized_query = query
            for phrase in self._generic_phrases:
                normalized_query = normalized_query.replace(phrase, "")
            normalized_query = self._normalized_text(normalized_query)
            requested_sign = self._sign_conditions(query)
            if requested_sign:
                ranked = [item for item in ranked
                          if requested_sign <= self._sign_conditions(str(item[1]['title']))]
            elif not requested_sign and self._unknown_sign(query):
                ranked = [item for item in ranked
                          if self._unknown_sign(str(item[1]['title']))]
            normalized_query_variants = {normalized_query, normalized_query.replace('产生', '生成')}
            topic = re.sub(r'^(?:帮我|讲讲|讲解|解释|分析|介绍|复习|学习|说明|判断|计算|什么是)+', '', normalized_query)
            # A single-character subject followed by a conjunction is still a
            # meaningful topic. Match the other participant as well; otherwise
            # a shared word such as “reaction” can rank an unrelated pairing first.
            pair = re.match(r'^([\u3400-\u9fff])[和与及]([\u3400-\u9fff]{2,})', topic)
            pair_entity = ''
            if pair:
                tail = pair.group(2)
                boundary = re.match(r'^(.{2,}?)(?:的|反应|变化|对比|比较|区别|关系|相同|不同|产生|生成|作用|用途|性质|结合|接触)', tail)
                if boundary:
                    pair_entity = boundary.group(1)
                elif len(tail) <= 4:
                    pair_entity = tail
                def mentions_pair_together(row: sqlite3.Row) -> bool:
                    title = re.sub(r'^(?:说明|运用|识别|解释|认识|掌握|理解|说出|写出|判断|比较|区分|复述|计算|分析)+',
                                   '', str(row['title']))
                    if not pair_entity:
                        return False
                    for first in re.finditer(re.escape(pair.group(1)), title):
                        if first.start() and title[first.start() - 1] not in '与和及对把将使为在中于、，,；;（(':
                            continue
                        for second in re.finditer(re.escape(pair_entity), title):
                            start = min(first.end(), second.end())
                            end = max(first.start(), second.start())
                            between = title[start:end]
                            if len(between) <= 12 and len(re.findall(r'[，,、；;]', between)) <= 1:
                                return True
                    return False
                # An unresolved second participant is not a safe reason to
                # claim an association based on a prefix or unrelated list.
                ranked = [item for item in ranked if mentions_pair_together(item[1])]
            topic_head = topic[:2] if 5 <= len(topic) <= 18 and re.fullmatch(r'[\u3400-\u9fff]+', topic) else ''
            if topic_head and pair:
                topic_head = pair.group(1)
            if topic_head in {'数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理'}:
                topic_head = ''
            condition_topic = (topic[:3] if len(topic) >= 3 and topic[2] not in '的和与跟是'
                               else topic_head)
            if (requested_sign or self._unknown_sign(query)) and topic_head not in {'', '正数', '负数'}:
                ranked = [item for item in ranked
                          if condition_topic in self._title_core(str(item[1]['title']))]
            generic_terms = {'方式', '方法', '原因', '特点', '关系', '情况', '内容', '问题',
                             '知识', '概念', '作用', '影响', '多少', '什么', '如何', '意思'}
            remainder = topic[2:] if topic_head else ''
            topic_terms = [remainder[index:index + 2] for index in range(len(remainder) - 1)]
            topic_terms = [term for term in topic_terms if term not in generic_terms
                           and term[0] not in '的与和及' and term[-1] not in '的与和及']
            def matches_topic_head(row: sqlite3.Row) -> bool:
                if not topic_head or not topic_terms:
                    return False
                title = self._title_core(str(row['title']))
                if pair:
                    return topic_head in title and pair_entity in title
                return topic_head in title and any(term in title for term in topic_terms)
            known_topic = topic_head and any(
                self._title_core(str(item[1]['title'])).startswith(topic_head) for item in ranked
            )
            if known_topic and topic_terms and not any(matches_topic_head(item[1]) for item in ranked):
                ranked = []
            def match_position(row: sqlite3.Row) -> int:
                title = self._title_core(str(row["title"]))
                positions = [title.find(variant) for variant in normalized_query_variants if variant]
                return min((position for position in positions if position >= 0), default=1_000_000)
            def has_requested_formula(row: sqlite3.Row) -> bool:
                if not normalized_query.endswith('公式'):
                    return False
                formula_topic = normalized_query[:-2].removesuffix('的')
                return bool(formula_topic and formula_topic in self._normalized_text(str(row['title']))
                            and re.search(r'[=＝]', str(row['title'])))
            def matches_fallback_in_title(row: sqlite3.Row) -> bool:
                title = self._title_core(str(row['title']))
                return bool(fallback_variants and all(any(variant in title for variant in variants)
                                                      for variants in fallback_variants))
            def matches_pair_outcome(row: sqlite3.Row) -> bool:
                outcome_terms = ('产物', '生成', '产生')
                if not pair or not any(term in topic for term in outcome_terms):
                    return False
                title = self._title_core(str(row['title']))
                return matches_topic_head(row) and any(term in title for term in outcome_terms)
            def fallback_topic_position(row: sqlite3.Row) -> int:
                if not fallback_variants:
                    return 1_000_000
                title = self._title_core(str(row['title']))
                positions = [title.find(variant) for variant in fallback_variants[0]]
                return min((position for position in positions if position >= 0), default=1_000_000)
            def requested_sign_rank(row: sqlite3.Row) -> int:
                if len(requested_sign) != 1:
                    return 0
                signs = self._sign_conditions(str(row['title']))
                return 0 if signs == requested_sign else (1 if requested_sign & signs else 2)
            def sign_context_coverage(row: sqlite3.Row) -> int:
                if not requested_sign:
                    return 0
                # Preserve punctuation here: stripping it can fabricate a
                # fragment across unrelated clauses (e.g. 正数，方程).
                title = str(row['title']).lower()
                return sum(fragment in title for fragment in fragments)
            # In mixed Chinese/English grammar questions, the requested English
            # word is a stronger discriminator than common words such as 从句.
            # Check word boundaries so e.g. "if" does not match "different".
            english_terms = [fragment for fragment in fragments
                             if subject == '英语' and re.fullmatch(r'[a-z]{2,}', fragment)]
            chinese_terms = {
                chunk[index:index + 4]
                for chunk in re.findall(r'[\u3400-\u9fff]{4,}', query)
                for index in range(len(chunk) - 3)
            } if english_terms else set()
            def matching_english_terms(row: sqlite3.Row) -> int:
                title = str(row['title']).lower()
                return sum(bool(re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', title))
                           for term in english_terms)
            def matching_chinese_terms(row: sqlite3.Row) -> int:
                title = str(row['title'])
                return sum(term in title for term in chinese_terms)
            def unrequested_comparison(row: sqlite3.Row) -> bool:
                if not english_terms or re.search(r'区别|比较|区分|异同|差异|对比|相较', query):
                    return False
                return bool(re.match(r'^(区分|比较|辨析)', str(row['title'])))
            ranked.sort(key=lambda item: (
                requested_sign_rank(item[1]), -sign_context_coverage(item[1]),
                not matches_topic_head(item[1]), not matches_pair_outcome(item[1]),
                not matches_fallback_in_title(item[1]), fallback_topic_position(item[1]),
                -matching_english_terms(item[1]), -matching_chinese_terms(item[1]),
                unrequested_comparison(item[1]), not has_requested_formula(item[1]),
                -round(item[0], 2), match_position(item[1]),
                item[2] != 'approved', -item[0],
                -len(self._title_core(str(item[1]["title"]))),
                -float(item[1]["confidence"]), item[1]["title"],
            ))
            selected = []
            seen_titles: list[tuple[str, str]] = []
            for item in ranked:
                title = str(item[1]["title"])
                item_subject = str(item[1]["subject"])
                if self._is_near_duplicate(title, item_subject, seen_titles):
                    continue
                seen_titles.append((self._title_core(title), item_subject))
                selected.append(item)
                if len(selected) >= limit:
                    break
            seed_keys = [str(item[1]["canonical_signature"]) for item in selected]

            relation_rows: list[sqlite3.Row] = []
            if seed_keys:
                marks = ",".join("?" for _ in seed_keys)
                relation_rows = connection.execute(
                    f"""SELECT * FROM kg_atomic_relation_candidate
                        WHERE source_key IN ({marks}) OR target_key IN ({marks})
                        ORDER BY confidence DESC LIMIT ?""",
                    (*seed_keys, *seed_keys, limit * 6),
                ).fetchall()
            relation_actions = self._review_actions(
                "relation", [str(row["relation_id"]) for row in relation_rows]
            )
            kept_relations: list[tuple[sqlite3.Row, str]] = []
            neighbor_keys: list[str] = []
            for row in relation_rows:
                status = _effective_status(relation_actions.get(row["relation_id"]), row["review_status"])
                if status == "rejected":
                    continue
                kept_relations.append((row, status))
                for key in (str(row["source_key"]), str(row["target_key"])):
                    if key not in seed_keys and key not in neighbor_keys:
                        neighbor_keys.append(key)

            neighbor_rows: list[sqlite3.Row] = []
            if neighbor_keys:
                bounded_keys = neighbor_keys[:limit * 3]
                marks = ",".join("?" for _ in bounded_keys)
                neighbor_rows = connection.execute(
                    f"""SELECT d.*,
                          COALESCE((SELECT json_group_array(DISTINCT c.unit_path) FROM kg_atomic_candidate c
                                    WHERE c.canonical_signature=d.canonical_signature AND c.unit_path<>''),'[]') AS chapters_json,
                          (SELECT c.source_ref FROM kg_atomic_candidate c WHERE c.canonical_signature=d.canonical_signature
                                   ORDER BY c.confidence DESC LIMIT 1) AS source_ref,
                          (SELECT c.evidence_text FROM kg_atomic_candidate c WHERE c.canonical_signature=d.canonical_signature
                                   ORDER BY c.confidence DESC LIMIT 1) AS evidence_text
                        FROM kg_atomic_definition_draft d
                        WHERE d.canonical_signature IN ({marks}) AND d.grade_band IN ({grade_marks})
                          {"AND d.subject=?" if subject else ""}""",
                    (*bounded_keys, *grade_bands, *((subject,) if subject else ())),
                ).fetchall()

            selected_evidence = self._draft_evidence(connection, [item[1] for item in selected])
            neighbor_evidence = self._draft_evidence(connection, neighbor_rows)
            audited_evidence = self._audited_evidence(connection, [item[1] for item in selected] + list(neighbor_rows))

        neighbor_actions = self._review_actions(
            "draft", [str(row["canonical_signature"]) for row in neighbor_rows]
        )
        nodes = [self._node(row, role="matched", relevance=score, status=status,
                            fallback_evidence=selected_evidence.get(str(row['draft_id'])),
                            audited_evidence=audited_evidence.get(str(row['draft_id'])))
                 for score, row, status in selected]
        neighbor_count = 0
        for row in neighbor_rows:
            status = _effective_status(neighbor_actions.get(row["canonical_signature"]), row["review_status"])
            title = str(row["title"])
            item_subject = str(row["subject"])
            if status == "rejected" or self._is_near_duplicate(title, item_subject, seen_titles):
                continue
            seen_titles.append((self._title_core(title), item_subject))
            nodes.append(self._node(row, role="one_hop_neighbor", relevance=0.0, status=status,
                                    fallback_evidence=neighbor_evidence.get(str(row['draft_id'])),
                                    audited_evidence=audited_evidence.get(str(row['draft_id']))))
            neighbor_count += 1
            if neighbor_count >= limit * 2:
                break
        node_keys = {str(node["key"]) for node in nodes}
        relations = []
        for row, status in kept_relations:
            if row["source_key"] not in node_keys or row["target_key"] not in node_keys:
                continue
            relations.append({
                "key": row["relation_id"],
                "source_key": row["source_key"],
                "target_key": row["target_key"],
                "type": row["relation_type"],
                "direction": "source_to_target",
                "rationale": str(row["rationale"] or "")[:240],
                "confidence": round(float(row["confidence"]), 4),
                "review_status": status,
                "usage_policy": self._policy(status),
            })

        title_by_key = {str(node["key"]): str(node["title"]) for node in nodes}
        weakness_hypotheses = []
        for relation in relations:
            if relation["type"] == "prerequisite_of" and relation["target_key"] in seed_keys:
                weakness_hypotheses.append({
                    "candidate_key": relation["source_key"],
                    "prompt": f"先检查前置知识“{title_by_key.get(relation['source_key'], '相邻前置知识')}”是否掌握。",
                    "basis": relation["key"],
                    "status": "question_required",
                })
            elif relation["type"] == "part_of" and relation["source_key"] in seed_keys:
                weakness_hypotheses.append({
                    "candidate_key": relation["target_key"],
                    "prompt": f"把当前困难放回“{title_by_key.get(relation['target_key'], '上位知识')}”脉络中追问定位。",
                    "basis": relation["key"],
                    "status": "question_required",
                })

        approved_count = sum(node["review_status"] == "approved" for node in nodes)
        event_id = self._record_usage(
            child_id=child_id,
            query=query,
            grade_bands=grade_bands,
            subject=subject,
            nodes=nodes,
            approved_count=approved_count,
        ) if self._audit_usage else None
        return {
            "nodes": nodes,
            "relations": relations,
            "weakness_hypotheses": weakness_hypotheses[:limit],
            "recovery": None if nodes else {
                "state": "no_matching_atomic_context",
                "next_action": "请孩子补充学科、教材章节或一个更具体的概念，不要猜测知识点。",
            },
            "scope": {"profile_grade": grade, "compatible_grade_bands": list(grade_bands), "subject": subject},
            "audit_event_id": event_id,
            "truthfulness": {
                "build_database_read_only": True,
                "all_content_stays_local": True,
                "cloud_transmission_allowed": False,
                "contains_local_source_evidence": True,
                "unreviewed_content_is_hypothesis_only": True,
                "mastery_was_updated": False,
                "mastery_update_requires": "人工批准的知识节点和客观作答证据",
                "matched_node_limit": limit,
                "neighbor_node_limit": limit * 2,
                "loading_mode": "keyword_match_plus_one_hop",
            },
        }

    def _record_usage(self, *, child_id: str, query: str, grade_bands: tuple[str, ...],
                      subject: str | None, nodes: list[dict[str, Any]], approved_count: int) -> str:
        event_id = f"atomic-context-{uuid4()}"
        with self._app_connection() as connection:
            connection.execute(
                """INSERT INTO kg_companion_graph_context_event(
                     event_id,child_fingerprint,query_fingerprint,grade_scope_json,subject_scope,
                     result_count,approved_count,hypothesis_count,selected_keys_json,build_label,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    self._privacy_fingerprint(child_id),
                    self._privacy_fingerprint(query),
                    json.dumps(grade_bands, ensure_ascii=False),
                    subject,
                    len(nodes),
                    approved_count,
                    len(nodes) - approved_count,
                    json.dumps([node["key"] for node in nodes], ensure_ascii=False),
                    self.build_db.name if self.build_db else "unavailable",
                    utc_now(),
                ),
            )
        return event_id

    def usage_summary(self) -> dict[str, Any]:
        """Privacy-safe operator observability; never returns child/query fingerprints."""
        with self._app_connection() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS calls,COALESCE(SUM(result_count),0) AS nodes_returned,
                          COALESCE(SUM(approved_count),0) AS approved_nodes,
                          MAX(created_at) AS latest_at
                   FROM kg_companion_graph_context_event"""
            ).fetchone()
        return dict(row)
