from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from .database import Database
from .repository import StudyRepository, PlanInputError, StaleSessionStateError, UnknownChildError, UnknownSessionError, utc_now
from .reports import build_child_learning_summary
from .learning_outcome_projection import LearningOutcomeProjection
from .learning_obligations import (
    MASTERY_REPLAY_VERSION, project_item_exposure, project_mastery_evidence,
    project_child_mastery_evidence, _empty_mastery_projection,
    qualified_exact_score, withdraw_excluded_reviews, withdrawn_evidence_ids,
)
from .specialized_catalog import SPECIALIZED_ITEMS, SPECIALIZED_NODES
from .school_oral_catalog import items as authored_oral_items


NODES = (
    ("node-p4-open-math-reasoning", "school", "数学", "四年级", "P4.MATH.OPEN.REASONING", "数量关系与说理", "自编开放题范围：结合本题条件解释数量关系并检查过程；不作为教材对齐或掌握证据。"),
    ("node-p4-open-chinese-reading", "school", "语文", "四年级", "P4.CHINESE.OPEN.READING", "阅读理解与原文依据", "自编开放题范围：准确提取原文信息并解释自己的理解；不由关键词判断掌握。"),
    ("node-p4-open-chinese-writing", "school", "语文", "四年级", "P4.CHINESE.OPEN.WRITING", "习作内容与连贯表达", "自编开放题范围：围绕目的组织具体细节并清楚表达；不由字数判断写作水平。"),
    ("node-p4-open-english-reading", "school", "英语", "四年级", "P4.ENGLISH.OPEN.READING", "短文理解与依据", "自编校内开放题范围：联系短文信息说明理解；不是PET成绩或完整阅读能力认证。"),
    ("node-p4-math-arithmetic", "school", "数学", "四年级", "P4.MATH.ARITHMETIC", "整数四则运算顺序", "自编诊断范围：按括号和先乘除后加减完成整数混合运算；不作为教材逐页对齐证据。"),
    ("node-p4-math-fractions-decimals", "school", "数学", "四年级", "P4.MATH.FRACTIONS_DECIMALS", "十分之一、百分之一与小数", "自编诊断范围：分母为10、100的分数与小数的等值表示及同分母加法；不据此推定完整分数知识已掌握。"),
    ("node-p4-english-phonics-discrimination", "school", "英语", "四年级", "P4.ENGLISH.PHONICS.DISCRIMINATION", "字母与字母组合读音辨析", "比较单词中划线字母或字母组合的实际读音，识别读音不同的一项并说明依据。"),
    ("node-j3-math-quadratic-formula", "school", "数学", "初三", "J3.MATH.QUADRATIC.FORMULA", "一元二次方程求根公式", "理解公式条件并准确代入。"),
    ("node-j3-math-discriminant", "school", "数学", "初三", "J3.MATH.QUADRATIC.DISCRIMINANT", "根的判别式", "用判别式判断实数根的数量。"),
    ("node-j3-chinese-reading-evidence", "school", "语文", "初三", "J3.CHINESE.READING.EVIDENCE", "现代文信息提取", "从文本中定位依据并用自己的话概括。"),
    ("node-j3-english-reading-purpose", "school", "英语", "初三", "J3.ENGLISH.READING.PURPOSE", "阅读目的与关键信息", "识别文本目的并提取支撑信息。"),
    ("node-j3-physics-experiment-reasoning", "school", "物理", "初三", "J3.PHYSICS.EXPERIMENT.REASONING", "物理实验变量与结论", "区分控制变量、观测量并由现象得出有限结论。"),
    ("node-j3-chem-equation", "school", "化学", "初三", "J3.CHEM.EQUATION", "化学方程式书写", "根据反应事实写出并配平化学方程式。"),
    ("node-j3-civics-material", "school", "道德与法治", "初三", "J3.CIVICS.MATERIAL", "材料观点与依据", "从材料提取行为事实，关联概念并形成有依据的建议。"),
    ("node-j3-history-causality", "school", "历史", "初三", "J3.HISTORY.CAUSALITY", "史事因果与影响", "区分背景、直接原因、事件与影响。"),
    ("node-pet-reading-main-idea", "pet", "英语", "四年级-PET", "PET.READING.MAIN_IDEA", "阅读主旨", "从短文中识别主要目的和核心信息。"),
    ("node-pet-writing-email", "pet", "英语", "四年级-PET", "PET.WRITING.EMAIL", "短邮件组织", "围绕任务点组织简短、连贯的英文邮件。"),
    ("node-pet-listening-detail", "pet", "英语", "四年级-PET", "PET.LISTENING.DETAIL", "听力细节", "从口语材料中提取人物、时间和行动信息。"),
    ("node-pet-speaking-picture", "pet", "英语", "四年级-PET", "PET.SPEAKING.PICTURE", "看图描述", "用完整句描述画面并进行简单推测。"),
    ("node-pet-speaking-read-aloud", "pet", "英语", "四年级-PET", "PET.SPEAKING.READ_ALOUD", "朗读参考", "朗读短文并用本地转写检查文本覆盖；不据此判断发音或流利度。"),
    ("node-oly-pattern", "competition_math", "数学", "四年级竞赛", "OLY.P4.PATTERN", "数列规律与归纳", "观察变化，提出并验证规律。"),
    ("node-oly-counting", "competition_math", "数学", "四年级竞赛", "OLY.P4.COUNTING", "有序计数", "用分类或列表避免重复与遗漏。"),
    ("node-oly-area", "competition_math", "数学", "四年级竞赛", "OLY.P4.AREA", "面积分割与重组", "通过分割、平移和补形求面积。"),
    ("node-oly-number-sense", "competition_math", "数学", "四年级竞赛", "OLY.P4.NUMBER_SENSE", "数感与估算", "理解数的大小、位值和数量级，并用估算检查结果。"),
    ("node-oly-arithmetic-structure", "competition_math", "数学", "四年级竞赛", "OLY.P4.ARITHMETIC_STRUCTURE", "运算结构", "利用交换律、结合律和分配律简化计算。"),
    ("node-oly-number-theory", "competition_math", "数学", "四年级竞赛", "OLY.P4.NUMBER_THEORY", "数论启蒙", "用整除、因数和余数分析整数问题。"),
    ("node-oly-logic", "competition_math", "数学", "四年级竞赛", "OLY.P4.LOGIC", "逻辑推理", "根据条件排除矛盾并形成完整推理链。"),
    ("node-oly-modeling", "competition_math", "数学", "四年级竞赛", "OLY.P4.MODELING", "应用建模", "把生活情境转化为数量关系、图表或算式。"),
    ("node-oly-expression", "competition_math", "数学", "四年级竞赛", "OLY.P4.EXPRESSION", "数学表达", "用算式、图示和完整语言说明思考过程。"),
)

GRAPH_NODES = (
    ("node-p4-english-error-spelling-pronunciation", "common_error", "school", "英语", "四年级", "P4.ENGLISH.ERROR.SPELLING_PRONUNCIATION", "只看拼写猜读音", "常见错因候选：只比较单词拼写，没有逐词读出并比较划线部分的实际发音。"),
    ("node-p4-english-strategy-phonics-compare", "learning_strategy", "school", "英语", "四年级", "P4.ENGLISH.STRATEGY.PHONICS_COMPARE", "逐词读音对比", "圈出划线部分，逐个读词并把相同读音归组，再找出不同项。"),
    ("node-p4-english-assessment-phonics", "assessment_task", "school", "英语", "四年级", "P4.ENGLISH.ASSESS.PHONICS", "读音辨析迁移检查", "更换单词和字母组合，检查能否独立比较实际发音而不是只看拼写。"),
    ("node-j3-chapter-quadratic", "textbook_chapter", "school", "数学", "初三", "J3.MATH.CHAPTER.QUADRATIC", "一元二次方程单元", "家庭版课程锚点，不绑定未确认的具体教材页码。"),
    ("node-j3-error-discriminant-sign", "common_error", "school", "数学", "初三", "J3.MATH.ERROR.DISCRIMINANT_SIGN", "判别式符号误判", "常见错因候选：漏平方、漏括号或符号计算错误。"),
    ("node-j3-strategy-substitute-check", "learning_strategy", "school", "数学", "初三", "J3.MATH.STRATEGY.SUBSTITUTE", "代入检查", "把求得的根代回原方程检查等式。"),
    ("node-j3-assessment-quadratic", "assessment_task", "school", "数学", "初三", "J3.MATH.ASSESS.QUADRATIC", "一元二次方程复测", "使用变式题和延迟复测验证稳定掌握。"),
    ("node-pet-vocabulary-context", "capability_component", "pet", "英语", "四年级-PET", "PET.COMPONENT.VOCABULARY", "语境词汇", "在短消息和日常对话中理解、调用常用词块。"),
    ("node-pet-grammar-sentence", "capability_component", "pet", "英语", "四年级-PET", "PET.COMPONENT.GRAMMAR", "句子控制", "用基础时态、连接词和句型表达清楚意思。"),
    ("node-pet-function-communication", "capability_component", "pet", "英语", "四年级-PET", "PET.COMPONENT.FUNCTION", "交际功能", "完成邀请、感谢、建议、询问信息等真实沟通目的。"),
    ("node-pet-text-short-message", "text_type", "pet", "英语", "四年级-PET", "PET.TEXT.SHORT_MESSAGE", "短消息与通知", "理解或创作邮件、便条和通知。"),
    ("node-pet-assessment-four-skills", "assessment_task", "pet", "英语", "四年级-PET", "PET.ASSESS.FOUR_SKILLS", "PET四项参考评估", "家庭版听说读写参考任务，不等同于官方评分。"),
    ("node-oly-error-unordered-counting", "common_error", "competition_math", "数学", "四年级竞赛", "OLY.P4.ERROR.UNORDERED", "无序枚举", "计数时因缺少分类顺序而重复或遗漏。"),
    ("node-oly-strategy-representation", "learning_strategy", "competition_math", "数学", "四年级竞赛", "OLY.P4.STRATEGY.REPRESENT", "画图、列表与表格", "用合适表示方法暴露结构并检查遗漏。"),
    ("node-oly-assessment-transfer", "assessment_task", "competition_math", "数学", "四年级竞赛", "OLY.P4.ASSESS.TRANSFER", "数学思维迁移题", "改变表面情境，检查方法能否迁移。"),
)

EDGES = (
    ("edge-p4-phonics-error", "node-p4-english-error-spelling-pronunciation", "node-p4-english-phonics-discrimination", "misconception_of", "只看字母外形会忽略同一字母在不同单词中的实际发音差异。"),
    ("edge-p4-phonics-remedy", "node-p4-english-error-spelling-pronunciation", "node-p4-english-strategy-phonics-compare", "remediated_by", "逐词朗读、归组和对比用于纠正只看拼写猜读音。"),
    ("edge-p4-phonics-assess", "node-p4-english-phonics-discrimination", "node-p4-english-assessment-phonics", "assessed_by", "用更换单词后的辨音题验证方法能否迁移。"),
    ("edge-p4-phonics-strategy", "node-p4-english-strategy-phonics-compare", "node-p4-english-phonics-discrimination", "transfers_to", "逐词读音对比可以稳定迁移到不同字母和字母组合的辨音任务。"),
    ("edge-j3-formula-chapter", "node-j3-math-quadratic-formula", "node-j3-chapter-quadratic", "taught_in", "求根公式属于一元二次方程单元。"),
    ("edge-j3-discriminant-chapter", "node-j3-math-discriminant", "node-j3-chapter-quadratic", "taught_in", "根的判别式属于一元二次方程单元。"),
    ("edge-j3-discriminant-assess", "node-j3-math-discriminant", "node-j3-assessment-quadratic", "assessed_by", "用不求根判断根的情况来评估判别式。"),
    ("edge-j3-error-misconception", "node-j3-error-discriminant-sign", "node-j3-math-discriminant", "misconception_of", "符号与平方错误会导致判别式结论错误。"),
    ("edge-j3-error-remedy", "node-j3-error-discriminant-sign", "node-j3-strategy-substitute-check", "remediated_by", "分步括号和代入检查用于暴露符号错误。"),
    ("edge-j3-discriminant-formula", "node-j3-math-discriminant", "node-j3-math-quadratic-formula", "prerequisite_of", "先理解判别式，才能解释求根公式在实数范围内的结果。"),
    ("edge-j3-check-transfer", "node-j3-strategy-substitute-check", "node-j3-math-quadratic-formula", "transfers_to", "代入检查可迁移到求根公式结果验证。"),
    ("edge-pet-vocab-reading", "node-pet-vocabulary-context", "node-pet-reading-main-idea", "part_of", "语境词汇支撑短文本主旨理解。"),
    ("edge-pet-grammar-writing", "node-pet-grammar-sentence", "node-pet-writing-email", "part_of", "句子控制是短邮件表达的一部分。"),
    ("edge-pet-function-writing", "node-pet-function-communication", "node-pet-writing-email", "part_of", "交际目的决定邮件任务点。"),
    ("edge-pet-text-reading", "node-pet-text-short-message", "node-pet-reading-main-idea", "part_of", "短消息与通知是阅读训练文本类型。"),
    ("edge-pet-function-text", "node-pet-function-communication", "node-pet-text-short-message", "taught_in", "在短消息、便条和邮件中练习真实交际功能。"),
    ("edge-pet-reading-assess", "node-pet-reading-main-idea", "node-pet-assessment-four-skills", "assessed_by", "四项参考任务保存阅读证据。"),
    ("edge-pet-writing-assess", "node-pet-writing-email", "node-pet-assessment-four-skills", "assessed_by", "四项参考任务保存写作量表依据。"),
    ("edge-pet-listening-assess", "node-pet-listening-detail", "node-pet-assessment-four-skills", "assessed_by", "四项参考任务保存听力客观证据。"),
    ("edge-pet-speaking-assess", "node-pet-speaking-picture", "node-pet-assessment-four-skills", "assessed_by", "四项参考任务保存口语内容依据。"),
    ("edge-pet-read-aloud-assess", "node-pet-speaking-read-aloud", "node-pet-assessment-four-skills", "assessed_by", "朗读任务只保存本地转写覆盖依据，不代替发音评分。"),
    ("edge-oly-number-arithmetic", "node-oly-number-sense", "node-oly-arithmetic-structure", "prerequisite_of", "稳定数感帮助判断巧算结果是否合理。"),
    ("edge-oly-arithmetic-theory", "node-oly-arithmetic-structure", "node-oly-number-theory", "prerequisite_of", "整除与因数问题需要可靠的运算结构。"),
    ("edge-oly-expression-model", "node-oly-expression", "node-oly-modeling", "part_of", "建模结果需要清楚的算式与语言表达。"),
    ("edge-oly-pattern-model", "node-oly-pattern", "node-oly-modeling", "transfers_to", "归纳规律可迁移到变化关系建模。"),
    ("edge-oly-area-represent", "node-oly-area", "node-oly-strategy-representation", "remediated_by", "分割图、补形图能降低面积题的工作记忆负担。"),
    ("edge-oly-error-counting", "node-oly-error-unordered-counting", "node-oly-counting", "misconception_of", "无序枚举会造成重复或遗漏。"),
    ("edge-oly-error-represent", "node-oly-error-unordered-counting", "node-oly-strategy-representation", "remediated_by", "列表和表格提供稳定的枚举顺序。"),
    ("edge-oly-counting-assess", "node-oly-counting", "node-oly-assessment-transfer", "assessed_by", "迁移题检查分类计数方法能否脱离原题使用。"),
    ("edge-oly-logic-assess", "node-oly-logic", "node-oly-assessment-transfer", "assessed_by", "迁移题检查条件推理链。"),
)

