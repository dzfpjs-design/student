"""Textbook-neutral atomic curriculum catalogue anchored to local 2022 standards.

This is deliberately a *catalogue*, not an assertion that every atom is already
covered by a video.  Mappings are mined automatically from local evidence; their
status remains ``unreviewed`` to describe provenance, not to block use.
"""
from __future__ import annotations

from collections import defaultdict

from ..database import Database
from ..repository import utc_now
from .service import KnowledgeGraphService


# (subject, grade, track, standard source key, domain title, evidence term, atoms)
# Atom labels are textbook-neutral. They are not copied from a commercial text.
CATALOGUES = (
    ("数学", "四年级", "school", "moe-2022-mathematics", "数与运算", "数与代数", (
        "亿以内数的读写", "十进制计数法与数位顺序", "大数比较", "大数改写成万或亿作单位的数", "近似数与四舍五入", "自然数与整数", "加法交换律", "加法结合律", "乘法交换律", "乘法结合律", "乘法分配律", "三位数乘两位数", "积的变化规律", "除数是两位数的除法", "商的变化规律", "有余数除法检验", "估算乘除结果", "四则混合运算顺序", "含括号的四则混合运算", "运算律简便计算", "数量关系：单价数量总价", "数量关系：速度时间路程", "数量关系：工作效率工作时间工作总量", "用表格整理数量关系",
    )),
    ("数学", "四年级", "school", "moe-2022-mathematics", "图形与度量", "图形与几何", (
        "线段直线射线", "平行线", "垂线", "角的认识", "角的度量", "角的分类", "三角形的认识", "三角形按角分类", "三角形三边关系", "四边形的认识", "平行四边形", "梯形", "图形的平移", "图形的旋转", "长方形面积", "正方形面积", "面积单位换算", "组合图形面积", "长度单位换算", "质量单位换算",
    )),
    ("数学", "四年级", "school", "moe-2022-mathematics", "数据与随机", "数据分析", (
        "条形统计图", "复式条形统计图", "平均数", "数据收集与整理", "从统计图读取信息", "用数据解释结论",
    )),
    ("英语", "四年级", "school", "moe-2022-english", "日常语言能力", "语言能力", (
        "课堂指令理解", "日常问候与回应", "人物信息询问", "时间日期表达", "地点与方位表达", "日常活动表达", "喜欢与偏好表达", "天气与穿着表达", "食物与购物表达", "简单邀请与建议", "感谢道歉与请求", "简单对话轮次", "字母与常见拼读规则", "高频词块理解", "简单句基本语序", "现在时基本表达", "读图获取显性信息", "短文本主旨理解",
    )),
    ("语文", "四年级", "school", "moe-2022-chinese", "语言文字积累与梳理", "语言文字积累与梳理", (
        "常用汉字识读", "常用汉字书写", "词语理解与积累", "近义词反义词辨析", "句子成分基本认识", "病句基本修改", "标点符号使用", "段落中心句", "按顺序观察与表达", "写人记事基本结构", "说明一个事物", "朗读与默读策略",
    )),
    ("数学", "初三", "school", "moe-2022-mathematics", "数与式", "数与代数", (
        "实数与数轴", "平方根与立方根", "科学记数法", "代数式化简", "整式乘法", "因式分解提公因式", "因式分解公式法", "分式化简", "分式方程", "二次根式性质", "二次根式化简", "二次根式运算", "一元一次方程", "一元一次不等式", "一元一次不等式组", "二元一次方程组", "一次函数图像", "一次函数性质", "反比例函数图像", "反比例函数性质",
    )),
    ("数学", "初三", "school", "moe-2022-mathematics", "二次函数与方程", "数与代数", (
        "一元二次方程定义", "一元二次方程直接开平方法", "一元二次方程配方法", "一元二次方程公式法", "一元二次方程因式分解法", "根的判别式", "一元二次方程根与系数关系", "二次函数表达式", "二次函数图像", "二次函数开口与对称轴", "二次函数顶点", "二次函数最值", "二次函数与一元二次方程关系", "二次函数实际问题建模",
    )),
    ("数学", "初三", "school", "moe-2022-mathematics", "图形与证明", "图形与几何", (
        "平行线性质", "三角形全等判定", "三角形相似判定", "相似比与面积比", "锐角三角函数", "勾股定理", "圆的基本元素", "圆周角", "圆心角", "切线性质", "正多边形与圆", "图形轴对称", "图形中心对称", "位似变换", "坐标系中的图形变换", "几何证明步骤", "尺规作图基本操作",
    )),
    ("数学", "初三", "school", "moe-2022-mathematics", "统计与概率", "数据分析", (
        "总体样本与抽样", "平均数中位数众数", "方差与数据波动", "频数分布表", "频数分布直方图", "用样本估计总体", "概率的意义", "列举法求概率", "树状图求概率", "用频率估计概率",
    )),
    ("英语", "初三", "school", "moe-2022-english", "初中英语语言能力", "语言能力", (
        "时态：一般现在时", "时态：一般过去时", "时态：一般将来时", "时态：现在完成时", "被动语态", "情态动词", "宾语从句", "定语从句基本理解", "非谓语动词基础", "词义猜测", "篇章主旨", "细节定位", "推断作者意图", "书面表达任务点", "书面表达衔接", "口语交际策略",
    )),
    ("语文", "初三", "school", "moe-2022-chinese", "阅读表达与思辨", "实用性阅读与交流", (
        "文言实词", "文言虚词", "文言句式翻译", "古诗文意象", "现代文信息筛选", "现代文概括主旨", "人物形象分析", "表达方式辨析", "修辞手法作用", "论点论据论证", "说明方法作用", "材料作文审题", "记叙文结构", "议论文结构", "应用文格式", "修改与润色",
    )),
    ("物理", "初三", "school", "moe-2022-physics", "物质运动与相互作用", "运动和相互作用", (
        "长度时间测量", "速度计算", "运动状态判断", "力的作用效果", "重力", "摩擦力", "压强", "液体压强", "浮力", "杠杆平衡", "功与功率", "机械效率", "机械能", "声音的产生与传播", "光的反射", "光的折射", "透镜成像", "物态变化",
    )),
    ("物理", "初三", "school", "moe-2022-physics", "电与能量", "能量", (
        "电荷与电路", "电流电压电阻", "欧姆定律", "串联电路", "并联电路", "电功", "电功率", "焦耳定律", "家庭电路安全", "磁场", "电磁感应", "能量转化与守恒",
    )),
    ("化学", "初三", "school", "moe-2022-chemistry", "物质组成与结构", "物质的组成与结构", (
        "化学实验基本操作", "物质的变化与性质", "空气组成", "氧气制取与性质", "水的组成", "水的净化", "分子原子离子", "元素与元素符号", "相对原子质量", "化合价", "化学式书写", "化学方程式配平", "质量守恒定律", "溶液组成", "溶解度", "酸碱盐基础",
    )),
    ("化学", "初三", "school", "moe-2022-chemistry", "化学变化与社会", "物质的化学变化", (
        "燃烧条件", "灭火原理", "碳和碳的氧化物", "金属活动性顺序", "金属锈蚀与防护", "常见酸的性质", "常见碱的性质", "复分解反应条件", "化肥基础", "化学与环境", "实验探究变量控制", "实验现象记录", "实验结论与证据",
    )),
)


