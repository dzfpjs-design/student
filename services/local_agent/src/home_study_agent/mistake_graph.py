"""Local, evidence-scoped knowledge graph matching for saved mistakes."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
from contextlib import closing, nullcontext
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .database import Database
from .knowledge_graph.atomic_companion import (
    AtomicGraphChildNotFoundError,
    AtomicGraphCompanionService,
    AtomicGraphUnavailableError,
    compatible_grade_bands,
    _effective_status,
)
from .knowledge_graph.catalog_bootstrap import ALIASES


_MATCHER_VERSION = 'mistake-graph-v10'
_IGNORED_TITLES = {'错题', '原题', '待识别', '待核对', '重新做', '课堂练习', '手动录入错题', '高拍仪录入错题'}
_GENERIC_MATH_TERMS = {'解方程', '解题', '公式法', '计算方法', '运算方法'}
_VERB = re.compile(r'^(?:说明|运用|识别|解释|认识|掌握|理解|说出|确定|判断|计算|分析|区分|比较)')
_SEPARATORS = re.compile(r'[\W_]+', re.UNICODE)
_FRACTION = r'(?:\d+\s*[/⁄]\s*\d+|[一二三四五六七八九十百零〇两\d]+分之[一二三四五六七八九十百零〇两\d]+)'
_FRACTION_OPERATION = re.compile(rf'{_FRACTION}\s*(除以|乘以|÷|×|\+|－|−|-|加|减)\s*{_FRACTION}')
_OPERATION_LABELS = {
    '除以': '分数除法', '÷': '分数除法', '乘以': '分数乘法', '×': '分数乘法',
    '+': '分数加法', '加': '分数加法', '－': '分数减法', '−': '分数减法', '-': '分数减法', '减': '分数减法',
}
_GRADE_ALIASES = {'七年级': '初一', '八年级': '初二', '九年级': '初三', '初中一年级': '初一',
                  '初中二年级': '初二', '初中三年级': '初三'}
_GRADE_ORDER = {name: index for index, name in enumerate(
    ('一年级', '二年级', '三年级', '四年级', '五年级', '六年级', '初一', '初二', '初三'), start=1
)}
_SUBJECT_ALIASES = {'科学课': '科学', '道法': '道德与法治', '道德法治': '道德与法治', '生物学': '生物',
                    '信息技术': '信息科技', '体育': '体育与健康', '劳动': '劳动科技'}
_SUBJECT_TERMS = {
    '数学': ('一元二次方程', '一元一次方程', '一次函数', '二次函数', '反比例函数', '方程组', '判别式', '求根公式',
             '公式法求根', '分数除法', '分数乘法', '异分母', '同分母', '勾股定理', '平行线判定', '三角形内角和',
             '等腰三角形', '相似三角形', '全等三角形', '公因数', '公倍数', '圆周角'),
    '物理': ('欧姆定律', '牛顿第一定律', '牛顿第二定律', '牛顿第三定律', '电流表', '电压表', '凸透镜', '凹透镜',
             '浮力', '阿基米德原理', '受力面积', '弹簧测力计', '串联电路', '并联电路', '电磁感应', '机械能守恒', '电功率'),
    '化学': ('化学方程式', '化学反应方程式', '化学式', '相对原子质量', '相对分子质量', '氧化还原', '中和反应',
             '化合价', '元素周期表', '酸碱指示剂'),
    '生物': ('光合作用', '细胞分裂', '染色体', '显性基因', '隐性基因', '细胞膜', '动物细胞', '植物细胞', '反射弧',
             '消化系统', '人体血液循环', '孟德尔', '食物链'),
    '地理': ('地球自转', '地球公转', '经纬网', '纬线', '本初子午线', '昼夜更替', '季风气候', '等高线', '时区换算',
             '地球仪', '板块运动', '海陆变迁', '黄河流域'),
    '历史': ('辛亥革命', '鸦片战争', '五四运动', '新文化运动', '商鞅变法', '安史之乱', '工业革命', '法国大革命',
             '明治维新', '汉武帝', '文艺复兴', '十月革命', '抗日战争'),
    '语文': ('修辞手法', '比喻句', '人物描写', '文言文', '古诗鉴赏', '病句修改', '汉字偏旁', '拼音', '句式仿写', '记叙顺序'),
    '英语': ('一般过去时', '一般现在时', '现在完成时', '过去完成时', '现在进行时', '情态动词', '定语从句',
             '状语从句', '一般疑问句', '被动语态', '英语语法', '英文单词', '宾语从句'),
    '道德与法治': ('宪法监督', '未成年人保护法', '公民基本权利', '人民代表大会制度'),
}
_SUBJECT_NAMES = tuple(sorted({*_SUBJECT_TERMS, *_SUBJECT_ALIASES, *_SUBJECT_ALIASES.values(), '奥数', '美术', '音乐',
                              '综合实践活动'}, key=lambda name: (-len(name), name)))
_SUBJECT_TAG = '|'.join(map(re.escape, _SUBJECT_NAMES))
_EXPLICIT_SUBJECT = re.compile(
    rf'^[\s【\[（(]*({_SUBJECT_TAG})(?:题|学科|课)?(?:\s*[：:】\]）)]|\s*$)|(?:学科|科目)\s*[：:]\s*({_SUBJECT_TAG})',
    re.MULTILINE,
)
_QUADRATIC = re.compile(r'(?<![A-Za-z])[xyz]\s*\^\s*2\s*(?:[+−-][^=。！？\n]{1,60})?=\s*[-−]?\d', re.IGNORECASE)


def _normalize(text: str) -> str:
    return _SEPARATORS.sub('', unicodedata.normalize('NFKC', text).lower()).replace('的', '')


def _chunks(text: str) -> tuple[str, ...]:
    return tuple(filter(None, (_normalize(part) for part in re.split(r'[。；;，,、！？!?\r\n]+', text))))


def _subject(text: str) -> str:
    text = text.strip()
    return _SUBJECT_ALIASES.get(text, text)


def _subject_from_source(title: str, question: str) -> str:
    signals = {_subject(match.group(1) or match.group(2)) for text in (title, question)
               for match in _EXPLICIT_SUBJECT.finditer(text)}
    chunks = _chunks(title) + _chunks(question)
    for subject, terms in _SUBJECT_TERMS.items():
        if any(term in chunk for term in terms for chunk in chunks):
            signals.add(subject)
    math_source = unicodedata.normalize('NFKC', question.replace('²', '^2'))
    if _FRACTION_OPERATION.search(math_source) or _QUADRATIC.search(math_source):
        signals.add('数学')
    return next(iter(signals)) if len(signals) == 1 else ''


def _grade(text: str) -> str:
    return _GRADE_ALIASES.get(text, text.removeprefix('小学'))


def _terms(title: str, aliases: Sequence[str]) -> tuple[str, ...]:
    # Qualifiers in the full title still matter. A parenthetical technique can
    # clarify an otherwise generic skill without becoming a standalone match.
    main = re.split(r'[(（，,；;]', title, maxsplit=1)[0]
    candidates = [_normalize(_VERB.sub('', main)), _normalize(_VERB.sub('', title)), *map(_normalize, aliases)]
    return tuple(dict.fromkeys(term for term in candidates if len(term) >= 3 and term not in _IGNORED_TITLES))


def _comparison_participants(chunks: Sequence[str]) -> list[tuple[str, str]]:
    pairs = []
    for chunk in chunks:
        stripped = re.sub(r'^(?:(?:请|帮我|解释|比较|区分|辨析|对比))+', '', chunk)
        match = re.fullmatch(r'(.{2,30}?)[和与跟](.{2,30}?)(?:之间)?(?:区别|不同|关系|异同|差异)', stripped)
        if match:
            pairs.append((match.group(1), match.group(2)))
    return pairs


def _question_core(chunk: str) -> str:
    """Remove ordinary question wrappers and stated measurements, not topic qualifiers."""
    text = re.sub(r'(?:底|高|长|宽|半径|直径|边长)(?:是|为)?\d+(?:千米|厘米|毫米|米|cm|mm|m)?', '', chunk)
    text = re.sub(r'^(?:(?:请问|请|帮我|已知|求出|求|计算|解释|说明|理解|掌握|认识|运用|用))+', '', text)
    text = re.sub(r'(?:是多少|是什么|怎么算|怎样算|怎么求|如何求|求法|公式|计算|多少|吗)$', '', text)
    return re.sub(r'(?:两个|两种|一个)', '', text)


def _has_unmatched_qualification(core: str, term: str) -> bool:
    # The question core removes generic counting words; normalize the title
    # the same way before checking whether any condition remains unmatched.
    term = re.sub(r'(?:两个|两种|一个)', '', term)
    if term not in core or core == term:
        return False
    remainder = core.replace(term, '', 1)
    remainder = re.sub(r'(?:在实数范围内|解方程|计算结果|求结果|求出|计算|求值|说明|解释|用)', '', remainder)
    remainder = re.sub(r'[a-z0-9]+', '', remainder)
    return bool(remainder)


def _contains_term(text: str, term: str) -> bool:
    if re.fullmatch(r'[a-z]{2,}', term):
        return bool(re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', text))
    return term in text


def _incomplete_title(title: str) -> bool:
    return bool(re.search(r'(?:[，,、；;：:]|[，,](?:若|如果|当|因为|但是|以及))$', title.strip()))


def _source_overlap_supports_candidate(query: str, title: str) -> bool:
    """Two independent literal source overlaps permit review, never a proven match."""
    query_text, title_text = _normalize(query), _normalize(title)
    raw_fragments = list(AtomicGraphCompanionService._query_fragments(query))
    if not re.search(r'[\u3400-\u9fff]', query):
        # Common sentence glue alone does not identify a grammar objective.
        # In bilingual questions these same words may themselves be the topic.
        glue = {'a', 'an', 'the', 'i', 'he', 'she', 'it', 'we', 'you', 'they',
                'here', 'there', 'have', 'has', 'had', 'is', 'are', 'was', 'were',
                'do', 'does', 'did', 'to', 'of', 'at', 'in', 'on', 'for', 'with',
                'by', 'from', 'and', 'or'}
        raw_fragments = [fragment for fragment in raw_fragments if fragment not in glue]
    pair = re.match(r'^([\u3400-\u9fff])[和与及]', query_text)
    if pair:
        raw_fragments.append(pair.group(1))
    fragments = sorted(set(map(_normalize, raw_fragments)),
                       key=lambda item: (-len(item), item))
    query_used: set[int] = set()
    title_used: set[int] = set()
    query_ascii_used: set[int] = set()
    title_ascii_used: set[int] = set()
    count = total = 0
    for fragment in fragments:
        if len(fragment) < 2 and not (pair and fragment == pair.group(1)):
            continue
        ascii_word = bool(re.fullmatch(r'[a-z0-9]{2,}', fragment))
        pattern = (r'(?<![a-z0-9])' + re.escape(fragment) + r'(?![a-z0-9])'
                   if ascii_word else re.escape(fragment))
        q_text = unicodedata.normalize('NFKC', query).lower() if ascii_word else query_text
        t_text = unicodedata.normalize('NFKC', title).lower() if ascii_word else title_text
        q_used = query_ascii_used if ascii_word else query_used
        t_used = title_ascii_used if ascii_word else title_used
        for q_match in re.finditer(pattern, q_text):
            q_span = set(range(q_match.start(), q_match.end()))
            if q_span & q_used:
                continue
            for t_match in re.finditer(pattern, t_text):
                t_span = set(range(t_match.start(), t_match.end()))
                if not t_span & t_used:
                    q_used.update(q_span)
                    t_used.update(t_span)
                    count += 1
                    total += len(fragment)
                    break
            else:
                continue
            break
    return count >= 2 and total >= (3 if pair else 4)


def _score(node: Mapping[str, Any], titles: Sequence[str], question: Sequence[str], inferred: Sequence[str],
           hints: Sequence[str]) -> float:
    terms = _terms(node['title'], node.get('aliases', ()))
    comparisons = _comparison_participants((*titles, *question))
    full_title = _normalize(node['title'])
    individual_cores = tuple(filter(None, (_question_core(chunk) for chunk in question)))
    cores = tuple(dict.fromkeys((*individual_cores, _question_core(''.join(question)))))
    scores = []
    for term in terms:
        direct_question = any(_contains_term(chunk, term) for chunk in question)
        direct_title = any(_contains_term(chunk, term) for chunk in titles)
        if direct_question:
            evidence = .94 if term in question else .9
            if direct_title:
                evidence += .03
        elif direct_title:
            evidence = .91 if term in titles else .85
        else:
            # A broad lexical overlap or model-only label can be shown for
            # inspection, but cannot establish the primary graph node.
            evidence = .68 if any(term == hint or (len(hint) >= 4 and hint in term) for hint in hints) else 0
            if any(len(chunk) >= 4 and chunk in term for chunk in (*titles, *question, *inferred)):
                evidence = max(evidence, .72)
            # A short yes/no question can contain the full topic while the
            # node additionally states the answer or its assumptions. Surface
            # that evidence for inspection, but do not equate a contained topic
            # with a fully verified knowledge-point match.
            for chunk in (*titles, *question):
                core = re.sub(r'(?:可以吗|能够吗|能吗|行吗|对吗|可不可以|能不能)$', '', chunk)
                if core != chunk and len(core) >= 3 and core in term:
                    evidence = max(evidence, .72)
        if term in inferred:
            evidence = max(evidence, .98 if node['title'].startswith(('计算', '求')) else .86)
        if any(core == term for core in cores):
            evidence = max(evidence, .92)
        if any(len(core) >= 4 and core in full_title for core in cores):
            evidence = max(evidence, .78)
        if len(term) >= 9 and not direct_question:
            for chunk in question:
                match = SequenceMatcher(None, term, chunk, autojunk=False).find_longest_match()
                overlap = match.size
                # A shared topic can omit the question's actual operation or
                # condition. Similarity alone must not promote that partial
                # overlap to a confirmed skill match.
                remainder = _question_core(chunk[:match.b] + chunk[match.b + overlap:])
                remainder = re.sub(r'(?:怎么|如何|什么|为什么|怎样|请问|是否|可以|能够|吗)', '', remainder)
                if (overlap >= 8 and overlap / len(term) >= .72 and overlap / len(chunk) >= .45
                        and len(remainder) < 2):
                    evidence = max(evidence, .95)
        if evidence >= .8 and node['review_status'] == 'approved':
            evidence += .035
        if evidence >= .8 and any(len(hint) >= 3 and (hint == term or hint in term) for hint in hints):
            evidence += .01
        # A bare operation name is common in many distinct lessons. Keep
        # that local candidate visible without letting it tie an explicit skill.
        if term in _GENERIC_MATH_TERMS:
            evidence = min(evidence, .74)
        qualification_cores = (individual_cores if any(term in core for core in individual_cores)
                               else cores)
        if (term not in inferred and not any(term == chunk for chunk in question)
                and any(_has_unmatched_qualification(core, term) for core in qualification_cores)):
            # A subtopic or comparison requested in the original problem is
            # not established merely because a broad node's name occurs in it.
            evidence = min(evidence, .72)
        if (not any(term == chunk for chunk in question)
                and any(left not in full_title or right not in full_title for left, right in comparisons)):
            evidence = min(evidence, .72)
        if _incomplete_title(str(node['title'])):
            evidence = min(evidence, .64)
        scores.append(evidence)
    return round(min(.99, max(scores, default=0)), 3)


def _view(node: Mapping[str, Any], confidence: float) -> dict:
    return {'key': node['key'], 'title': node['title'], 'review_status': node['review_status'], 'confidence': confidence}


class MistakeGraphMatcher:
    """Match within the active child's subject and grade, without mutating mastery."""

    def __init__(self, database: Database, atomic_graph_db_path: Path | str | None):
        self.database = database
        self.atomic = AtomicGraphCompanionService(database.path, atomic_graph_db_path, audit_usage=False)

    def semantic_options(self, graph: Mapping[str, Any]) -> tuple[list[dict[str, str]], dict[str, str]]:
        """Export only demonstrably self-authored, short labels, never source excerpts."""
        if (graph.get('status') != 'ambiguous' or graph.get('candidates_truncated') is not False
                or not self.atomic.available):
            return [], {}
        alternatives = graph.get('alternatives')
        if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= 3:
            return [], {}
        options, mapping = [], {}
        try:
            with closing(self.atomic._build_connection()) as connection, closing(self.atomic._app_connection()) as app_connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                review_table = app_connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_atomic_review_decision'").fetchone()
                if not {'kg_atomic_draft_evidence', 'kg_resource', 'kg_atomic_label_cloud_right'} <= tables:
                    return [], {}
                for index, item in enumerate(alternatives, 1):
                    if not isinstance(item, Mapping) or not isinstance(item.get('key'), str):
                        return [], {}
                    row = connection.execute('''SELECT draft_id,title,review_status FROM kg_atomic_definition_draft
                        WHERE canonical_signature=?''', (item['key'],)).fetchone()
                    if not row or row['title'] != item.get('title'):
                        return [], {}
                    # A cloud-use right does not review the knowledge itself.
                    # Check the current effective review decision independently.
                    decision = (app_connection.execute('''SELECT action FROM kg_atomic_review_decision
                        WHERE entity_type='draft' AND entity_key=?''', (item['key'],)).fetchone()
                        if review_table else None)
                    if _effective_status(decision['action'] if decision else None, row['review_status']) != 'approved':
                        return [], {}
                    title = str(row['title']).strip()
                    if not 3 <= len(title) <= 80 or re.search(r'[\r\n。；;：:]|例如|如图', title):
                        return [], {}
                    columns={col[1] for col in connection.execute('PRAGMA table_info(kg_atomic_label_cloud_right)')}
                    expiry_column='expires_at' if 'expires_at' in columns else 'NULL AS expires_at'
                    right = connection.execute('''SELECT title_sha256,origin,status,basis,''' + expiry_column + ''' FROM kg_atomic_label_cloud_right
                        WHERE draft_id=?''', (row['draft_id'],)).fetchone()
                    if (not right or right['origin'] != 'project_authored' or right['status'] != 'active'
                            or not str(right['basis'] or '').strip()
                            or right['title_sha256'] != hashlib.sha256(title.encode('utf-8')).hexdigest()):
                        return [], {}
                    if right['expires_at'] is not None:
                        try:
                            expires=datetime.fromisoformat(right['expires_at'])
                            if expires.tzinfo is None or expires <= datetime.now(timezone.utc): return [], {}
                        except (TypeError,ValueError,OverflowError):
                            return [], {}
                    evidence = connection.execute('''SELECT e.exact_quote FROM kg_atomic_draft_evidence e
                        JOIN kg_resource r ON r.resource_id=e.resource_id
                        WHERE e.draft_id=? AND r.review_status<>'rejected' ''', (row['draft_id'],)).fetchall()
                    label_core = _normalize(_VERB.sub('', title))
                    if not evidence or any(len(label_core) >= 8 and label_core in _normalize(str(value[0] or ''))
                                           for value in evidence):
                        return [], {}
                    ref = f'C{index}'
                    options.append({'ref': ref, 'label': title})
                    mapping[ref] = item['key']
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return [], {}
        return options, mapping

    def cache_version(self, *, connection: sqlite3.Connection | None = None) -> str:
        packaged = self.atomic.packaged_identity()
        if packaged:
            asset = '@'.join(packaged)
        else:
            build = self.atomic.build_db
            try:
                stat = build.stat() if build else None
                wal = build.with_name(build.name + '-wal') if build else None
                wal_stat = wal.stat() if wal and wal.exists() else None
                revision = [build.name, stat.st_size, stat.st_mtime_ns] if stat and build else None
                if revision and wal_stat and wal_stat.st_size:
                    revision.extend((wal_stat.st_size, wal_stat.st_mtime_ns))
                asset = 'local@' + hashlib.sha256(repr(revision).encode()).hexdigest() if revision else 'missing'
            except OSError:
                asset = 'missing'
        digest = hashlib.sha256(_MATCHER_VERSION.encode())
        try:
            manager = nullcontext(connection) if connection is not None else self.database.connect()
            with manager as connection:
                tables = {row[0] for row in connection.execute('''SELECT name FROM sqlite_master WHERE type='table'
                    AND name IN ('kg_atomic_review_decision','child_profile','kg_node','kg_edge')''')}
                statements = {
                    'kg_atomic_review_decision': '''SELECT entity_type,entity_key,action,updated_at
                        FROM kg_atomic_review_decision ORDER BY entity_type,entity_key''',
                    'child_profile': "SELECT child_id,grade FROM child_profile WHERE status='active' ORDER BY child_id",
                    'kg_node': """SELECT canonical_key,title,scope_json,review_status,status,version_label FROM kg_node
                        WHERE license_boundary='project_curated_local' AND node_type='knowledge_point' ORDER BY canonical_key""",
                    'kg_edge': """SELECT e.source_node_id,e.target_node_id,e.review_status,e.version_label,e.confidence FROM kg_edge e
                        JOIN kg_node n ON n.node_id=e.target_node_id WHERE n.license_boundary='project_curated_local'
                        AND e.relation_type='prerequisite_of' ORDER BY e.edge_id""",
                }
                for table in sorted(tables):
                    if table == 'kg_edge' and 'kg_node' not in tables:
                        continue
                    for row in connection.execute(statements[table]):
                        digest.update(json.dumps((table, *row), ensure_ascii=True).encode())
        except (OSError, sqlite3.Error):
            digest.update(b'unavailable-app-graph')
        return f'{asset}+{_MATCHER_VERSION}@{digest.hexdigest()[:24]}'

    @staticmethod
    def _curated_scope(row: sqlite3.Row, grade: str, subject: str, *, allow_earlier: bool = False) -> bool:
        try:
            scope = json.loads(row['scope_json'])
        except (TypeError, ValueError):
            return False
        if not (isinstance(scope, dict) and scope.get('track') == 'school' and scope.get('subject') == subject
                and isinstance(scope.get('grade'), str)):
            return False
        node_grade = _grade(scope['grade'])
        child_grade = _grade(grade)
        if node_grade == child_grade:
            return True
        return bool(allow_earlier and node_grade in _GRADE_ORDER and child_grade in _GRADE_ORDER
                    and _GRADE_ORDER[node_grade] < _GRADE_ORDER[child_grade])

    def _curated(self, child_id: str, subject: str) -> tuple[str, list[dict], list[dict], list[dict]]:
        with self.database.connect() as connection:
            child = connection.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
            if child is None:
                return '', [], [], []
            grade = str(child['grade'])
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE name IN ('kg_node','kg_edge')")}
            if 'kg_node' not in tables:
                return grade, [], [], []
            rows = connection.execute('''SELECT node_id,canonical_key,title,scope_json,review_status FROM kg_node
                WHERE license_boundary='project_curated_local' AND node_type='knowledge_point'
                AND review_status='approved' AND status='active' ORDER BY canonical_key''').fetchall()
            sources = {row['node_id']: row for row in rows if self._curated_scope(row, grade, subject, allow_earlier=True)}
            targets = {node_id: row for node_id, row in sources.items() if self._curated_scope(row, grade, subject)}
            def view(row: sqlite3.Row) -> dict:
                return {'key': row['canonical_key'], 'title': row['title'], 'review_status': row['review_status'],
                        'aliases': ALIASES.get(row['canonical_key'].removeprefix('starter:'), ())}
            if not targets or 'kg_edge' not in tables:
                return grade, [view(row) for row in targets.values()], [], []
            marks = ','.join('?' for _ in targets)
            relations = []
            prerequisites = {}
            for edge in connection.execute(f'''SELECT source_node_id,target_node_id,review_status,confidence
                    FROM kg_edge WHERE target_node_id IN ({marks}) AND relation_type='prerequisite_of'
                    AND review_status='approved' ''', tuple(targets)):
                source = sources.get(edge['source_node_id'])
                if source is None:
                    continue
                prerequisites[edge['source_node_id']] = view(source)
                relations.append(dict(source_key=source['canonical_key'],
                                      target_key=targets[edge['target_node_id']]['canonical_key'],
                                      review_status=edge['review_status'], type='prerequisite_of',
                                      confidence=edge['confidence']))
            return grade, [view(row) for row in targets.values()], list(prerequisites.values()), relations

    def match(self, child_id: str, source: Mapping[str, Any], *, hints: Sequence = (),
              _verified_key: str | None = None) -> dict:
        version = self.cache_version()
        empty = {'status': 'no_match', 'graph_version': version, 'primary': None, 'prerequisites': [], 'alternatives': []}
        title = str(source.get('title') or '')[:120].strip()
        question = str(source.get('question') or '')[:240].strip()
        subject = _subject(str(source.get('subject') or ''))
        if subject not in _SUBJECT_NAMES:
            subject = _subject_from_source(title, question)
        if not subject:
            return empty
        empty['subject'] = subject
        # The learner's old wrong answer and external hints cannot supply
        # evidence for the primary association.
        raw_hints = [str(hint.get('label') or '')[:80].strip() if isinstance(hint, Mapping) else hint[:80].strip()
                     for hint in list(hints)[:3] if isinstance(hint, (str, Mapping))]
        inferred = tuple(dict.fromkeys(_OPERATION_LABELS[m.group(1)] for m in _FRACTION_OPERATION.finditer(
            unicodedata.normalize('NFKC', question)))) if subject == '数学' else ()
        if _normalize(title) in _IGNORED_TITLES:
            title = ''
        try:
            grade, curated_nodes, curated_prerequisites, relations = self._curated(child_id, subject)
        except (OSError, sqlite3.Error, ValueError):
            return empty | {'status': 'unavailable'}
        if not compatible_grade_bands(grade):
            return empty
        nodes = {node['key']: node for node in curated_nodes}
        eligible = set(nodes)
        nodes.update({node['key']: node for node in curated_prerequisites})
        atomic_source_queries: dict[str, set[str]] = {}
        atomic_source_rank: dict[str, int] = {}
        build_error = not self.atomic.available
        # Retrieve from the original source before considering a model label.
        # Reuse the existing bounded subject/grade search and its review overlay.
        queries = tuple(dict.fromkeys(query[:240] for query in (*inferred, title, question, '；'.join(raw_hints)) if query))
        for query in queries[:5] if not build_error else ():
            try:
                result = self.atomic.context_for_child(child_id=child_id, query=query, subject=subject, limit=6)
            except ValueError:
                continue
            except (AtomicGraphUnavailableError, AtomicGraphChildNotFoundError, OSError, sqlite3.Error):
                build_error = True
                break
            relations.extend(result['relations'])
            for position, node in enumerate(result['nodes']):
                if node['review_status'] in {'rejected', 'restricted'}:
                    continue
                nodes[node['key']] = node
                if node['graph_role'] == 'matched':
                    eligible.add(node['key'])
                    if query in {title, question}:
                        atomic_source_queries.setdefault(node['key'], set()).add(query)
                        atomic_source_rank[node['key']] = min(position, atomic_source_rank.get(node['key'], position))
        match_inputs = _chunks(title), _chunks(question), tuple(map(_normalize, inferred)), tuple(map(_normalize, raw_hints))
        def candidate_score(key: str) -> float:
            score = _score(nodes[key], *match_inputs)
            if not _incomplete_title(str(nodes[key]['title'])) and any(_source_overlap_supports_candidate(query, nodes[key]['title'])
                   for query in atomic_source_queries.get(key, ())):
                score = max(score, .68)
            return score
        ranked = sorted(((candidate_score(key), key) for key in eligible),
                        key=lambda row: (-row[0], atomic_source_rank.get(row[1], 1_000_000), row[1]))
        ranked = [(score, key) for score, key in ranked if score >= .65]
        current_version = self.cache_version()
        if current_version != version:
            return empty | {'status': 'unavailable', 'graph_version': current_version}
        if not ranked:
            return empty | {'status': 'unavailable' if build_error else 'no_match'}
        if _verified_key is not None:
            verified = next(((score, key) for score, key in ranked[:3] if key == _verified_key), None)
            if verified is None:
                return empty | {'status': 'no_match'}
            ranked = [verified] + [item for item in ranked if item[1] != _verified_key]
        top_score, top_key = ranked[0]
        # Multiple versions of the same teaching objective are safe to merge
        # only if they have the same full normalized title.
        top_title = _normalize(nodes[top_key]['title'])
        competitors = [(score, key) for score, key in ranked[1:] if _normalize(nodes[key]['title']) != top_title]
        leading_terms = _terms(nodes[top_key]['title'], nodes[top_key].get('aliases', ()))
        close_competitors = [score for score, key in competitors if not any(
            len(term) + 2 <= len(leading) and term in leading and score < top_score
            for term in _terms(nodes[key]['title'], ()) for leading in leading_terms
        )]
        if _verified_key is None and (top_score < .83 or (close_competitors and top_score - close_competitors[0] < .045)):
            return empty | {'status': 'ambiguous', 'alternatives': [_view(nodes[key], score) for score, key in ranked[:3]],
                            'candidates_truncated': len(ranked) > 3}
        prerequisites = {}
        for relation in sorted(relations, key=lambda row: (row['review_status'] != 'approved', -row['confidence'], row['source_key'])):
            key = relation['source_key']
            if (key == top_key or key not in nodes or relation['target_key'] != top_key or relation['type'] != 'prerequisite_of'
                    or relation['review_status'] in {'rejected', 'restricted'}):
                continue
            node = nodes[key]
            prerequisites.setdefault(key, {'key': key, 'title': node['title'], 'review_status': node['review_status'],
                                            'relation_review_status': relation['review_status']})
        return empty | {'status': 'matched', 'primary': _view(nodes[top_key], top_score),
                        'prerequisites': list(prerequisites.values())[:4],
                        'alternatives': [_view(nodes[key], score) for score, key in competitors[:3]]}