ITEMS = (
    ("diag-p4-arithmetic-closed-1", "node-p4-math-arithmetic", "计算 (24+6)×3，只回答一个整数。", "text", {"keywords": ["90"], "local_rule": "local_exact_v1", "exact_answers": ["90"]}, "先算括号内24+6=30，再算30×3=90。自编闭答案诊断。", 1),
    ("diag-p4-arithmetic-closed-2", "node-p4-math-arithmetic", "计算 24+6×3，只回答一个整数。", "text", {"keywords": ["42"], "local_rule": "local_exact_v1", "exact_answers": ["42"]}, "先算6×3=18，再算24+18=42。自编闭答案诊断。", 1),
    ("diag-p4-fraction-decimal-closed-1", "node-p4-math-fractions-decimals", "把3/10写成小数，只回答一个数。", "text", {"keywords": ["0.3"], "local_rule": "local_exact_v1", "exact_answers": ["0.3"]}, "3个十分之一写成0.3。自编闭答案诊断。", 1),
    ("diag-p4-fraction-decimal-closed-2", "node-p4-math-fractions-decimals", "计算2/10+5/10，把结果写成小数。", "text", {"keywords": ["0.7"], "local_rule": "local_exact_v1", "exact_answers": ["0.7"]}, "同分母相加得到7/10，写成小数是0.7。自编闭答案诊断。", 1),
    ("diag-p4-english-phonics-1", "node-p4-english-phonics-discrimination", "比较 cake、bag、name 中字母 a 的读音，哪一个不同？请说出判断依据。", "text", {"keywords": ["bag", "不同", "cake", "name"]}, "cake 和 name 中的字母 a 读音相同，bag 中的 a 读音不同，所以选择 bag。", 1),
    ("diag-p4-english-phonics-2", "node-p4-english-phonics-discrimination", "比较 bike、sit、five 中字母 i 的读音，哪一个不同？请说出判断依据。", "text", {"keywords": ["sit", "不同", "bike", "five"]}, "bike 和 five 中的字母 i 读音相同，sit 中的 i 读音不同，所以选择 sit。", 2),
    ("diag-p4-english-phonics-3", "node-p4-english-phonics-discrimination", "比较 home、hot、nose 中字母 o 的读音，哪一个不同？这次先按拼写分组，再逐词读音核对。", "text", {"keywords": ["hot", "不同", "home", "nose"]}, "home 和 nose 中的字母 o 读音相同，hot 中的 o 读音不同，所以选择 hot；最终判断必须来自实际读音对比。", 2),
    ("diag-j3-discriminant-1", "node-j3-math-discriminant", "方程 x²-4x+4=0 的判别式是多少？它有几个不同的实数根？", "text", {"keywords": ["0", "一个", "1个", "两个相等"]}, "判别式为16-16=0，因此有两个相等的实数根，也可说有一个不同的实数根。", 1),
    ("diag-j3-discriminant-2", "node-j3-math-discriminant", "不求解方程 2x²+x+3=0，判断它在实数范围内有没有解，并说明依据。", "text", {"keywords": ["-23", "小于0", "没有实数", "无实数"]}, "判别式1-24=-23，小于0，所以没有实数根。", 2),
    ("diag-j3-chinese-reading-1", "node-j3-chinese-reading-evidence", "阅读句子：‘雨停后，父亲没有立刻收伞，而是把伞向我这边又倾了倾。’这个细节主要表现了什么？请引用动作作为依据。", "text", {"keywords": ["关爱", "照顾", "倾", "父亲"]}, "‘把伞向我这边又倾了倾’这一动作表现父亲对孩子细致、无声的关爱。", 1),
    ("diag-j3-chinese-reading-2", "node-j3-chinese-reading-evidence", "阅读句子：‘奶奶把刚端上桌的鱼轻轻转了半圈，把鱼腹对着我。’这个动作表现了什么？请用句中细节作证。", "text", {"keywords": ["关爱", "照顾", "鱼腹", "转"]}, "‘把鱼腹对着我’的动作表现奶奶不声张地照顾孩子。", 2),
    ("diag-j3-english-reading-1", "node-j3-english-reading-purpose", "Read: 'The science club meeting has moved to Room 304 because the lab is being repaired. Please bring your project notes.' What is the main purpose of the notice?", "text", {"keywords": ["meeting", "Room 304", "moved", "bring"]}, "The notice tells members about the new meeting room and reminds them what to bring.", 1),
    ("diag-j3-english-reading-2", "node-j3-english-reading-purpose", "Read: 'Library users: the second floor will close at 6 pm today for maintenance. Please return borrowed books at the desk downstairs.' What is the notice asking readers to know or do?", "text", {"keywords": ["second floor", "close", "6 pm", "return", "downstairs"]}, "It announces the early closure of the second floor and tells users where to return books.", 2),
    ("diag-j3-physics-experiment-1", "node-j3-physics-experiment-reasoning", "探究滑动摩擦力与压力大小的关系时，为什么要让同一木块在同一水平面上运动？", "text", {"keywords": ["控制", "接触面", "粗糙", "变量"]}, "要控制接触面的粗糙程度等条件相同，只改变压力大小，才能判断压力对摩擦力的影响。", 1),
    ("diag-j3-physics-experiment-2", "node-j3-physics-experiment-reasoning", "探究导体电阻与长度的关系时，为什么两根电阻丝应保持材料和横截面积相同？", "text", {"keywords": ["控制变量", "材料", "横截面积", "长度"]}, "要用控制变量法只改变长度，所以材料和横截面积必须相同。", 2),
    ("diag-j3-chem-equation-1", "node-j3-chem-equation", "写出铁在氧气中燃烧生成四氧化三铁的化学方程式，并说明配平后的系数比。", "text", {"keywords": ["3Fe", "2O2", "Fe3O4", "3:2:1"]}, "3Fe+2O₂点燃生成Fe₃O₄，系数比为3:2:1。", 1),
    ("diag-j3-chem-equation-2", "node-j3-chem-equation", "写出铝和氧气反应生成氧化铝的化学方程式，并说明配平后的系数比。", "text", {"keywords": ["4Al", "3O2", "2Al2O3", "4:3:2"]}, "4Al+3O₂生成2Al₂O₃，系数比为4:3:2。", 2),
    ("diag-j3-civics-material-1", "node-j3-civics-material", "同学未经允许把他人的照片做成表情包发到班群。请指出涉及的权利，并给出一条有依据的处理建议。", "text", {"keywords": ["肖像", "隐私", "删除", "道歉"]}, "可能涉及肖像权或隐私权；应停止传播、删除内容并向当事人道歉，必要时请老师和家长协助。", 1),
    ("diag-j3-civics-material-2", "node-j3-civics-material", "同学偷看别人的日记后把内容告诉多名同学。请指出可能被侵犯的权利，并给出两个合理的处理步骤。", "text", {"keywords": ["隐私", "停止传播", "道歉", "老师"]}, "这可能侵犯隐私权；应立即停止传播并道歉，必要时请老师或家长协助。", 2),
    ("diag-j3-history-causality-1", "node-j3-history-causality", "分析历史事件时，‘长期社会矛盾’和‘某次直接冲突’分别更接近背景原因还是直接原因？", "text", {"keywords": ["背景", "长期", "直接", "冲突"]}, "长期社会矛盾通常属于背景或根本原因，某次冲突通常属于直接原因或导火线。", 1),
    ("diag-j3-history-causality-2", "node-j3-history-causality", "一项改革前长期存在财政危机，后来因一次会议冲突而爆发。请区分财政危机和会议冲突的原因层次。", "text", {"keywords": ["长期", "背景", "根本", "直接", "导火线"]}, "长期财政危机是背景或根本原因，会议冲突是直接原因或导火线。", 2),
    ("diag-pet-reading-1", "node-pet-reading-main-idea", "Read: 'Hi Sam, I left your blue notebook at the school office. You can collect it before 5 pm. Mia.' Why did Mia write this message?", "text", {"keywords": ["notebook", "office", "collect"]}, "The message tells Sam where the notebook is and when to collect it.", 1),
    ("diag-pet-reading-2", "node-pet-reading-main-idea", "Read: 'The swimming lesson will start at 4:30 instead of 4:00 today. Please wait by the small pool.' What is the main purpose of this notice?", "text", {"keywords": ["time", "4:30", "change", "pool"]}, "It tells learners about a changed start time and where to wait.", 1),
    ("diag-oly-pattern-1", "node-oly-pattern", "观察 2, 5, 10, 17, 26。下一个数是多少？请说明规律。", "text", {"keywords": ["37", "加3", "加5", "加7", "加9", "奇数"]}, "相邻两项依次加3、5、7、9，所以下一次加11，得到37。", 1),
    ("diag-oly-pattern-2", "node-oly-pattern", "一排图形所用小棒数是4、7、10、13……第8个图形需要多少根？请说明你怎样从规律得到。", "text", {"keywords": ["25", "每次多3", "加3", "4+7×3"]}, "每增加一个图形多3根，第8个需要4+7×3=25根。", 2),
    ("diag-oly-counting-1", "node-oly-counting", "用数字1、2、3组成没有重复数字的两位数，一共有多少个？请有序列出。", "text", {"keywords": ["6", "12", "13", "21", "23", "31", "32"]}, "十位有3种选择，个位剩2种，共6个：12、13、21、23、31、32。", 1),
    ("diag-oly-counting-2", "node-oly-counting", "小明从A到B可选2条路，从B到C可选3条路。必须经过B，从A到C共有几种不同走法？请解释为什么。", "text", {"keywords": ["6", "2×3", "2*3", "每条", "搭配"]}, "A到B的每种走法都能搭配3种B到C走法，所以2×3=6种。", 1),
    ("diag-oly-pattern-3", "node-oly-pattern", "把1、4、9、16继续写两项，并说明这些数与图形有什么关系。", "text", {"keywords": ["25", "36", "平方", "正方形"]}, "它们是1²、2²、3²、4²，后两项是25和36，可看作边长递增的正方形点阵。", 2),
    ("diag-oly-counting-3", "node-oly-counting", "书架上有3本不同的故事书和2本不同的科普书，各选一本共有多少种选法？请说明怎样保证不遗漏。", "text", {"keywords": ["6", "3×2", "3*2", "列表", "搭配"]}, "每本故事书都可搭配2本科普书，共3×2=6种；可用表格逐行列出。", 1),
    ("diag-oly-area-1", "node-oly-area", "一个10厘米×8厘米的长方形右上角剪去一个4厘米×3厘米的小长方形，剩余面积是多少？", "text", {"keywords": ["68", "80", "12", "减"]}, "先算整体80平方厘米，再减去12平方厘米，剩68平方厘米。", 1),
    ("diag-oly-area-2", "node-oly-area", "一个长方形面积是48平方厘米，长是8厘米。把它平均分成两个相同小长方形，每个小长方形面积是多少？说明是否需要先求宽。", "text", {"keywords": ["24", "不需要", "一半"]}, "平均分成两个相同部分，每部分面积就是48÷2=24平方厘米，不必先求宽。", 2),
    ("diag-oly-number-sense-1", "node-oly-number-sense", "从1写到100，数字9一共出现多少次？请分个位和十位说明。", "text", {"keywords": ["20", "个位", "十位", "10"]}, "个位出现10次，十位出现10次，共20次；99中的两个9分别计数。", 2),
    ("diag-oly-number-sense-2", "node-oly-number-sense", "不精确计算，估计398×51更接近2000、20000还是200000，并说明理由。", "text", {"keywords": ["20000", "400", "50"]}, "把398看作400、51看作50，400×50=20000。", 1),
    ("diag-oly-arithmetic-1", "node-oly-arithmetic-structure", "不用竖式，计算125×32，并写出你的巧算过程。", "text", {"keywords": ["4000", "125×8", "125*8", "32=8×4", "乘4"]}, "把32拆成8×4，125×8×4=1000×4=4000。", 2),
    ("diag-oly-arithmetic-2", "node-oly-arithmetic-structure", "用简便方法计算99×37，并说明用了什么结构。", "text", {"keywords": ["3663", "100×37", "3700", "减37", "分配律"]}, "99×37=(100-1)×37=3700-37=3663，使用分配律。", 2),
    ("diag-oly-number-theory-1", "node-oly-number-theory", "36有多少个正因数？请成对列出。", "text", {"keywords": ["9", "1和36", "2和18", "3和12", "4和9", "6和6"]}, "因数对为1和36、2和18、3和12、4和9、6和6，共9个不同正因数。", 2),
    ("diag-oly-number-theory-2", "node-oly-number-theory", "一个三位数4□2能被3整除，方框里可以填哪些数字？说明依据。", "text", {"keywords": ["0", "3", "6", "9", "各位数字和", "被3整除"]}, "各位数字和6+□应是3的倍数，所以可填0、3、6、9。", 2),
    ("diag-oly-logic-1", "node-oly-logic", "甲、乙、丙比赛跑步。甲不是第一，乙在丙前面，丙不是第二。三人的名次分别是什么？", "text", {"keywords": ["乙第一", "甲第二", "丙第三"]}, "乙必须在丙前；丙不是第二只能第三，乙第一，甲第二。", 2),
    ("diag-oly-logic-2", "node-oly-logic", "三个盒子分别标着苹果、梨、混合，但三个标签全错。只从一个盒子拿一个水果，应该先从哪个标签的盒子拿？为什么？", "text", {"keywords": ["混合", "标签全错", "实际不是混合"]}, "先从标着“混合”的盒子拿，因为它实际一定不是混合；拿到的水果可确定该盒内容，再推出另外两个。", 3),
    ("diag-oly-modeling-1", "node-oly-modeling", "停车场有自行车和三轮车共10辆，共有24个轮子。两种车各多少辆？请用一种表示方法说明。", "text", {"keywords": ["6辆自行车", "4辆三轮车", "自行车6", "三轮车4", "假设"]}, "若全是自行车有20个轮子，多出的4个轮子说明有4辆三轮车，所以自行车6辆。", 2),
    ("diag-oly-modeling-2", "node-oly-modeling", "成人票每张20元，儿童票每张12元。2名成人和3名儿童一共多少钱？先写数量关系再计算。", "text", {"keywords": ["76", "2×20", "3×12", "相加"]}, "总价=成人票价×人数+儿童票价×人数=2×20+3×12=76元。", 1),
    ("diag-oly-expression-1", "node-oly-expression", "计算(36+24)÷6×5，并用一句话说明运算顺序。", "text", {"keywords": ["50", "先算括号", "从左到右"]}, "先算括号得60，再按从左到右计算60÷6×5=50。", 1),
    ("diag-oly-expression-2", "node-oly-expression", "小华有40元，买3本每本8元的本子。请只列一个综合算式表示剩下的钱，并解释每一部分。", "text", {"keywords": ["40-3×8", "40-8×3", "16", "总钱数", "总价"]}, "综合算式40-3×8=16元；3×8是本子总价，再从40元中减去。", 1),
)