def _slug(text: str) -> str:
    # Canonical keys need a stable representation without relying on an external
    # transliteration library, which keeps Windows offline installs simple.
    return "-".join(f"{ord(char):x}" for char in text if char.strip())


class OfficialCurriculumCatalogProjector:
    """Project the minimum usable atom catalogue without manufacturing coverage."""
    version_label = "official-catalog-v1"

    def __init__(self, database: Database):
        self.database = database
        self.graph = KnowledgeGraphService(database)

    def run(self, *, rebuild_local_candidates: bool = True) -> dict:
        """Project standard structure, optionally rebuilding local title candidates.

        ``False`` is used at desktop startup: standard nodes remain available for
        host diagnostic attribution, while corpus scans stay in explicit
        ingestion/maintenance jobs with their own checkpoints and budgets.
        """
        with self.graph.catalog_batch() as graph:
            result, atom_scopes = self._project_nodes(graph)
        exact_title_candidates = self._map_exact_title_candidates(atom_scopes) if rebuild_local_candidates else {
            "source": "official_catalog_exact_local_evidence_v2", "upserted": 0,
            "covered_atomic_nodes": 0, "skipped": "startup_does_not_rescan_local_corpus",
        }
        return {**result, "exact_title_candidates": exact_title_candidates}

    def _project_nodes(self, graph):
        source_cache: dict[tuple[str, str], dict | None] = {}
        groups: dict[tuple[str, str, str, str, str], list[str]] = defaultdict(list)
        node_ids: dict[str, str] = {}
        atom_scopes: list[tuple[str, str, str, str, str]] = []
        aligned = unresolved = 0
        for subject, grade, track, source_key, domain, term, atoms in CATALOGUES:
            evidence = source_cache.setdefault((source_key, term), self._find_evidence(source_key, term))
            source_ref, source_time, confidence = self._source_fields(source_key, evidence)
            group_key = f"catalog-domain:{grade}:{subject}:{_slug(domain)}"
            group_id = graph.upsert_node(
                canonical_key=group_key, node_type="unit", title=domain,
                description=f"{grade}{subject}的课标内容域；资源映射由本机课标与转录证据自动生成。",
                scope={"grade":grade, "subject":subject, "track":track}, source_ref=source_ref,
                source_time=source_time, version_label=self.version_label, license_boundary="official_standard",
                confidence=confidence, review_status="unreviewed",
            )
            for atom in atoms:
                canonical_key = f"catalog-atom:{grade}:{subject}:{_slug(atom)}"
                node_id = graph.upsert_node(
                    canonical_key=canonical_key, node_type="knowledge_point", title=atom,
                    description=f"{grade}{subject}·{domain}中的原子学习目标；目录已锚定本地课标，资源映射采用本机自动证据。",
                    scope={"grade":grade, "subject":subject, "track":track, "domain":domain}, source_ref=source_ref,
                    source_time=source_time, version_label=self.version_label, license_boundary="official_standard",
                    confidence=confidence, review_status="unreviewed",
                )
                node_ids[canonical_key] = node_id
                atom_scopes.append((node_id, atom, subject, grade, track))
                graph.link(group_id, node_id, "contains", rationale=f"课标内容域“{domain}”下的原子目录项",
                                source_ref=source_ref, version_label=self.version_label, confidence=confidence, review_status="unreviewed")
                groups[(grade, subject, track, source_key, domain)].append(node_id)
                if evidence:
                    capability = graph.upsert_node(
                        canonical_key=f"catalog-evidence:{source_key}:{evidence['page_number']}:{_slug(domain)}", node_type="standard_capability",
                        title=f"{subject}课标证据：{domain}", description=evidence["content_text"][:900],
                        scope={"grade":grade, "subject":subject, "track":track}, source_ref=source_ref, source_time=source_time,
                        version_label=str(evidence["version_label"]), license_boundary="official_standard", confidence=.9, review_status="unreviewed",
                    )
                    graph.link(capability, node_id, "aligned_to", rationale=f"本地课标第{evidence['page_number']}页命中“{term}”，自动目录分解保留该来源。",
                                    source_ref=source_ref, version_label=self.version_label, confidence=.72, review_status="unreviewed")
                    aligned += 1
                else:
                    unresolved += 1
        return ({"catalogue_version":self.version_label, "atomic_nodes":len(node_ids), "aligned_atoms":aligned,
                 "unresolved_standard_evidence":unresolved, "domains":len(groups)}, atom_scopes)

    def _map_exact_title_candidates(self, atoms: list[tuple[str, str, str, str, str]]) -> dict:
        """Create only replayable candidates backed by exact local evidence.

        This deliberately does not use vector similarity: semantic proximity is
        useful for search but insufficient evidence for an instructional mapping.
        """
        now = utc_now(); inserted = 0; mapped_atoms: set[str] = set()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""DELETE FROM kg_resource_node WHERE review_status='unreviewed'
                AND source_ref IN ('official_catalog_exact_title_v1','official_catalog_exact_local_evidence_v2')""")
            for node_id, title, subject, grade, track in atoms:
                if len(title) < 3:
                    continue
                grade_condition = "(COALESCE(r.grade_scope,'')='' OR r.grade_scope=?)" if track == 'school' else "1=1"
                parameters: tuple = (title, title, subject, grade) if track == 'school' else (title, title, subject)
                matches = connection.execute(f"""SELECT s.resource_id,s.segment_id FROM kg_segment s
                    JOIN kg_resource r ON r.resource_id=s.resource_id
                    WHERE (instr(s.text,?)>0 OR instr(r.title,?)>0)
                    AND (COALESCE(r.subject_scope,'')='' OR r.subject_scope=?)
                    AND {grade_condition} ORDER BY s.segment_id LIMIT 80""", parameters).fetchall()
                for match in matches:
                    connection.execute("""INSERT INTO kg_resource_node(resource_id,segment_id,node_id,relation_type,confidence,review_status,source_ref,created_at)
                        VALUES(?,?,?,'covers',.68,'unreviewed','official_catalog_exact_local_evidence_v2',?)
                        ON CONFLICT(resource_id,segment_id,node_id,relation_type) DO UPDATE SET
                        confidence=excluded.confidence,source_ref=excluded.source_ref,created_at=excluded.created_at
                        WHERE kg_resource_node.review_status='unreviewed'""", (match['resource_id'],match['segment_id'],node_id,now))
                    inserted += connection.execute("SELECT changes()").fetchone()[0]
                    mapped_atoms.add(node_id)
            connection.commit()
        return {"source":"official_catalog_exact_local_evidence_v2", "upserted":inserted, "covered_atomic_nodes":len(mapped_atoms)}

    def _find_evidence(self, source_key: str, term: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute("""SELECT s.source_id,s.version_label,p.page_number,p.content_text
                FROM curriculum_standard_source s JOIN curriculum_standard_page p ON p.source_id=s.source_id
                WHERE s.source_key=? AND instr(p.content_text, ?) > 0 ORDER BY p.page_number LIMIT 1""", (source_key, term)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _source_fields(source_key: str, evidence: dict | None) -> tuple[str, str | None, float]:
        if evidence:
            return (f"curriculum_standard:{evidence['source_id']}:page:{evidence['page_number']}", str(evidence["version_label"]), .72)
        # Explicitly unresolved: never pretend a catalogue atom has a page citation.
        return (f"curriculum_standard_unresolved:{source_key}", None, .45)