EXTENDED_ITEMS = (
    ("diag-pet-listening-1", "node-pet-listening-detail", "听两遍短消息：Where should Leo meet Anna?", "listening_text", {"keywords": ["library", "front", "门口"]}, "Leo should meet Anna in front of the library at half past three.", 1, "Hi Leo. The football practice is cancelled today. Please meet me in front of the library at half past three. Anna.", None, "objective"),
    ("diag-pet-listening-2", "node-pet-listening-detail", "听两遍通知：What must students bring?", "listening_text", {"keywords": ["raincoat", "雨衣"]}, "Students must bring a raincoat because the walk will continue if it rains.", 1, "Tomorrow's nature walk starts at nine. Bring a raincoat because we will still go if it rains. You do not need to bring lunch.", None, "objective"),
    ("diag-pet-writing-1", "node-pet-writing-email", "Write an email to your English friend Alex. Invite Alex to study with you this Saturday. Say where to meet, what time, and what you will study. Write about 35-55 words.", "writing", {"task_points": ["地点", "时间", "学习内容"], "keyword_groups": [["library", "home", "school", "cafe", "park"], ["am", "pm", "o'clock", "morning", "afternoon"], ["math", "english", "science", "homework", "study"]], "min_words": 35, "max_words": 55}, "参考量表看任务点覆盖、基本连贯和可理解语言，不等同于PET官方评分。", 1, None, None, "pet_reference_rubric"),
    ("diag-pet-writing-2", "node-pet-writing-email", "Write a short email to Mia. Thank her for a book, say what you liked about it, and tell her when you will return it. Write about 35-55 words.", "writing", {"task_points": ["感谢", "喜欢之处", "归还时间"], "keyword_groups": [["thank"], ["like", "liked", "interesting", "funny", "exciting"], ["return", "give back", "tomorrow", "monday", "week"]], "min_words": 35, "max_words": 55}, "参考量表看三个任务点、组织和可理解性，不等同于PET官方评分。", 1, None, None, "pet_reference_rubric"),
    ("diag-pet-speaking-1", "node-pet-speaking-picture", "Look at the study-room picture and describe what the people are doing. Then make one guess about what may happen next.", "speaking", {"task_points": ["人物", "动作", "推测"], "keyword_groups": [["boy", "girl", "child", "student", "people"], ["reading", "writing", "studying", "talking", "looking"], ["maybe", "might", "will", "probably", "next"]], "min_words": 20}, "参考量表仅根据本地转写评估任务完成和表达展开，不能据此判断发音。", 1, None, "pet-study-room", "pet_reference_rubric"),
    ("diag-pet-speaking-2", "node-pet-speaking-picture", "Talk about a place where you like to read. Say where it is, what you read there, and why you like it.", "speaking", {"task_points": ["地点", "阅读内容", "原因"], "keyword_groups": [["home", "room", "library", "school", "cafe", "park"], ["book", "story", "novel", "comic", "magazine"], ["because", "quiet", "comfortable", "interesting", "relax"]], "min_words": 20}, "参考量表仅根据本地转写评估内容覆盖和连贯性，不能据此判断发音。", 1, None, None, "pet_reference_rubric"),
    ("diag-pet-read-aloud-1", "node-pet-speaking-read-aloud", "请先默读下面的短文，再用自然速度完整朗读。系统只检查本地转写是否覆盖文本，不评判发音。", "read_aloud", {"task_points": ["开头信息", "中间行动", "结尾目的"], "keyword_groups": [["saturday", "morning", "ben"], ["market", "father", "apples", "bread"], ["picnic", "park"]], "min_words": 20}, "参考量表只检查转写中的段落覆盖，不能判断发音、重音、语调或流利度。", 1, "On Saturday morning, Ben walks to the market with his father. They buy apples and bread because the family is having a picnic in the park.", None, "pet_reference_rubric"),
    ("diag-pet-daily-dialogue-1", "node-pet-function-communication", "I ask: 'Would you like to study together this Saturday?' Reply in English. Say yes or no, suggest a time and place, say what to study, and ask me one question.", "speaking", {"task_points": ["回应邀请", "时间地点", "学习内容", "反问"], "keyword_groups": [["yes", "sure", "sorry", "can", "would"], ["saturday", "morning", "afternoon", "library", "home", "school"], ["study", "english", "math", "homework", "science"], ["what", "where", "when", "do you", "shall we"]], "min_words": 20}, "参考量表只根据本地转写检查交际任务点，不能判断发音或真实对话流利度。", 1, None, None, "pet_reference_rubric"),
    ("diag-pet-daily-story-create-1", "node-pet-grammar-sentence", "Continue this story in 35-55 words: 'Mia opened her school bag and found a tiny map inside. The map showed a red star near the old library...' Include an action, a small problem, and an ending.", "writing", {"task_points": ["行动", "问题", "结尾"], "keyword_groups": [["went", "walked", "looked", "opened", "found", "followed"], ["but", "problem", "lost", "couldn't", "could not", "wrong"], ["finally", "at last", "in the end", "home", "safe", "found"]], "min_words": 35, "max_words": 55}, "参考量表检查故事任务点、基本连贯和可理解语言，不等同于PET官方评分。", 1, None, None, "pet_reference_rubric"),
    ("diag-pet-daily-retell-1", "node-pet-reading-main-idea", "Read or listen to the short story, then retell it in English. Say who was there, what happened, and how it ended.", "speaking", {"task_points": ["人物", "经过", "结果"], "keyword_groups": [["lily", "girl"], ["dog", "library", "water", "owner", "called"], ["home", "safe", "finally", "owner came"]], "min_words": 20}, "参考量表只根据本地转写检查复述内容覆盖，不能判断发音。", 1, "Lily found a small dog near the library. The dog looked tired, so she gave it some water. She called the phone number on its collar. Soon the owner came, and the dog went home safely.", None, "pet_reference_rubric"),
)

# Authored references provide real source seeds, not reviewed learner scores.
EXTENDED_ITEMS += tuple(
    ('diag-p4-open-'+slug+'-1', 'node-p4-open-'+slug, prompt, 'open_response',
     {'task_points':points, 'review_profile':'school_p4_open_'+subject+'_'+family+'_v1',
      'profile_version':'school_primary_open_response_profiles_v1', 'task_family':family,
      'response_mode':'written', 'context_requirement':'self_contained'},
     '自编原题参考；原始作答仍须逐项可靠评分，不能据此自动认证掌握。', 1, None, None, 'open_response_rubric')
    for slug, subject, family, prompt, points in (
        ('math-reasoning','math','mathematical_process',
         '每盒彩笔12支，买3盒后平均分给6人。小林列式12×3÷6。解释每一步表示什么，并检查结果是否合理。',
         ['说明先求总数再平均分的依据','用原题条件检查结果']),
        ('chinese-reading','chinese','textual_interpretation',
         '读短文：小禾走到门口，看见雨水打湿了台阶。她返回屋里拿了伞，又把门边的小凳子搬进屋。小禾为什么返回屋里？请结合原文说明，不把猜测当作事实。',
         ['回答小禾返回屋里的原因','结合原文具体信息说明依据']),
        ('chinese-writing','chinese','communicative_writing',
         '给一位没参加班级活动的同学写一段话，介绍一次你熟悉的活动。写清发生了什么，选择一个具体细节，并说说你的感受。',
         ['写清活动和事情经过','选择具体细节并表达感受']),
        ('english-reading','english','textual_interpretation',
         "Read: Lily has a new book. She puts it in her bag and walks to Ben's home. Ben wants to read it too. Why does Lily take the book? Use information from the text to explain.",
         ['回答Lily带书的原因','使用短文信息支持解释']),
    )
)


EXTENDED_ITEMS += authored_oral_items()


_CHINESE_NUMBER = re.compile(r"负?[零〇一二两三四五六七八九十百千万]+")
_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _spoken_integer(value: str) -> int:
    negative = value.startswith("负")
    text = value[1:] if negative else value
    if not any(unit in text for unit in "十百千万"):
        parsed = int("".join(str(_CHINESE_DIGITS[character]) for character in text))
        return -parsed if negative else parsed
    total = section = number = 0
    for character in text:
        if character in _CHINESE_DIGITS:
            number = _CHINESE_DIGITS[character]
            continue
        unit = {"十": 10, "百": 100, "千": 1000, "万": 10000}[character]
        if unit == 10000:
            section = (section + number) * unit
            total += section
            section = number = 0
        else:
            section += (number or 1) * unit
            number = 0
    parsed = total + section + number
    return -parsed if negative else parsed


def normalize_spoken_math(value: str) -> str:
    normalized = value.lower().replace(" ", "")
    normalized = _CHINESE_NUMBER.sub(lambda match: str(_spoken_integer(match.group(0))), normalized)
    return (
        normalized.replace("小于零", "小于0")
        .replace("大于零", "大于0")
        .replace("等于零", "等于0")
        .replace("负号", "-")
    )


PET_BASELINE_DEFINITION = (
    ("reading", "阅读", "objective", (("reading_objective", ("diag-pet-reading-1", "diag-pet-reading-2")),)),
    ("listening", "听力", "objective", (("listening_objective", ("diag-pet-listening-1", "diag-pet-listening-2")),)),
    ("writing", "写作", "reference", (("short_writing", ("diag-pet-writing-1", "diag-pet-writing-2")),)),
    (
        "speaking",
        "口语",
        "reference",
        (
            ("spoken_response", ("diag-pet-speaking-1", "diag-pet-speaking-2")),
            ("read_aloud", ("diag-pet-read-aloud-1",)),
        ),
    ),
)


class LearningStateService:
    def __init__(self, database: Database):
        self.database = database

    def ensure_closure_schema(self) -> None:
        with self.database.connect() as connection:
            connection.executescript("""CREATE TABLE IF NOT EXISTS learning_session_closure (
                session_id TEXT PRIMARY KEY REFERENCES daily_session(session_id) ON DELETE CASCADE,
                child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
                learning_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('completed','failed')),
                source_fingerprint TEXT, summary_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, retry_after TEXT, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_learning_closure_child_date
                  ON learning_session_closure(child_id,learning_date);""")
            connection.commit()

    @staticmethod
    def _learning_date(value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("learning source timestamp requires a timezone")
        return parsed.astimezone(timezone(timedelta(hours=8))).date().isoformat()

    def close_session_learning(self, session_id: str) -> dict:
        """After session completion commit, outside the caller's write connection.

        Snapshot + next-day review receipts commit atomically. Failure does not
        undo session completion; maintenance retries its content-free checkpoint.
        """
        self.ensure_closure_schema()
        with self.database.connect() as connection:
            session = connection.execute("SELECT * FROM daily_session WHERE session_id=?", (session_id,)).fetchone()
        if not session:
            raise UnknownSessionError(session_id)
        if session["status"] != "completed" or not session["ended_at"]:
            raise ValueError("learning closure requires a completed session")
        day = self._learning_date(session["ended_at"])
        now = utc_now()
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                withdraw_excluded_reviews(connection, session["child_id"], now)
                LearningOutcomeProjection(self.database).reconcile_commitments(connection, session["child_id"], now)
                material = self._closure_material(connection, session_id)
                actions = self._closure_actions(connection, material, schedule=True)
                result = build_child_learning_summary(material, actions)
                fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                # Canonical source records own prose and erasure. Persist only
                # references here so forgetting a memory cannot leave a text copy.
                manifest = {"version": result["version"], "source_refs": [item["source"] for field in
                    ("learning_content", "homework", "weak_points", "needs_verification") for item in material[field]],
                    "remediation_refs": [{key: item[key] for key in ("source_type", "source_ref", "status")} for item in actions],
                    "local_only": True, "cloud_transmission_allowed": False}
                connection.execute("""INSERT INTO learning_session_closure
                    (session_id,child_id,learning_date,status,source_fingerprint,summary_json,attempts,retry_after,updated_at)
                    VALUES (?,?,?,'completed',?,?,1,?,?) ON CONFLICT(session_id) DO UPDATE SET
                    status='completed',source_fingerprint=excluded.source_fingerprint,summary_json=excluded.summary_json,
                    attempts=attempts+1,error_code=NULL,retry_after=excluded.retry_after,updated_at=excluded.updated_at""", (
                    session_id, material["child_id"], day, fingerprint, json.dumps(manifest, ensure_ascii=False),
                    (datetime.fromisoformat(now) + timedelta(hours=1)).isoformat(), now))
                connection.commit()
                return result
        except Exception:
            with self.database.connect() as connection:
                if connection.execute("SELECT 1 FROM daily_session WHERE session_id=?", (session_id,)).fetchone():
                    connection.execute("""INSERT INTO learning_session_closure
                        (session_id,child_id,learning_date,status,attempts,error_code,retry_after,updated_at)
                        VALUES (?,?,?,'failed',1,'CLOSURE_FAILED',?,?) ON CONFLICT(session_id) DO UPDATE SET
                        status='failed',attempts=attempts+1,error_code='CLOSURE_FAILED',retry_after=excluded.retry_after,
                        updated_at=excluded.updated_at""", (session_id, session["child_id"], day,
                        (datetime.fromisoformat(now) + timedelta(minutes=5)).isoformat(), now))
                    connection.commit()
            raise

    def process_pending_closures(self, limit: int = 50) -> dict:
        self.ensure_closure_schema()
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT s.session_id FROM daily_session s
                LEFT JOIN learning_session_closure c USING(session_id)
                WHERE s.status='completed' AND s.ended_at IS NOT NULL
                  AND (c.session_id IS NULL OR c.retry_after<=?)
                ORDER BY (c.session_id IS NOT NULL),c.retry_after,s.ended_at,s.session_id LIMIT ?""",
                (utc_now(), max(0, min(int(limit), 500)))).fetchall()
        result = {"processed": 0, "failed": 0, "failures": [], "cloud_transmission_allowed": False}
        for row in rows:
            try:
                self.close_session_learning(row["session_id"])
                result["processed"] += 1
            except Exception:
                result["failed"] += 1
                result["failures"].append({"session_id": row["session_id"], "error_code": "CLOSURE_FAILED"})
        return result

    def daily_learning_summary(self, child_id: str, learning_date: str | None = None) -> dict:
        """Read-only local view. Revalidate sources, never replay stale erased prose."""
        day = learning_date or self._learning_date(utc_now())
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("learning_date must be YYYY-MM-DD")
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            if not connection.execute("SELECT 1 FROM child_profile WHERE child_id=?", (child_id,)).fetchone():
                raise UnknownChildError(child_id)
            rows = connection.execute("""SELECT session_id,status,ended_at,started_at FROM daily_session
                WHERE child_id=? AND status IN ('active','completed') ORDER BY started_at,session_id""", (child_id,)).fetchall()
            summaries = []
            for row in rows:
                material = self._closure_material(connection, row["session_id"], evidence_date=day)
                if not any(material[field] for field in ("learning_content", "homework", "weak_points", "needs_verification")):
                    continue
                summaries.append(build_child_learning_summary(material, self._closure_actions(connection, material, schedule=False)))
        combined = {"child_id": child_id, "session_id": None, "learning_date": day,
                    "session_status": "active" if any(item["session_status"] == "active" for item in summaries) else "completed"}
        for field in ("learning_content", "homework", "weak_points", "needs_verification"):
            unique = {}
            for summary in summaries:
                for item in summary[field]:
                    ref = item["source"]
                    unique[(ref["kind"], ref["source_id"], item.get("field"))] = item
            combined[field] = list(unique.values())
        actions = {}
        for summary in summaries:
            for item in summary["remediation"]:
                actions[(item["source_type"], item["source_ref"] or item.get("source_evidence_id"))] = item
        result = build_child_learning_summary(combined, list(actions.values()))
        result.update(scope="learning_day", sessions=summaries, status="ready" if summaries else "empty")
        if not summaries:
            result["child_response"] = f"北京时间{day}暂无学习会话记录，不以其他日期的记录代替。"
        # The document camera can record pages during an active session, before
        # the child completes a reflection. Keep these as local, unverified OCR
        # observations, separate from confirmed learning or mastery.
        from .daily_visual_evidence import project_daily_visual_evidence
        try:
            visual = project_daily_visual_evidence(self.database, child_id, day)
        except sqlite3.OperationalError:
            # Older databases may not have the optional monitor tables yet.
            # Preserve the existing summary instead of claiming no observation.
            result['document_observation'] = {'status': 'unavailable', 'reason': 'DOCUMENT_EVIDENCE_UNAVAILABLE'}
        except ValueError as error:
            if str(error) != 'VISUAL_EVIDENCE_DELETION_PENDING':
                raise
            result['document_observation'] = {'status': 'unavailable', 'reason': 'DELETION_PENDING'}
        else:
            result['document_observation'] = {'status': 'available', **visual}
            if visual['pages']:
                if not summaries:
                    result['child_response'] = f'北京时间{day}暂无其他学习记录。'
                first = visual['pages'][0]
                excerpt = ' '.join(first['text'].split())[:60]
                result['child_response'] += f'高拍仪识别到页面内容：{excerpt}。识别可能有误，需核对；看到页面不代表已经掌握。'
                result['status'] = 'ready'
        from .daily_page_observation import daily_page_observation
        try:
            result['page_observation_time'] = daily_page_observation(self.database, child_id, day)
        except ValueError as error:
            if str(error) != 'PAGE_OBSERVATION_DELETION_PENDING':
                raise
            result['page_observation_time'] = {'status':'unavailable', 'reason':'DELETION_PENDING'}
        return result

    def session_learning_summary(self, session_id: str) -> dict:
        """No credentials, schema creation, writes or mastery mutation."""
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            material = self._closure_material(connection, session_id)
            return build_child_learning_summary(material, self._closure_actions(connection, material, schedule=False))

    def preview_session_learning(self, session_id: str) -> dict:
        """Alias for the pre-close study/reflection integration hook."""
        return self.session_learning_summary(session_id)

    def _closure_material(self, connection, session_id: str, *, evidence_date: str | None = None) -> dict:
        owned_transaction = not connection.in_transaction
        if owned_transaction:
            connection.execute("BEGIN")
        try:
            return self._closure_material_in_transaction(connection, session_id, evidence_date=evidence_date)
        finally:
            if owned_transaction:
                connection.rollback()

    def _closure_material_in_transaction(self, connection, session_id: str, *, evidence_date: str | None = None) -> dict:
        session = connection.execute("SELECT * FROM daily_session WHERE session_id=? AND status IN ('active','completed')", (session_id,)).fetchone()
        if not session:
            raise ValueError("completed learning source unavailable")
        child = session["child_id"]
        material = {"session_id": session_id, "child_id": child, "session_status": session["status"],
                    "learning_date": evidence_date or self._learning_date(session["ended_at"] or utc_now()),
                    "learning_content": [], "homework": [], "weak_points": [], "needs_verification": [], "tutor_followups": []}

        def record(kind, source_id, title, occurred, **extra):
            return {"title": str(title or "")[:300], "source": {"kind": kind, "source_id": source_id,
                    "session_id": session_id, "occurred_at": occurred}, "mastery_update_allowed": False, **extra}

        for row in connection.execute("""SELECT * FROM classroom_recall WHERE session_id=?
            AND confirmation_status='child_confirmed' ORDER BY created_at,recall_id""", (session_id,)):
            material["learning_content"].append(record("classroom_recall", row["recall_id"], row["summary"], row["created_at"],
                subject=row["subject"], evidence_status="child_report_not_verified_learning"))
        for row in connection.execute("""SELECT * FROM homework_item WHERE session_id=? AND child_id=?
            AND confirmation_status='child_confirmed' ORDER BY created_at,homework_id""", (session_id, child)):
            tasks = connection.execute("""SELECT pi.plan_item_id,pi.status,pi.completion_evidence_type,pi.actual_minutes,pi.started_at,pi.completed_at
                FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE pi.homework_id=? AND dp.child_id=? AND dp.session_id=?
                  AND pi.source_type='school_homework' AND pi.status!='cancelled'
                  AND pi.plan_item_id NOT LIKE 'daily-correction:%' ORDER BY pi.position""", (row["homework_id"], child, session_id)).fetchall()
            source_at = row["created_at"]
            if evidence_date:
                recorded = [row["created_at"], *[task[key] for task in tasks for key in ("started_at", "completed_at") if task[key]]]
                event_times = connection.execute("""SELECT e.occurred_at FROM plan_item_event e JOIN plan_item pi USING(plan_item_id)
                    JOIN daily_plan dp USING(plan_id) WHERE pi.homework_id=? AND dp.child_id=? AND dp.session_id=?
                    AND e.event_type IN ('started','paused','resumed','completed')""", (row["homework_id"], child, session_id)).fetchall()
                recorded.extend(event[0] for event in event_times)
                on_day = [stamp for stamp in recorded if self._learning_date(stamp) == evidence_date]
                if not on_day:
                    continue
                source_at = max(on_day, key=datetime.fromisoformat)
            from .learning_input_replan import completed_homework_source
            done = completed_homework_source(connection, child, session_id, row['homework_id']) and bool(tasks) and all(task["status"] == "completed" and (not evidence_date or
                task["completed_at"] and self._learning_date(task["completed_at"]) <= evidence_date) for task in tasks)
            correction_tasks = [task["plan_item_id"] for task in tasks if task["completion_evidence_type"] == "needs_correction"
                and (not evidence_date or task["completed_at"] and self._learning_date(task["completed_at"]) <= evidence_date)]
            correction = bool(correction_tasks)
            material["homework"].append(record("homework_item", row["homework_id"], row["title"], source_at,
                subject=row["subject"], range_text=(row["range_text"] or "")[:160], status="needs_correction" if correction else "completed" if done else "unfinished",
                status_label="孩子标记仍需订正" if correction else "孩子确认完成，不代表掌握" if done else "尚无全部完成记录",
                plan_item_ids=[task["plan_item_id"] for task in tasks], correction_plan_item_ids=correction_tasks, actual_minutes=sum(task["actual_minutes"] or 0 for task in tasks
                    if not evidence_date or task["completed_at"] and self._learning_date(task["completed_at"]) == evidence_date)))
        reflection = connection.execute("SELECT * FROM daily_reflection WHERE session_id=? AND child_id=? AND skipped=0", (session_id, child)).fetchone()
        if reflection:
            for field in ("learned_text", "difficult_text", "next_action"):
                if reflection[field]:
                    material["needs_verification"].append(record("daily_reflection", reflection["reflection_id"], reflection[field], reflection["created_at"],
                        field=field, evidence_status="child_self_report"))
        evidence = connection.execute("""SELECT le.*,da.diagnostic_item_id,kn.title,kn.subject FROM learning_evidence le
            LEFT JOIN diagnostic_attempt da ON da.evidence_id=le.evidence_id AND da.child_id=le.child_id AND da.session_id=le.session_id
            JOIN knowledge_node kn ON kn.knowledge_node_id=le.knowledge_node_id
            WHERE le.child_id=? AND le.session_id=? AND le.evidence_type='diagnostic_answer'
            ORDER BY julianday(le.occurred_at),le.rowid""", (child, session_id)).fetchall()
        cutoff = (datetime.combine(date.fromisoformat(evidence_date) + timedelta(days=1), time.min,
            timezone(timedelta(hours=8))).astimezone(timezone.utc) if evidence_date else None)
        batch = project_child_mastery_evidence(connection, child, as_of=utc_now())
        projections = {node: batch[node] for node in {row["knowledge_node_id"] for row in evidence} if node in batch}
        accepted = {item["evidence_id"]: item for projection in projections.values()
            for item in projection["effective_evidence"]}
        rejected = {item["evidence_id"]: item["reason"] for projection in projections.values()
            for item in projection["excluded_evidence"]}
        latest_error_in_session = {}
        for row in evidence:
            if evidence_date and self._learning_date(row["occurred_at"]) != evidence_date:
                continue
            effective = accepted.get(row["evidence_id"])
            reason = rejected.get(row["evidence_id"], "receipt_unavailable")
            content = record("learning_evidence", row["evidence_id"], row["title"], row["occurred_at"],
                subject=row["subject"], correctness=effective["correctness"] if effective else None,
                reference_correctness=row["correctness"], independent=bool(effective and effective["independent"] and not effective["hint_level"]),
                evidence_status="answer_check_not_mastery" if effective else "withdrawn" if reason == "withdrawn" else "score_needs_verification",
                score_status="verified_local" if effective else "withdrawn" if reason == "withdrawn" else "needs_review")
            if not effective:
                content.update(exclusion_reason=reason, status_label="作答已保存，评分待核验，不作为正确能力证据。")
                if reason != "withdrawn":
                    material["needs_verification"].append({**content,
                        "next_action": "当前评分不能可靠确认对错，需要先核实题目与作答，再决定是否安排复测。"})
            elif effective["correctness"] < 1:
                latest_error_in_session[row["knowledge_node_id"]] = row
            material["learning_content"].append(content)
        for node_id, row in latest_error_in_session.items():
            # Supported success and rejected later responses cannot erase a reliable error.
            decisive = [item for item in projections[node_id]["effective_evidence"]
                if (not cutoff or datetime.fromisoformat(item["occurred_at"]) < cutoff)
                and (item["correctness"] < 1 or item["independent"] and not item["hint_level"])]
            latest = decisive[-1] if decisive else None
            if latest and latest["correctness"] >= 1:
                continue
            material["weak_points"].append(record("learning_evidence", row["evidence_id"], row["title"], row["occurred_at"],
                knowledge_node_id=node_id, diagnostic_item_id=row["diagnostic_item_id"], subject=row["subject"],
                current_status="needs_recheck", current_status_source_id=latest["evidence_id"] if latest else None,
                evidence_status="answer_needs_recheck_not_confirmed_cause"))
        for row in connection.execute("""SELECT DISTINCT mc.*,COALESCE(le.occurred_at,mc.created_at) AS source_occurred_at
            FROM mistake_case mc LEFT JOIN learning_evidence le ON le.evidence_id=mc.source_evidence_id
            WHERE mc.child_id=? AND mc.confirmed_at IS NOT NULL AND mc.status NOT IN ('excluded','mastered','captured','awaiting_confirmation')
              AND (le.session_id=? AND le.child_id=mc.child_id OR EXISTS (
                SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.child_id=mc.child_id AND dp.session_id=? AND pi.source_type='mistake_review' AND pi.source_ref=mc.mistake_id))
            ORDER BY mc.updated_at,mc.mistake_id""", (child, session_id, session_id)):
            material["weak_points"].append(record("mistake_case", row["mistake_id"], row["title"], row["source_occurred_at"],
                knowledge_node_id=row["knowledge_node_id"], subject=row["subject"], evidence_status=row["status"]))
        for row in connection.execute("""SELECT turn_id,question,created_at FROM tutor_turn WHERE child_id=? AND session_id=?
            AND status='completed' ORDER BY created_at,turn_id""", (child, session_id)):
            material["needs_verification"].append(record("tutor_turn", row["turn_id"], row["question"], row["created_at"], evidence_status="explanation_not_mastery"))
        outcomes = LearningOutcomeProjection(self.database).for_session(child, session_id, connection=connection)
        available = {}
        for item in outcomes:
            if item["evidence_status"] != "available":
                continue
            available[item["objective_id"]] = item
            material["learning_content"].append(record("agent_outcome", item["outcome_id"],
                (item["mapping"] or {}).get("title") or "Tutor作答与检查", item["created_at"],
                evidence_status="model_rubric_not_verified_mastery", outcome_level=item["outcome_level"], confidence=item["confidence"]))
        objectives = connection.execute("""SELECT DISTINCT ao.* FROM agent_objective ao
            JOIN agent_plan p ON p.objective_id=ao.objective_id AND p.child_id=ao.child_id
            JOIN agent_step s USING(plan_id)
            WHERE ao.child_id=? AND p.session_id=? AND ao.objective_type='learning'
              AND ao.status IN ('active','paused') AND s.capability_name LIKE 'learning.%'
            ORDER BY ao.created_at,ao.objective_id""", (child, session_id)).fetchall()
        for objective in objectives:
            latest = connection.execute("""SELECT s.step_id,s.capability_name,s.arguments_json,s.result_json,s.updated_at FROM agent_step s JOIN agent_plan p USING(plan_id)
                WHERE p.objective_id=? AND p.child_id=? AND p.session_id=? AND s.capability_name LIKE 'learning.%'
                  AND s.status IN ('completed','waiting_child') ORDER BY s.updated_at DESC,s.rowid DESC LIMIT 1""",
                (objective["objective_id"], child, session_id)).fetchone()
            if not latest:
                continue
            try:
                arguments, result = json.loads(latest["arguments_json"] or "{}"), json.loads(latest["result_json"] or "{}")
                if isinstance(result.get("result"), dict):
                    result = result["result"]
                if isinstance(result.get("evaluation"), dict):
                    result = result["evaluation"]
                outcome_id = result.get("outcome_id")
                if latest["capability_name"] == "learning.evaluate_response" and not outcome_id:
                    continue
                if outcome_id and not LearningOutcomeProjection(self.database).available_outcome(connection, child, outcome_id, objective["objective_id"]):
                    continue
                prompt = str(result.get("next_check") or arguments.get("question_asked") or "请不用照着讲解，用自己的话说出关键步骤，再独立检查一次。")[:800]
                subject = str(arguments.get("subject") or "复习")[:40]
            except (ValueError, TypeError, AttributeError):
                continue
            outcome = available.get(objective["objective_id"])
            followup = record("agent_objective", objective["objective_id"], objective["summary"], latest["updated_at"],
                phase=objective["phase"], prompt=prompt, subject=subject,
                outcome_id=outcome_id, source_step_id=latest["step_id"], evidence_status="follow_up_not_confirmed_weakness")
            material["tutor_followups"].append(followup)
            if objective["phase"] in {"explain", "check", "remediate"}:
                material["needs_verification"].append(followup)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='companion_memory_document_source'").fetchone():
            for row in connection.execute("""SELECT DISTINCT m.memory_id,m.content,m.observation_json,s.occurred_at FROM companion_memory m
                JOIN companion_memory_evidence e USING(memory_id) JOIN companion_memory_source s USING(source_id)
                JOIN companion_memory_document_source d USING(source_id)
                WHERE m.child_id=? AND s.child_id=m.child_id AND d.child_id=m.child_id AND s.session_id=?
                  AND m.status='active' AND m.content IS NOT NULL AND m.expires_at>? AND s.tombstoned=0
                ORDER BY s.occurred_at,m.memory_id""", (child, session_id, utc_now())):
                citations = json.loads(row["observation_json"] or "[]")
                title = "；".join(item["quote"] for item in citations[:2]) if citations else (row["content"].partition("OCR文字片段：")[2] or "页面文字待核对")
                material["learning_content"].append(record("companion_memory", row["memory_id"], title, row["occurred_at"], evidence_status="document_observation_unconfirmed"))
        if evidence_date:
            for field in ("learning_content", "homework", "weak_points", "needs_verification", "tutor_followups"):
                material[field] = [item for item in material[field] if self._learning_date(item["source"]["occurred_at"]) == evidence_date]
        for item in material["needs_verification"]:
            item.setdefault("next_action", "下次先说出具体题目和卡住的一步，我们核对后再决定练习什么。")
        from .oral_supply_carryover import pending_for_session, unfinished_deliveries
        material['oral_unanswered_deliveries'] = [d for d in unfinished_deliveries(connection,child,session_id)
            if not evidence_date or self._learning_date(d['retained_at'])==evidence_date]
        material['oral_supply_requests'] = [root for root in pending_for_session(connection,child,session_id)
            if not evidence_date or self._learning_date(root.get('retained_at',root['created_at']))==evidence_date]
        for root in material['oral_supply_requests']:
            carried = root.get('carry_decision_id')
            material['needs_verification'].append(record('plan_constraint_decision' if carried else 'agent_commitment',
                carried or root['commitment_id'],'上次请求的口述新题',root.get('retained_at',root['created_at']),
                evidence_status='undelivered_question_not_weakness',
                next_action='先核对新题与可用时间，保留原回答，不重做旧题代替。'))
        return material

    def _closure_actions(self, connection, material: dict, *, schedule: bool) -> list[dict]:
        child, actions = material["child_id"], []
        oral_requests = material.get('oral_supply_requests',[])
        oral_goals = {root['objective_id'] for root in oral_requests}
        oral_actions = []
        for delivery in material.get('oral_unanswered_deliveries',[]):
            oral_goals.add(delivery['objective_id'])
            oral_actions.append(dict(source_type='oral_delivered_continuation',source_ref=delivery['commitment_id'],
                objective_id=delivery['objective_id'],title='上次还没回答的口述题',status=delivery['status'],
                guidance='题目已经交付但还没回答；保留原题，继续前重新核对时间与孩子意愿，不认定完成或掌握。',
                mastery_update_allowed=False,local_only=True,cloud_transmission_allowed=False))
        for root in oral_requests:
            oral_actions.append(dict(source_type='oral_question_supply',source_ref=root['commitment_id'],
                objective_id=root['objective_id'],title='上次请求的口述新题',status='needs_supply',
                guidance='补齐合格新题后再核对今天的安排与孩子意愿；原回答保留，不用重做旧题代替。',
                mastery_update_allowed=False,local_only=True,cloud_transmission_allowed=False))
        mistake_nodes = {item["knowledge_node_id"] for item in material["weak_points"] if item["source"]["kind"] == "mistake_case"}
        for item in material["weak_points"]:
            source = item["source"]
            node = item["knowledge_node_id"]
            if source["kind"] == "mistake_case":
                actions.append({"source_type": "mistake_review", "source_ref": source["source_id"], "title": item["title"],
                    "status": "pending", "guidance": "先复述题意并核对当前错题步骤，再独立做一次检查；讲解不算通过。",
                    "mastery_update_allowed": False})
                continue
            if node in mistake_nodes:
                continue
            review = connection.execute("""SELECT rt.*,di.prompt FROM review_task rt JOIN diagnostic_item di USING(diagnostic_item_id)
                WHERE rt.child_id=? AND rt.knowledge_node_id=? AND rt.interval_label='next_day'
                  AND rt.status!='cancelled' AND (rt.status='pending' OR rt.source_evidence_id=?)
                  AND NOT EXISTS (SELECT 1 FROM mistake_case mc WHERE mc.child_id=rt.child_id
                    AND mc.source_evidence_id=rt.source_evidence_id AND mc.status='excluded')
                ORDER BY rt.created_at DESC,rt.review_task_id LIMIT 1""",
                (child, node, source["source_id"])).fetchone()
            if schedule and not review and len(actions) < 3:
                self._schedule_next_day_review(connection, child, node, item["diagnostic_item_id"], source["source_id"], source["occurred_at"], material["learning_date"])
                review = connection.execute("SELECT rt.*,di.prompt FROM review_task rt JOIN diagnostic_item di USING(diagnostic_item_id) WHERE rt.source_evidence_id=? AND rt.interval_label='next_day'", (source["source_id"],)).fetchone()
            if (review and self._qualified_review_item(connection, child, review["diagnostic_item_id"])
                    and self._qualified_review_source(connection, child, node, review["source_evidence_id"], as_of=utc_now())):
                actions.append({"source_type": "due_review", "source_ref": review["review_task_id"],
                    "source_evidence_id": review["source_evidence_id"], "diagnostic_item_id": review["diagnostic_item_id"],
                    "knowledge_node_id": node, "title": item["title"], "due_at": review["due_at"], "status": review["status"],
                    "guidance": "先不看提示独立作答，再核对哪一步需要帮助；一次通过仍需后续间隔复测。",
                    "estimated_minutes": 10, "mastery_update_allowed": False})
            else:
                actions.append({"source_type": "due_review", "source_ref": review["review_task_id"] if review else None, "source_evidence_id": source["source_id"],
                    "knowledge_node_id": node, "title": item["title"], "status": "needs_supply" if review else "needs_scheduling",
                    "guidance": "需要补齐可用的复测题或下一次时间预算，不虚构已安排的练习。", "mastery_update_allowed": False})
        for item in material["tutor_followups"]:
            objective_id = item["source"]["source_id"]
            if objective_id in oral_goals:
                continue
            commitment = connection.execute("""SELECT * FROM agent_commitment WHERE child_id=? AND objective_id=?
                AND status IN ('pending','due') AND (commitment_type='review' OR
                  (commitment_type='follow_up' AND json_extract(payload_json,'$.origin')='daily_learning_closure'))
                ORDER BY due_at,commitment_id LIMIT 1""", (child, objective_id)).fetchone()
            if commitment and not LearningOutcomeProjection(self.database).commitment_available(connection, commitment):
                commitment = None
            if not commitment and schedule and len(actions) < 3 and item["phase"] in {"explain", "check", "remediate", "transfer_check"}:
                phase = "transfer_check" if item["phase"] == "transfer_check" else "check"
                due_day = date.fromisoformat(material["learning_date"]) + timedelta(days=1)
                due = datetime.combine(due_day, time.min, timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat()
                action_id = "daily-learning-followup:" + objective_id + ":" + (item["outcome_id"] or "initial")
                payload = {"origin": "daily_learning_closure", "phase": phase, "prompt": item["prompt"],
                           "subject": item["subject"], "summary": item["title"], "source_outcome_id": item["outcome_id"],
                           "source_step_id": item["source_step_id"]}
                connection.execute("""INSERT OR IGNORE INTO agent_commitment
                    (commitment_id,objective_id,child_id,commitment_type,due_at,status,payload_json,created_at,client_action_id)
                    VALUES (?,?,?,'follow_up',?,'pending',?,?,?)""",
                    (str(uuid4()), objective_id, child, due, json.dumps(payload, ensure_ascii=False), utc_now(), action_id))
                commitment = connection.execute("SELECT * FROM agent_commitment WHERE client_action_id=?", (action_id,)).fetchone()
            actions.append({"source_type": "agent_retention", "source_ref": commitment["commitment_id"] if commitment else None,
                "objective_id": objective_id, "title": item["title"], "due_at": commitment["due_at"] if commitment else None,
                "status": commitment["status"] if commitment else "needs_scheduling",
                "guidance": "继续上次的学习目标，先用自己的话解释，再独立回答检查问题；需要帮助就先补关键步骤。",
                "estimated_minutes": 8, "mastery_update_allowed": False})
        actions.extend(self._closure_corrections(connection, material, schedule=schedule))
        return actions + oral_actions

    def _closure_corrections(self, connection, material: dict, *, schedule: bool) -> list[dict]:
        actions = []
        for homework in material["homework"]:
            for original_id in homework.get("correction_plan_item_ids", []):
                source = connection.execute("""SELECT pi.* FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                    WHERE pi.plan_item_id=? AND dp.child_id=? AND dp.session_id=?
                      AND pi.status='completed' AND pi.completion_evidence_type='needs_correction'""",
                    (original_id, material["child_id"], material["session_id"])).fetchone()
                if not source:
                    continue
                obligation_id = "daily-correction:" + original_id
                receipt = connection.execute("SELECT plan_item_id FROM plan_item WHERE plan_item_id=?", (obligation_id,)).fetchone()
                if schedule and not receipt:
                    position = connection.execute("SELECT COALESCE(MAX(position),0)+1 FROM plan_item WHERE plan_id=?", (source["plan_id"],)).fetchone()[0]
                    connection.execute("""INSERT INTO plan_item
                        (plan_item_id,plan_id,homework_id,subject,title,expected_minutes,priority,status,position,
                         source_type,source_ref,completion_standard,cognitive_load,task_type)
                        VALUES (?,?,?,?,?,10,1,'rescheduled',?,'school_homework',?,?,'medium','correction')""",
                        (obligation_id, source["plan_id"], source["homework_id"], source["subject"], "订正 · " + source["title"],
                         position, source["homework_id"], "完成孩子标记的订正并说明修改步骤，不预设错因或宣称掌握"))
                    connection.execute("""INSERT INTO plan_item_event
                        (event_id,plan_item_id,event_type,occurred_at,payload_json,client_action_id)
                        VALUES (?,?,'rescheduled',?,?,?)""", (str(uuid4()), obligation_id, utc_now(),
                        json.dumps({"reason": "孩子完成时标记仍需订正，下次先确认承接", "source_plan_item_id": original_id}, ensure_ascii=False), obligation_id))
                    receipt = {"plan_item_id": obligation_id}
                review = connection.execute("SELECT decision,target_homework_id FROM deferred_task_review WHERE source_plan_item_id=?", (obligation_id,)).fetchone()
                status = "needs_confirmation" if receipt else "needs_scheduling"
                if review:
                    status = "completed" if review["decision"] == "completed_elsewhere" else "cancelled" if review["decision"] == "cancelled" else "pending"
                    if review["target_homework_id"]:
                        tasks = connection.execute("SELECT status,completion_evidence_type FROM plan_item WHERE homework_id=?", (review["target_homework_id"],)).fetchall()
                        if tasks and all(task["status"] == "completed" and task["completion_evidence_type"] != "needs_correction" for task in tasks):
                            status = "completed"
                actions.append({"source_type": "school_homework", "source_ref": obligation_id if receipt else None,
                    "kind": "homework_correction", "source_plan_item_id": original_id, "title": source["title"],
                    "status": status, "estimated_minutes": 10, "mastery_update_allowed": False,
                    "guidance": "订正已保留为延期任务，下次先确认继续，随后纳入当天时间预算。" if receipt else "结束后保留订正任务，下次先确认继续。"})
        return actions

    @staticmethod
    def _schedule_next_day_review(connection, child_id, node_id, original_item_id, evidence_id, now, learning_date=None):
        if not LearningStateService._qualified_review_source(connection, child_id, node_id, evidence_id, as_of=now):
            return
        pending = connection.execute("""SELECT diagnostic_item_id,source_evidence_id FROM review_task
            WHERE child_id=? AND knowledge_node_id=? AND interval_label='next_day' AND status='pending'""", (child_id, node_id))
        for review in pending:
            if (LearningStateService._qualified_review_item(connection, child_id, review["diagnostic_item_id"])
                    and LearningStateService._qualified_review_source(connection, child_id, node_id, review["source_evidence_id"], as_of=now)):
                return
        alternative = LearningStateService._select_review_item(connection, child_id, node_id, original_item_id)
        if not alternative:
            return
        day = date.fromisoformat(learning_date or LearningStateService._learning_date(now)) + timedelta(days=1)
        due = datetime.combine(day, time.min, timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat()
        connection.execute("""INSERT OR IGNORE INTO review_task
            (review_task_id,child_id,knowledge_node_id,diagnostic_item_id,source_evidence_id,interval_label,due_at,status,created_at)
            VALUES (?,?,?,?,?,'next_day',?,'pending',?)""", (str(uuid4()), child_id, node_id, alternative, evidence_id, due, now))

    def seed_catalog(self) -> None:
        with self.database.connect() as connection:
            for node in (*NODES, *SPECIALIZED_NODES):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_node(
                      knowledge_node_id, node_type, track, subject, grade_band, code, title, description,
                      source_ref, knowledge_version, status
                    ) VALUES (?, 'skill', ?, ?, ?, ?, ?, ?, 'hsc_original_v1', 'starter_v1', 'active')
                    """,
                    node,
                )
                connection.execute(
                    "UPDATE knowledge_node SET node_type = 'skill' WHERE knowledge_node_id = ?",
                    (node[0],),
                )
            for node in GRAPH_NODES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_node(
                      knowledge_node_id, node_type, track, subject, grade_band, code, title,
                      description, source_ref, knowledge_version, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'hsc_original_v1', 'starter_v2', 'active')
                    """,
                    node,
                )
            for edge in EDGES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_edge(
                      edge_id, source_node_id, target_node_id, relation_type, rationale,
                      source_ref, knowledge_version, status
                    ) VALUES (?, ?, ?, ?, ?, 'hsc_original_v1', 'starter_v2', 'active')
                    """,
                    edge,
                )
            for item_id, node_id, prompt, answer_type, expected, explanation, difficulty in (*ITEMS, *SPECIALIZED_ITEMS):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO diagnostic_item(
                      diagnostic_item_id, knowledge_node_id, prompt, answer_type,
                      expected_answer_json, explanation, difficulty, source_ref, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'hsc_original_v1', 'active')
                    """,
                    (item_id, node_id, prompt, answer_type, json.dumps(expected, ensure_ascii=False), explanation, difficulty),
                )
            for item_id, node_id, prompt, answer_type, expected, explanation, difficulty, stimulus_text, stimulus_asset, assessment_mode in EXTENDED_ITEMS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO diagnostic_item(
                      diagnostic_item_id, knowledge_node_id, prompt, answer_type,
                      expected_answer_json, explanation, stimulus_text, stimulus_asset,
                      assessment_mode, difficulty, source_ref, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hsc_original_v1', 'active')
                    """,
                    (item_id, node_id, prompt, answer_type, json.dumps(expected, ensure_ascii=False), explanation, stimulus_text, stimulus_asset, assessment_mode, difficulty),
                )
            from .specialized_catalog import SCHOOL_LISTENING_STIMULI, LEGACY_SCHOOL_LISTENING_PROMPT
            for item_id, stimulus in SCHOOL_LISTENING_STIMULI.items():
                original = next(item for item in SPECIALIZED_ITEMS if item[0] == item_id)
                # Only the exact shipped catalog row is upgraded; a changed row is not rewritten.
                # Changed item identity invalidates its previous frozen prompts on normal read.
                connection.execute('''UPDATE diagnostic_item SET prompt=?,answer_type='listening_text',stimulus_text=?
                    WHERE diagnostic_item_id=? AND knowledge_node_id=? AND source_ref='hsc_original_v1'
                      AND prompt IN (?,?) AND answer_type IN ('text','listening_text')
                      AND (stimulus_text IS NULL OR stimulus_text=?)''',
                    (original[2], stimulus, item_id, original[1], original[2], LEGACY_SCHOOL_LISTENING_PROMPT, stimulus))
            connection.commit()

    def dashboard(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            child = connection.execute("SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            tracks = ("school",) if child["grade"] == "初三" else ("school", "pet", "competition_math")
            placeholders = ",".join("?" for _ in tracks)
            rows = connection.execute(
                f"""
                SELECT kn.knowledge_node_id, kn.track, kn.subject, kn.code, kn.title, kn.description,
                       ms.mastery_probability, ms.evidence_count, ms.hint_dependency,
                       ms.status_confidence, ms.last_practiced_at,
                       ms.last_independent_correct_at, ms.forgetting_rate, ms.next_review_at
                FROM knowledge_node kn
                LEFT JOIN mastery_state ms ON ms.knowledge_node_id = kn.knowledge_node_id AND ms.child_id = ?
                WHERE kn.track IN ({placeholders}) AND kn.status = 'active'
                  AND (kn.track != 'school' OR kn.grade_band = ?)
                  AND kn.node_type = 'skill'
                ORDER BY kn.track, kn.code
                """,
                (child_id, *tracks, child["grade"]),
            ).fetchall()
            items = []
            context = self._current_learning_context(connection, child_id)
            for row in rows:
                item = dict(row)
                current = self._current_node_evidence(context, row["knowledge_node_id"])
                item.update(current_evidence=current, readiness=current["readiness"],
                    mastery_fields_source="historical_cache", mastery_claim_allowed=False)
                item.update(self._path_context(connection, child_id, row["knowledge_node_id"], context=context))
                items.append(item)
        return {
            "child_id": child_id,
            "algorithm_version": "mastery_rules_v2",
            "knowledge_version": "starter_v2",
            "items": items,
            "pet_baseline": self.pet_baseline(child_id) if child["grade"] == "四年级" else None,
        }

    def pet_baseline(self, child_id: str) -> dict:
        all_item_ids = tuple(
            item_id
            for _, _, _, tasks in PET_BASELINE_DEFINITION
            for _, item_ids in tasks
            for item_id in item_ids
        )
        placeholders = ",".join("?" for _ in all_item_ids)
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT grade FROM child_profile WHERE child_id = ? AND status = 'active'",
                (child_id,),
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            rows = connection.execute(
                f"""
                SELECT diagnostic_item_id, correctness, created_at
                FROM diagnostic_attempt
                WHERE child_id = ? AND diagnostic_item_id IN ({placeholders})
                ORDER BY created_at
                """,
                (child_id, *all_item_ids),
            ).fetchall()
        latest_by_item = {row["diagnostic_item_id"]: row for row in rows}
        dimensions = []
        for key, label, score_kind, tasks in PET_BASELINE_DEFINITION:
            task_results = []
            scores = []
            for task_key, item_ids in tasks:
                candidates = [latest_by_item[item_id] for item_id in item_ids if item_id in latest_by_item]
                latest = max(candidates, key=lambda row: row["created_at"]) if candidates else None
                if latest:
                    scores.append(float(latest["correctness"]))
                task_results.append({
                    "task": task_key,
                    "completed": latest is not None,
                    "last_attempt_at": latest["created_at"] if latest else None,
                })
            completed_tasks = sum(1 for task in task_results if task["completed"])
            dimensions.append({
                "key": key,
                "label": label,
                "score_kind": score_kind,
                "completed_tasks": completed_tasks,
                "required_tasks": len(task_results),
                "status": "completed" if completed_tasks == len(task_results) else "in_progress" if completed_tasks else "not_started",
                "reference_score": round(sum(scores) / len(scores) * 100) if scores else None,
                "tasks": task_results,
            })
        completed_tasks = sum(item["completed_tasks"] for item in dimensions)
        required_tasks = sum(item["required_tasks"] for item in dimensions)
        return {
            "definition_version": "pet_baseline_v1",
            "status": "completed" if completed_tasks == required_tasks else "in_progress" if completed_tasks else "not_started",
            "completed_tasks": completed_tasks,
            "required_tasks": required_tasks,
            "evidence_count": len(rows),
            "reference_only": True,
            "boundary": "完成仅表示听说读写规定任务已有证据，不代表达到PET通过水平；写作、口语和朗读均为未校准参考评估。",
            "dimensions": dimensions,
        }

    def graph(self, child_id: str) -> dict:
        with self.database.connect() as connection:
            child = connection.execute("SELECT grade FROM child_profile WHERE child_id = ?", (child_id,)).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            tracks = ("school",) if child["grade"] == "初三" else ("school", "pet", "competition_math")
            placeholders = ",".join("?" for _ in tracks)
            nodes = connection.execute(
                f"""
                SELECT kn.knowledge_node_id, kn.node_type, kn.track, kn.subject, kn.grade_band,
                       kn.code, kn.title, kn.description, kn.source_ref, kn.knowledge_version,
                       ms.mastery_probability, ms.status_confidence
                FROM knowledge_node kn
                LEFT JOIN mastery_state ms ON ms.knowledge_node_id = kn.knowledge_node_id
                  AND ms.child_id = ?
                WHERE kn.track IN ({placeholders}) AND kn.status = 'active'
                  AND (kn.track != 'school' OR kn.grade_band = ?)
                ORDER BY kn.track, kn.code
                """,
                (child_id, *tracks, child["grade"]),
            ).fetchall()
            node_ids = {row["knowledge_node_id"] for row in nodes}
            edges = connection.execute(
                """
                SELECT edge_id, source_node_id, target_node_id, relation_type, rationale,
                       source_ref, knowledge_version
                FROM knowledge_edge WHERE status = 'active'
                ORDER BY relation_type, edge_id
                """
            ).fetchall()
            context = self._current_learning_context(connection, child_id)
            current_nodes = []
            for row in nodes:
                current = self._current_node_evidence(context, row["knowledge_node_id"])
                current_nodes.append({**dict(row), "current_evidence": current, "readiness": current["readiness"],
                    "mastery_fields_source": "historical_cache", "mastery_claim_allowed": False})
        return {
            "child_id": child_id,
            "knowledge_version": "starter_v2",
            "nodes": current_nodes,
            "edges": [dict(row) for row in edges if row["source_node_id"] in node_ids and row["target_node_id"] in node_ids],
        }

    def history(self, child_id: str, knowledge_node_id: str, limit: int = 20) -> list[dict]:
        with self.database.connect() as connection:
            child = connection.execute(
                "SELECT 1 FROM child_profile WHERE child_id = ?", (child_id,)
            ).fetchone()
            if not child:
                raise UnknownChildError(child_id)
            rows = connection.execute(
                """
                SELECT mue.old_state_json, mue.new_state_json, mue.algorithm_version,
                       mue.created_at, le.evidence_type, le.correctness, le.hint_level,
                       le.independent, le.confidence, le.evidence_summary, le.occurred_at
                FROM mastery_update_event mue
                JOIN learning_evidence le ON le.evidence_id = mue.evidence_id
                WHERE mue.child_id = ? AND mue.knowledge_node_id = ?
                ORDER BY mue.created_at DESC LIMIT ?
                """,
                (child_id, knowledge_node_id, max(1, min(limit, 100))),
            ).fetchall()
        return [
            {
                **{key: row[key] for key in (
                    "algorithm_version", "created_at", "evidence_type", "correctness",
                    "hint_level", "independent", "confidence", "evidence_summary", "occurred_at",
                )},
                "old_state": json.loads(row["old_state_json"]),
                "new_state": json.loads(row["new_state_json"]),
            }
            for row in rows
        ]

    def next_diagnostic(
        self, child_id: str, track: str, preferred_item_id: str | None = None,
        subject: str | None = None, *, require_unexposed: bool = False, knowledge_node_id: str | None = None,
    ) -> dict | None:
        with self.database.connect() as connection:
            context = None
            history = None
            now = datetime.fromisoformat(utc_now())
            if require_unexposed:
                from .goal_question_inventory import shown_question_identities, unexposed_practice_item
                history = shown_question_identities(connection, child_id)
            def eligible(item_id):
                return self._eligible_diagnostic(connection, child_id, item_id) and (
                    not require_unexposed or bool(unexposed_practice_item(connection, child_id, item_id, now, history)))
            if preferred_item_id:
                preferred = connection.execute(
                    """
                    SELECT di.diagnostic_item_id, di.prompt, di.answer_type, di.difficulty,
                           di.stimulus_text, di.stimulus_asset, di.assessment_mode,
                           kn.knowledge_node_id, kn.track, kn.subject, kn.title
                    FROM diagnostic_item di
                    JOIN knowledge_node kn ON kn.knowledge_node_id = di.knowledge_node_id
                    JOIN child_profile cp ON cp.child_id = ? AND cp.status = 'active'
                    WHERE di.diagnostic_item_id = ? AND kn.track = ? AND di.status = 'active'
                      AND (kn.track != 'school' OR kn.grade_band = cp.grade)
                    """,
                    (child_id, preferred_item_id, track),
                ).fetchone()
                if preferred and (knowledge_node_id is None or preferred['knowledge_node_id']==knowledge_node_id) and (
                        not require_unexposed or subject is None or preferred['subject']==subject) and eligible(preferred["diagnostic_item_id"]):
                    result = self._public_item(connection, child_id, preferred)
                    result.update(self._path_context(connection, child_id, preferred["knowledge_node_id"]))
                    return result
                return None
            due = connection.execute(
                """
                SELECT di.diagnostic_item_id, di.prompt, di.answer_type, di.difficulty,
                       di.stimulus_text, di.stimulus_asset, di.assessment_mode,
                       kn.knowledge_node_id, kn.track, kn.subject, kn.title,
                       rt.review_task_id, rt.interval_label AS review_interval, rt.due_at
                FROM review_task rt
                JOIN diagnostic_item di ON di.diagnostic_item_id = rt.diagnostic_item_id
                JOIN knowledge_node kn ON kn.knowledge_node_id = rt.knowledge_node_id
                WHERE rt.child_id = ? AND kn.track = ? AND rt.status = 'pending'
                  AND rt.due_at <= ? AND di.status = 'active'
                  AND (? IS NULL OR kn.subject = ?)
                ORDER BY (rt.interval_label='next_day') DESC, rt.due_at, rt.created_at
                """,
                (child_id, track, utc_now(), subject, subject),
            ).fetchall()
            qualified_due = None
            if due:
                context = self._current_learning_context(connection, child_id)
            projections = context["projections"] if context else {}
            for candidate in due if not require_unexposed else ():
                try:
                    StudyRepository._validate_review_source(connection, candidate["review_task_id"], child_id,
                        allowed_statuses=("pending",), evidence_projections=projections)
                except PlanInputError:
                    continue
                qualified_due = candidate
                break
            due = qualified_due
            if due:
                result = self._public_item(connection, child_id, due)
                result.update(self._path_context(connection, child_id, due["knowledge_node_id"], context=context))
                return result
            row = connection.execute(
                """
                SELECT di.diagnostic_item_id, di.prompt, di.answer_type, di.difficulty,
                       di.stimulus_text, di.stimulus_asset, di.assessment_mode,
                       kn.knowledge_node_id, kn.track, kn.subject, kn.title
                FROM diagnostic_item di JOIN knowledge_node kn ON kn.knowledge_node_id = di.knowledge_node_id
                JOIN child_profile cp ON cp.child_id = ? AND cp.status = 'active'
                WHERE kn.track = ? AND di.status = 'active'
                  AND (kn.track != 'school' OR kn.grade_band = cp.grade)
                  AND di.diagnostic_item_id NOT LIKE 'diag-pet-daily-%'
                  AND (? IS NULL OR kn.subject = ?)
                  AND (? IS NULL OR kn.knowledge_node_id = ?)
                ORDER BY di.difficulty, di.diagnostic_item_id
                """,
                (child_id, track, subject, subject, knowledge_node_id, knowledge_node_id),
            ).fetchall()
            if row and context is None:
                context = self._current_learning_context(connection, child_id)
            from .learning_feedback import PRIORITY
            def priority(candidate):
                node_id = candidate["knowledge_node_id"]
                current = self._current_node_evidence(context, node_id)
                unmet = any(self._current_node_evidence(context, parent["knowledge_node_id"])["readiness"] != "ready"
                    for parent in context["prerequisites"].get(node_id, []))
                return (unmet, PRIORITY.get(current["status"], 2), current["attempt_count"],
                        candidate["difficulty"], candidate["diagnostic_item_id"])
            row = sorted(row, key=priority)
            row = next((candidate for candidate in row if eligible(candidate["diagnostic_item_id"])), None)
            if row:
                result = self._public_item(connection, child_id, row)
                result.update(self._path_context(connection, child_id, row["knowledge_node_id"], context=context))
                return result
        return None

    @staticmethod
    def _public_item(connection, child_id: str, row) -> dict:
        from .pet_skill_gate import pet_skill_gate
        result = dict(row)
        modality = pet_skill_gate(connection, child_id, row["diagnostic_item_id"])["modality"]
        result["pet_audio_modality"] = modality if modality in {"listening", "speaking"} else None
        if row['track'] == 'school' and modality == 'listening':
            result['stimulus_text'] = None
        return result

    @staticmethod
    def _eligible_diagnostic(connection, child_id: str, item_id: str) -> bool:
        from .local_supply import LocalSupplyError, validate_item
        from .learning_grade_scope import allows_learning_grade
        row = connection.execute("""SELECT di.source_ref,kn.grade_band,kn.track,cp.grade FROM diagnostic_item di
            JOIN knowledge_node kn USING(knowledge_node_id) JOIN child_profile cp ON cp.child_id=?
            WHERE di.diagnostic_item_id=? AND di.status='active' AND kn.status='active' AND cp.status='active'""", (child_id, item_id)).fetchone()
        if not row or not allows_learning_grade(row['grade'], row['grade_band'], row['track']):
            return False
        if (row["source_ref"] or "").startswith(("local_verified_", "parent_approved_")):
            try:
                validate_item(connection, child_id, item_id)
            except LocalSupplyError:
                return False
        return True

    @staticmethod
    def _current_learning_context(connection, child_id: str) -> dict:
        from .learning_feedback import build_learning_feedback
        now = datetime.fromisoformat(utc_now())
        projections = project_child_mastery_evidence(connection, child_id, as_of=now.isoformat())
        feedback = build_learning_feedback(connection, child_id, now, evidence_projections=projections)
        edges = connection.execute("""SELECT edge.source_node_id,edge.target_node_id,edge.relation_type,
            source.title AS source_title,source.track,ms.mastery_probability,ms.status_confidence,
            target.title AS target_title FROM knowledge_edge edge
            JOIN knowledge_node source ON source.knowledge_node_id=edge.source_node_id AND source.status='active'
            JOIN knowledge_node target ON target.knowledge_node_id=edge.target_node_id AND target.status='active'
            LEFT JOIN mastery_state ms ON ms.child_id=? AND ms.knowledge_node_id=edge.source_node_id
            WHERE edge.status='active' AND edge.relation_type IN ('prerequisite_of','transfers_to')
            ORDER BY edge.relation_type,source.code,target.code""", (child_id,)).fetchall()
        prerequisites, next_paths = {}, {}
        for edge in edges:
            if edge["relation_type"] == "prerequisite_of":
                prerequisites.setdefault(edge["target_node_id"], []).append({
                    "knowledge_node_id": edge["source_node_id"], "title": edge["source_title"], "track": edge["track"],
                    "mastery_probability": edge["mastery_probability"], "status_confidence": edge["status_confidence"]})
            next_paths.setdefault(edge["source_node_id"], []).append({"knowledge_node_id": edge["target_node_id"],
                "title": edge["target_title"], "relation_type": edge["relation_type"]})
        return {"projections": projections, "feedback": {node["knowledge_node_id"]: node for node in feedback["nodes"]},
                "prerequisites": prerequisites, "next_paths": next_paths}

    @staticmethod
    def _current_node_evidence(context: dict, node_id: str) -> dict:
        node = context["feedback"].get(node_id, {})
        projection = context["projections"].get(node_id, {})
        ready = (node.get("status") == "retention_observed" and node.get("independent_distinct_items", 0) >= 2
                 and node.get("retention_success_count", 0) >= 1)
        return {"status": node.get("status", "needs_evidence"), "readiness": "ready" if ready else "needs_evidence",
            "readiness_basis": "independent_distinct_questions_and_delayed_retention", "window_days": 90,
            **{key: node.get(key, 0) for key in ("attempt_count", "independent_attempt_count", "supported_attempt_count",
                "independent_correct", "distinct_items", "independent_distinct_items", "retention_success_count")},
            "evidence_refs": list(node.get("evidence_refs", [])), "retention_basis_refs": list(node.get("retention_basis_refs", [])),
            "latest_evidence_at": node.get("latest_evidence_at"), "evidence_fingerprint": projection.get("evidence_fingerprint"),
            "mastery_claim_allowed": False}

    @staticmethod
    def _path_context(connection, child_id: str, knowledge_node_id: str, *, context: dict | None = None) -> dict:
        if context is None:
            context = LearningStateService._current_learning_context(connection, child_id)
        prerequisites = []
        for item in context["prerequisites"].get(knowledge_node_id, []):
            current = LearningStateService._current_node_evidence(context, item["knowledge_node_id"])
            prerequisites.append({**item, "readiness": current["readiness"], "current_evidence": current,
                "mastery_fields_source": "historical_cache", "mastery_claim_allowed": False})
        return {"prerequisites": prerequisites, "next_paths": context["next_paths"].get(knowledge_node_id, [])}

    def submit_diagnostic(self, session_id: str, item_id: str, answer: str, client_action_id: str, review_task_id: str | None = None, *, hint_level: int = 0, _trusted_prompt_id: str | None = None, _trusted_exam_slot: tuple[str, int] | None = None, _pet_media_scope: dict | None = None) -> dict:
        if type(hint_level) is not int or not 0 <= hint_level <= 5:
            raise ValueError("invalid diagnostic hint level")
        if _pet_media_scope is not None and (_trusted_prompt_id is None or not isinstance(_pet_media_scope, dict)
                or set(_pet_media_scope) - {"media_root", "data_encryption_key"}):
            raise ValueError("trusted diagnostic media scope required")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (_trusted_prompt_id is None and isinstance(client_action_id, str)
                    and client_action_id.startswith("diagnostic-prompt-answer:")):
                raise ValueError("trusted diagnostic prompt required for reserved action")
            if _trusted_prompt_id is not None and _trusted_exam_slot is not None:
                raise ValueError("diagnostic scoring owners conflict")
            if (_trusted_exam_slot is None and isinstance(client_action_id, str)
                    and client_action_id.startswith("exam-bound-answer:")):
                raise ValueError("trusted exam slot required for reserved action")
            existing = connection.execute("SELECT * FROM diagnostic_attempt WHERE client_action_id = ?", (client_action_id,)).fetchone()
            if _trusted_exam_slot is None and not existing and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='specialized_exam_run'").fetchone() and connection.execute(
                    """SELECT 1 FROM specialized_exam_run r JOIN specialized_exam_item i USING(run_id)
                        WHERE r.session_id=? AND r.delivery_phase!='ordinary'
                        AND r.status IN ('active','paused') AND i.diagnostic_item_id=?""",
                    (session_id,item_id)).fetchone():
                raise ValueError('exam_item_requires_exam_submit')
            if _trusted_prompt_id is not None:
                from .diagnostic_prompt import DiagnosticPromptService
                DiagnosticPromptService._validate_scoring_permission(
                    connection, prompt_id=_trusted_prompt_id, session_id=session_id, item_id=item_id,
                    answer=answer, client_action_id=client_action_id, review_task_id=review_task_id,
                    hint_level=hint_level, existing=existing,
                )
            if _trusted_exam_slot is not None:
                from .exam_simulation import ExamSimulationService
                ExamSimulationService._validate_scoring_permission(
                    connection, exam_slot=_trusted_exam_slot, session_id=session_id, item_id=item_id,
                    answer=answer, client_action_id=client_action_id, review_task_id=review_task_id,
                    hint_level=hint_level, existing=existing,
                )
            if existing:
                if (existing["session_id"], existing["diagnostic_item_id"], existing["submitted_answer"], existing["review_task_id"]) != (session_id, item_id, answer, review_task_id):
                    raise ValueError("diagnostic action ownership or payload conflict")
                owner = connection.execute("""SELECT 1 FROM daily_session ds JOIN learning_evidence le ON le.session_id=ds.session_id
                    WHERE ds.session_id=? AND ds.child_id=? AND le.evidence_id=? AND le.child_id=ds.child_id""",
                    (session_id, existing["child_id"], existing["evidence_id"])).fetchone()
                if not owner:
                    raise ValueError("diagnostic receipt ownership conflict")
                conditions = connection.execute("SELECT hint_level FROM learning_evidence WHERE evidence_id=?", (existing["evidence_id"],)).fetchone()
                if conditions[0] != hint_level:
                    raise ValueError("diagnostic hint payload conflict")
                replayed_attempt = self._attempt(connection, existing["attempt_id"])
                if review_task_id:
                    try:
                        original = StudyRepository._validate_review_source(connection, review_task_id, existing["child_id"], allowed_statuses=("completed", "pending"))
                        if (original["status"] == "completed" and original["result_evidence_id"] != existing["evidence_id"]
                                or original["status"] == "pending" and replayed_attempt["score_status"] != "needs_review"):
                            raise PlanInputError("review receipt conflict")
                    except PlanInputError as error:
                        raise UnknownReviewTaskError(review_task_id) from error
                return replayed_attempt
            session = connection.execute("SELECT child_id, state, status FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
            if not session:
                raise UnknownSessionError(session_id)
            if session["status"] != "active" or (session["state"] != "study" and _trusted_prompt_id is None and _trusted_exam_slot is None):
                raise StaleSessionStateError("study", session["state"])
            item = connection.execute(
                """
                SELECT di.diagnostic_item_id, di.knowledge_node_id, di.answer_type,
                       di.expected_answer_json, di.explanation, di.assessment_mode
                FROM diagnostic_item di WHERE di.diagnostic_item_id = ? AND di.status = 'active'
                """,
                (item_id,),
            ).fetchone()
            if not item or not self._eligible_diagnostic(connection, session["child_id"], item_id):
                raise UnknownDiagnosticItemError(item_id)
            review_task = None
            if not answer.strip():
                raise ValueError("diagnostic answer must not be blank")
            if review_task_id:
                review_task = connection.execute(
                    """
                    SELECT review_task_id, child_id, diagnostic_item_id, status, due_at
                    FROM review_task WHERE review_task_id = ?
                    """,
                    (review_task_id,),
                ).fetchone()
                if not review_task or review_task["child_id"] != session["child_id"] or review_task["diagnostic_item_id"] != item_id or review_task["status"] != "pending":
                    raise UnknownReviewTaskError(review_task_id)
                if datetime.fromisoformat(review_task["due_at"]) > datetime.fromisoformat(now):
                    raise ReviewTaskNotDueError("review task is not due / 复测尚未到期")
                try:
                    StudyRepository._validate_review_source(connection, review_task_id, session["child_id"], allowed_statuses=("pending",))
                except PlanInputError as error:
                    raise UnknownReviewTaskError(review_task_id) from error
                from .writing_rubric import is_writing
                if is_writing(connection, item_id):
                    from .school_open_reviews import is_school_item
                    if is_school_item(connection, item_id):
                        from .school_review_execution import validate_answer as validate_writing_review_answer
                    else:
                        from .writing_stage_reviews import validate_writing_review_answer
                    validate_writing_review_answer(connection, child_id=session["child_id"], session_id=session_id,
                        item_id=item_id, review_task_id=review_task_id, prompt_id=_trusted_prompt_id,
                        answer=answer, as_of=now)
            review_independent = True
            numeric_practice = False
            if _trusted_prompt_id is not None:
                from .listening_review_submission import scoring_independence
                saved_prompt = connection.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
                    (_trusted_prompt_id,)).fetchone()
                review_independent = scoring_independence(connection, saved_prompt)
                numeric_practice = json.loads(saved_prompt['request_json']).get('numeric_clarification') is not None
            correctness, feedback = self._score_answer(item, answer)
            from .pet_skill_gate import pet_skill_gate
            pet_scope = {}
            if _trusted_prompt_id is not None and _pet_media_scope is not None:
                pinned = connection.execute("SELECT item_version FROM diagnostic_prompt WHERE prompt_id=?", (_trusted_prompt_id,)).fetchone()
                pet_scope = dict(_pet_media_scope, session_id=session_id, prompt_id=_trusted_prompt_id, item_version=pinned[0])
            if _trusted_exam_slot is not None:
                from .exam_simulation import ExamSimulationService
                if ExamSimulationService._listening_item(connection,item_id):
                    from .exam_listening_delivery import scoring_scope
                    run = connection.execute('SELECT * FROM specialized_exam_run WHERE run_id=?',(_trusted_exam_slot[0],)).fetchone()
                    slot = connection.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=?',_trusted_exam_slot).fetchone()
                    pet_scope = scoring_scope(connection,run,slot,answer=answer)
            modality = pet_skill_gate(connection, session["child_id"], item_id, answer=answer, **pet_scope)
            if modality["modality"] == "listening":
                from .pet_listening_scoring import qualified_listening_score
                qualified = qualified_listening_score(connection, session["child_id"], item_id, answer, **pet_scope)
                if qualified is not None:
                    correctness = qualified[1]
                    feedback = "本次原题听力练习回答正确。" if correctness == 1 else "本次原题听力练习答案尚未匹配，请核对。"
            else:
                qualified = qualified_exact_score(connection, session["child_id"], item_id, answer)
            # A text-content score is retained as a reference, never certified as
            # hearing/speaking skill even when an older scorer grants exactness.
            reliable = modality["skill_scoring_allowed"] and qualified is not None and qualified[1] == correctness
            if not reliable:
                feedback = "作答已保存，当前评分方式不能可靠确认对错，需要进一步核实。"
            exposure = project_item_exposure(connection, session["child_id"], item_id, as_of=now)
            repeated = exposure["answer_repeated"]
            independent = int(reliable and review_independent and not numeric_practice and hint_level == 0 and not repeated and not exposure["tutor_assisted"])
            confidence = 1.0 if reliable else 0.0 if correctness is None else .35
            summary = "原创诊断题作答"
            if not review_independent:
                summary += ';independence_reason=listening_review_pre_delivery_response'
            if numeric_practice:
                summary += ';independence_reason=numeric_clarification_same_item'
            if repeated:
                summary += ";independence_reason=same_item_within_24h"
            elif hint_level:
                summary += ";independence_reason=reported_hint"
            elif exposure["tutor_assisted"]:
                summary += ";independence_reason=tutor_assistance_within_24h"
            elif not reliable:
                summary += ";independence_reason=unreliable_scoring"
            if _trusted_prompt_id is not None:
                saved = connection.execute("SELECT updated_at FROM diagnostic_prompt WHERE prompt_id=? AND status='answer_saved'",
                                           (_trusted_prompt_id,)).fetchone()
                if saved:
                    summary += ";prompt_answer_saved_at=" + saved[0]
            if _trusted_exam_slot is not None and pet_scope:
                saved = connection.execute('SELECT answer_arrived_at FROM specialized_exam_item WHERE run_id=? AND position=?',_trusted_exam_slot).fetchone()
                if saved and saved[0]:
                    summary += ';prompt_answer_saved_at=' + saved[0]
            evidence_id = str(uuid4())
            self._insert_evidence(connection, evidence_id, session["child_id"], session_id, item["knowledge_node_id"], "diagnostic_answer", correctness, hint_level, independent, confidence, summary, client_action_id, now)
            attempt_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO diagnostic_attempt(
                  attempt_id, diagnostic_item_id, evidence_id, child_id, session_id,
                  submitted_answer, correctness, feedback, review_task_id, created_at, client_action_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (attempt_id, item_id, evidence_id, session["child_id"], session_id, answer, correctness, feedback, review_task_id, now, client_action_id),
            )
            if review_task and reliable:
                connection.execute(
                    "UPDATE review_task SET status = 'completed', completed_at = ?, result_evidence_id = ? WHERE review_task_id = ?",
                    (now, evidence_id, review_task_id),
                )
                if correctness < 1:
                    self._schedule_next_day_review(connection, session["child_id"], item["knowledge_node_id"], item_id, evidence_id, now)
            elif not review_task and correctness is not None and modality['modality'] != 'listening':
                self._schedule_reviews(connection, session["child_id"], item["knowledge_node_id"], item_id, evidence_id, now)
            next_due = connection.execute(
                "SELECT MIN(due_at) FROM review_task WHERE child_id = ? AND knowledge_node_id = ? AND status = 'pending'",
                (session["child_id"], item["knowledge_node_id"]),
            ).fetchone()[0]
            self._update_mastery(
                connection, session["child_id"], item["knowledge_node_id"], evidence_id,
                correctness, hint_level, bool(independent), confidence, now, next_due,
            )
            connection.commit()
            return self._attempt(connection, attempt_id)

    @staticmethod
    def _score_answer(item, answer: str) -> tuple[float | None, str]:
        if item['answer_type'] == 'open_response' or item['assessment_mode'] == 'open_response_rubric':
            return None, '作答已保存，尚未评价，不据此判定对错或掌握。'
        from .local_supply import score_local_item
        local_score = score_local_item(item, answer)
        if local_score is not None:
            return local_score
        expected = json.loads(item["expected_answer_json"])
        if expected.get('answer_format') == 'reviewed_spoken_numeric_tuple_v1':
            return None, '原话已保存，各个数值还不能可靠核对。请按原题顺序逐个说清楚，不会据此判定对错或掌握。'
        if expected.get('answer_format') == 'reviewed_spoken_numeric_v1':
            return None, '原话已保存，还不能确定一个数值答案，请再说一次答案。'
        uses_open_response_rubric = (
            item["assessment_mode"] == "pet_reference_rubric"
            or bool(expected.get("task_points") and expected.get("keyword_groups"))
        )
        if uses_open_response_rubric:
            words = [word for word in answer.replace("'", " ").replace("-", " ").split() if any(character.isalpha() for character in word)]
            minimum = int(expected.get("min_words", 20))
            length_score = min(1.0, len(words) / max(minimum, 1))
            normalized = answer.lower()
            task_points = expected.get("task_points", [])
            keyword_groups = expected.get("keyword_groups") or [[point] for point in task_points]
            covered = sum(1 for group in keyword_groups if any(keyword.lower() in normalized for keyword in group))
            coverage_score = covered / max(len(task_points), 1)
            connector_score = 1.0 if any(connector in normalized for connector in (" and ", " because ", " but ", " then ", " so ")) else 0.4
            score = round(0.45 * length_score + 0.4 * coverage_score + 0.15 * connector_score, 2)
            if item["answer_type"] == "read_aloud":
                mode_note = "本次只检查本地转写覆盖，不能判断发音、重音、语调或流利度。"
            elif item["answer_type"] == "speaking":
                mode_note = "本次只根据转写文本判断内容，不能判断发音。"
            else:
                mode_note = "本次为家庭版写作参考评估，不代替学校或考试正式评分。"
            return score, f"参考评估：任务点覆盖{covered}/{len(task_points)}，词数约{len(words)}。{mode_note} {item['explanation']}"
        keywords = expected.get("keywords", [])
        normalized = normalize_spoken_math(answer)
        matched = sum(1 for keyword in keywords if normalize_spoken_math(str(keyword)) in normalized)
        correctness = 1.0 if matched >= max(1, min(2, len(keywords))) else 0.0
        feedback = "回答正确。" if correctness == 1.0 else f"这次还没有完全答对。参考思路：{item['explanation']}"
        return correctness, feedback

    @staticmethod
    def _qualified_review_item(connection, child_id: str, item_id: str) -> bool:
        from .listening_reviews import listening_review_source_item
        if listening_review_source_item(connection, child_id, item_id) is not None:
            return True
        row = connection.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()
        try:
            answers = json.loads(row[0]).get("exact_answers") if row else None
            if not isinstance(answers, list) or not answers or not isinstance(answers[0], str) or not answers[0].strip():
                return False
            qualified = qualified_exact_score(connection, child_id, item_id, answers[0])
            return bool(qualified and qualified[1] == 1)
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _qualified_review_source(connection, child_id: str, node_id: str, evidence_id: str, *, as_of: str) -> bool:
        projection = project_mastery_evidence(connection, child_id, node_id, as_of=as_of)
        return any(row["evidence_id"] == evidence_id for row in projection["effective_evidence"])

    @staticmethod
    def _select_review_item(connection, child_id: str, node_id: str, original_item_id: str) -> str | None:
        candidates = connection.execute("""SELECT diagnostic_item_id FROM diagnostic_item
            WHERE knowledge_node_id=? AND status='active'
            ORDER BY (diagnostic_item_id=?),difficulty,diagnostic_item_id""", (node_id, original_item_id))
        return next((row[0] for row in candidates
            if LearningStateService._qualified_review_item(connection, child_id, row[0])), None)

    @staticmethod
    def _schedule_reviews(connection, child_id: str, node_id: str, original_item_id: str, evidence_id: str, now: str) -> None:
        if not LearningStateService._qualified_review_source(connection, child_id, node_id, evidence_id, as_of=now):
            return
        review_item_id = LearningStateService._select_review_item(connection, child_id, node_id, original_item_id)
        if not review_item_id:
            return
        base = datetime.fromisoformat(now)
        for label, due in (
            ("immediate", base),
            ("48h", base + timedelta(hours=48)),
            ("7d", base + timedelta(days=7)),
        ):
            connection.execute(
                """
                INSERT OR IGNORE INTO review_task(
                  review_task_id, child_id, knowledge_node_id, diagnostic_item_id,
                  source_evidence_id, interval_label, due_at, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (str(uuid4()), child_id, node_id, review_item_id, evidence_id, label, due.isoformat(), now),
            )

    def derive_reflection_evidence(self, session_id: str) -> None:
        with self.database.connect() as connection:
            session = connection.execute("SELECT child_id FROM daily_session WHERE session_id = ?", (session_id,)).fetchone()
            reflection = connection.execute("SELECT learned_text, difficult_text, skipped FROM daily_reflection WHERE session_id = ?", (session_id,)).fetchone()
            if not session or not reflection or reflection["skipped"]:
                return
            combined = f"{reflection['learned_text']} {reflection['difficult_text']}"
            mappings = (("判别式", "node-j3-math-discriminant"), ("求根公式", "node-j3-math-quadratic-formula"), ("主旨", "node-pet-reading-main-idea"), ("邮件", "node-pet-writing-email"), ("规律", "node-oly-pattern"), ("计数", "node-oly-counting"), ("面积", "node-oly-area"))
            for keyword, node_id in mappings:
                if keyword not in combined:
                    continue
                action_id = f"reflection:{session_id}:{node_id}"
                if connection.execute("SELECT 1 FROM learning_evidence WHERE client_action_id = ?", (action_id,)).fetchone():
                    continue
                evidence_id = str(uuid4())
                now = utc_now()
                self._insert_evidence(connection, evidence_id, session["child_id"], session_id, node_id, "child_self_report", None, 0, 0, 0.35, f"孩子复盘提到：{keyword}", action_id, now)
                self._update_mastery(connection, session["child_id"], node_id, evidence_id, None, 0, False, 0.35, now)
            connection.commit()

    @staticmethod
    def _insert_evidence(connection, evidence_id, child_id, session_id, node_id, evidence_type, correctness, hint_level, independent, confidence, summary, action_id, now):
        connection.execute(
            """
            INSERT INTO learning_evidence(
              evidence_id, child_id, session_id, knowledge_node_id, evidence_type, correctness,
              hint_level, independent, confidence, evidence_summary, occurred_at, client_action_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (evidence_id, child_id, session_id, node_id, evidence_type, correctness, hint_level, independent, confidence, summary, now, action_id),
        )

    @staticmethod
    def _update_mastery(connection, child_id, node_id, evidence_id, correctness, hint_level, independent, confidence, now, next_review_override=None):
        # Callers must persist the genuine response receipt before replaying.
        if correctness is None:
            return
        replay = LearningStateService._replay_mastery_scope(connection, child_id, node_id, now, "response_received")
        if evidence_id in {row["evidence_id"] for row in replay["effective_evidence"]}:
            connection.execute("""INSERT OR IGNORE INTO mastery_update_event(
                event_id,child_id,knowledge_node_id,evidence_id,old_state_json,new_state_json,algorithm_version,created_at)
                VALUES (?,?,?,?,?,?,?,?)""", (str(uuid4()), child_id, node_id, evidence_id,
                    json.dumps(replay["old_state"], ensure_ascii=False), json.dumps(replay["state"], ensure_ascii=False),
                    MASTERY_REPLAY_VERSION, now))

    def replay_mastery(self, child_id: str, knowledge_node_id: str | None = None, *, as_of: str | None = None,
                       connection=None, reason: str = "explicit_replay") -> dict:
        """Explicit repair; a borrowed transaction remains owned by the caller.

        No schema initialization, automatic scheduling or edits to raw evidence.
        On a borrowed-call failure the caller must roll back its transaction.
        """
        if not isinstance(reason, str) or not reason or len(reason) > 80 or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in reason):
            raise ValueError("MASTERY_REPLAY_REASON_INVALID")
        now = as_of or utc_now()
        if connection is None:
            with self.database.connect() as opened:
                opened.execute("BEGIN IMMEDIATE")
                result = self.replay_mastery(child_id, knowledge_node_id, as_of=now, connection=opened, reason=reason)
                opened.commit()
                return result
        if not connection.in_transaction:
            raise ValueError("MASTERY_REPLAY_TRANSACTION_REQUIRED")
        if not connection.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone():
            raise UnknownChildError(child_id)
        nodes = [r[0] for r in connection.execute("""SELECT knowledge_node_id FROM learning_evidence WHERE child_id=?
            UNION SELECT knowledge_node_id FROM mastery_state WHERE child_id=?""", (child_id, child_id))]
        projections = project_child_mastery_evidence(connection, child_id, as_of=now)
        if knowledge_node_id is not None:
            nodes = [node for node in nodes if node == knowledge_node_id]
            if nodes:
                nodes = sorted(set(nodes) | set(projections.get(knowledge_node_id, {}).get("related_knowledge_node_ids", [])))
        result = [self._replay_mastery_node(connection, child_id, node, now, reason,
            projection=projections.get(node) or _empty_mastery_projection(child_id, node)) for node in sorted(nodes)]
        from .school_open_reviews import reconcile_school_reviews
        reconcile_school_reviews(connection, child_id, as_of=now, cancel_only=True)
        return {"version": MASTERY_REPLAY_VERSION, "child_id": child_id, "nodes": result, "mastery_claim_allowed": False}

    @staticmethod
    def _replay_mastery_scope(connection, child_id: str, node_id: str, now: str, reason: str) -> dict:
        projections = project_child_mastery_evidence(connection, child_id, as_of=now)
        result = LearningStateService._replay_mastery_node(connection, child_id, node_id, now, reason,
            projection=projections.get(node_id) or _empty_mastery_projection(child_id, node_id))
        for related in result["related_knowledge_node_ids"]:
            if related != node_id:
                LearningStateService._replay_mastery_node(connection, child_id, related, now, reason,
                    projection=projections.get(related) or _empty_mastery_projection(child_id, related))
        return result

    @staticmethod
    def _replay_mastery_node(connection, child_id: str, node_id: str, now: str, reason: str, *, projection=None) -> dict:
        if projection is None:
            projection = project_mastery_evidence(connection, child_id, node_id, as_of=now)
        new = {"mastery_probability": .35, "evidence_count": 0, "hint_dependency": 0.0,
               "status_confidence": 0.0, "last_practiced_at": None, "last_independent_correct_at": None,
               "forgetting_rate": .2, "next_review_at": None, "algorithm_version": MASTERY_REPLAY_VERSION}
        previous = connection.execute("SELECT * FROM mastery_state WHERE child_id=? AND knowledge_node_id=?", (child_id, node_id)).fetchone()
        old = {key: previous[key] for key in new} if previous else {**new, "algorithm_version": None}
        hint_sum, confidence_sum = 0.0, 0.0
        for evidence in projection["effective_evidence"]:
            independent = bool(evidence["independent"] and evidence["hint_level"] == 0)
            correct = evidence["correctness"] == 1
            delta = (.18 if independent else .10) if correct else -.14
            new["mastery_probability"] = round(max(.05, min(.98, new["mastery_probability"] + delta * evidence["confidence"])), 6)
            new["evidence_count"] += 1
            hint_sum += min(evidence["hint_level"] / 5, 1)
            confidence_sum += evidence["confidence"]
            new["hint_dependency"] = round(hint_sum / new["evidence_count"], 4)
            new["status_confidence"] = round(min(1.0, new["evidence_count"] * .2) * confidence_sum / new["evidence_count"], 4)
            new["last_practiced_at"] = evidence["occurred_at"]
            if correct and independent:
                new["last_independent_correct_at"] = evidence["occurred_at"]
                new["forgetting_rate"] = round(max(.05, new["forgetting_rate"] * .9), 4)
            elif not correct:
                new["forgetting_rate"] = round(min(.5, new["forgetting_rate"] * 1.15), 4)
        if new["evidence_count"]:
            days = 1 if new["mastery_probability"] < .5 else 3 if new["mastery_probability"] < .75 else 7
            # Replay time is not practice time: no postponing a due review on every read.
            new["next_review_at"] = projection["next_review_at"] or (
                datetime.fromisoformat(new["last_practiced_at"]) + timedelta(days=days)).isoformat()
        changed = old != new and (previous is not None or new["evidence_count"] > 0)
        if changed:
            columns = tuple(new)
            connection.execute(f"""INSERT INTO mastery_state(child_id,knowledge_node_id,{','.join(columns)},updated_at)
                VALUES ({','.join('?' for _ in range(len(columns) + 3))})
                ON CONFLICT(child_id,knowledge_node_id) DO UPDATE SET
                {','.join(f'{key}=excluded.{key}' for key in columns)},updated_at=excluded.updated_at""",
                (child_id, node_id, *(new[key] for key in columns), now))
        last = connection.execute("""SELECT summary_json FROM audit_event WHERE child_id=? AND action='mastery_evidence_replayed'
            AND json_extract(summary_json,'$.knowledge_node_id')=? ORDER BY rowid DESC LIMIT 1""", (child_id, node_id)).fetchone()
        if changed or not last or json.loads(last[0]).get("evidence_fingerprint") != projection["evidence_fingerprint"]:
            summary = {**projection, "old_state": old, "state": new, "reason": reason, "as_of": now}
            connection.execute("""INSERT INTO audit_event(audit_id,actor,action,child_id,summary_json,occurred_at)
                VALUES (?,'system','mastery_evidence_replayed',?,?,?)""",
                (str(uuid4()), child_id, json.dumps(summary, ensure_ascii=False), now))
        return {**projection, "old_state": old, "state": new, "changed": changed}

    @staticmethod
    def _attempt(connection, attempt_id: str) -> dict:
        row = connection.execute(
            """
            SELECT attempt_id, diagnostic_item_id, evidence_id, child_id, session_id,
                   submitted_answer, correctness, feedback, review_task_id, created_at
            FROM diagnostic_attempt WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        attempt = dict(row)
        evidence = connection.execute("SELECT knowledge_node_id FROM learning_evidence WHERE evidence_id=? AND child_id=?",
            (attempt["evidence_id"], attempt["child_id"])).fetchone()
        projection = project_mastery_evidence(connection, attempt["child_id"], evidence[0], as_of=utc_now()) if evidence else {}
        accepted = any(item["evidence_id"] == attempt["evidence_id"] for item in projection.get("effective_evidence", []))
        projected_receipt = next((item for item in [*projection.get("effective_evidence", []), *projection.get("excluded_evidence", [])]
            if item["evidence_id"] == attempt["evidence_id"]), {})
        attempt["question_identity"] = projected_receipt.get("question_identity")
        attempt["identity_version"] = projection.get("identity_version")
        reason = next((item["reason"] for item in projection.get("excluded_evidence", [])
            if item["evidence_id"] == attempt["evidence_id"]), "receipt_unavailable")
        attempt["score_status"] = "verified_local" if accepted else "practice_only" if reason in {"same_item_within_24h", "numeric_clarification_same_item"} else "needs_review"
        attempt["mastery_update_allowed"] = accepted
        if not accepted:
            attempt["score_reason"] = reason
            if attempt["score_status"] == "needs_review":
                attempt["feedback"] = "作答已保存，当前评分方式不能可靠确认对错，需要进一步核实。"
                if reason == 'unreliable_score' and attempt['correctness'] is None:
                    from .local_supply import LocalSupplyError, validate_item
                    from .spoken_numeric import FORMAT, parse
                    try:
                        current = validate_item(connection, attempt['child_id'], attempt['diagnostic_item_id'])
                        rubric = json.loads(current['expected_answer_json'])
                        if (rubric.get('answer_format') == FORMAT
                                and parse(attempt['submitted_answer'], rubric.get('numeric_kind')) is None):
                            attempt['feedback'] = '原话已保存，我还不能确认唯一的数值答案。请只说一个数，不带单位或算式。'
                    except (LocalSupplyError, ValueError, TypeError, KeyError):
                        pass
        return attempt


class UnknownDiagnosticItemError(Exception):
    pass


class UnknownReviewTaskError(Exception):
    pass


class ReviewTaskNotDueError(UnknownReviewTaskError, ValueError):
    pass
