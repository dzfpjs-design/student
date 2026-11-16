from __future__ import annotations


# Each entry is an executable baseline diagnostic, not merely a roadmap label.
# The source is the family's original diagnostic catalog; results are reference
# evidence and never presented as an official PET, competition, or exam score.
SPECIALIZED_DIAGNOSTICS = (
    # PET reading
    ("pet-reading-detail", "pet", "英语", "四年级-PET", "PET.READING.DETAIL", "阅读细节定位", "定位短文本中的明确事实。",
     "Read: 'The art club meets on Tuesday in Room 6. Bring a pencil and an old shirt.' When and where does the club meet?", {"keywords": ["Tuesday", "Room 6"]}, "It meets on Tuesday in Room 6.", 1, "pet.reading.detail"),
    ("pet-reading-vocabulary", "pet", "英语", "四年级-PET", "PET.READING.VOCABULARY", "阅读语境词义", "利用上下文理解常见词语。",
     "Read: 'The path was narrow, so only one person could walk on it at a time.' What does narrow mean here?", {"keywords": ["not wide", "small width", "only one"]}, "Narrow means not wide.", 1, "pet.reading.vocabulary"),
    ("pet-reading-inference", "pet", "英语", "四年级-PET", "PET.READING.INFERENCE", "阅读简单推断", "根据多个明确线索作有限推断。",
     "Read: 'Nina closed the window, put on her coat and took an umbrella.' What was the weather probably like? Give one clue.", {"keywords": ["rain", "cold", "umbrella", "coat"]}, "It was probably rainy or cold; the umbrella or coat is the clue.", 2, "pet.reading.inference"),
    ("pet-reading-text-function", "pet", "英语", "四年级-PET", "PET.READING.TEXT_FUNCTION", "阅读文本功能", "判断通知、邀请和说明的交际目的。",
     "Read: 'Please do not leave bicycles outside this door. Use the racks beside the library.' Why is this notice here?", {"keywords": ["tell", "ask", "bicycles", "racks"]}, "It tells cyclists where they should leave their bicycles.", 1, "pet.reading.text_function"),
    # PET listening (stimulus is embedded so the voice agent can read it twice)
    ("pet-listening-gist", "pet", "英语", "四年级-PET", "PET.LISTENING.GIST", "听力整体理解", "听懂短对话的主要话题和结果。",
     "Listen: 'I planned to play tennis, but the court is wet. Let's go to the sports centre and swim instead.' What will the speakers do?", {"keywords": ["swim", "sports centre"]}, "They will swim at the sports centre.", 1, "pet.listening.gist"),
    ("pet-listening-intent", "pet", "英语", "四年级-PET", "PET.LISTENING.SPEAKER_INTENT", "听力说话者意图", "识别请求、建议、提醒或拒绝。",
     "Listen: 'Could you turn the music down? I am trying to finish my homework.' What does the speaker want?", {"keywords": ["music", "down", "quieter"]}, "The speaker wants the music to be quieter.", 1, "pet.listening.speaker_intent"),
    ("pet-listening-note", "pet", "英语", "四年级-PET", "PET.LISTENING.NOTE_COMPLETION", "听力信息记录", "从短通知中记录时间、地点和物品。",
     "Listen: 'The class trip leaves school at eight fifteen on Friday. Meet by the main gate and bring a bottle of water.' Say the time, meeting place and one thing to bring.", {"keywords": ["8:15", "eight fifteen", "main gate", "water"]}, "The time is 8:15, the place is the main gate, and students need water.", 2, "pet.listening.note_completion"),
    # PET writing
    ("pet-writing-organization", "pet", "英语", "四年级-PET", "PET.WRITING.ORGANIZATION", "写作组织衔接", "按开头、要点和结尾组织短文。",
     "Write 35-55 words to tell a friend about a school activity. Include when it happened, what you did, and how you felt. Use at least two linking words.", {"task_points": ["时间", "活动", "感受", "连接"], "keyword_groups": [["yesterday", "last", "on monday", "week"], ["did", "played", "made", "visited", "joined"], ["felt", "happy", "excited", "tired", "enjoyed"], ["and", "but", "because", "then", "so"]], "min_words": 35, "max_words": 55}, "参考量表检查信息顺序和衔接，不等同于PET官方评分。", 1, "pet.writing.organization"),
    ("pet-writing-grammar", "pet", "英语", "四年级-PET", "PET.WRITING.GRAMMAR", "写作句子控制", "用基本时态和完整句表达。",
     "Write 35-55 words about what you did last Sunday and what you will do next Sunday. Give a reason for one activity.", {"task_points": ["过去活动", "未来活动", "原因"], "keyword_groups": [["went", "played", "visited", "watched", "did"], ["will", "going to"], ["because", "so"]], "min_words": 35, "max_words": 55}, "参考量表检查过去与未来表达及完整句，不等同于PET官方评分。", 2, "pet.writing.grammar"),
    ("pet-writing-vocabulary", "pet", "英语", "四年级-PET", "PET.WRITING.VOCABULARY", "写作词汇表达", "使用具体词语表达人物、活动和感受。",
     "Write 35-55 words describing a place you enjoy. Include two details about the place, one activity, and why you like it.", {"task_points": ["地点细节", "活动", "原因"], "keyword_groups": [["big", "small", "quiet", "bright", "beautiful", "near", "trees"], ["read", "play", "walk", "study", "meet"], ["because", "so", "like", "enjoy"]], "min_words": 35, "max_words": 55}, "参考量表检查具体词语和任务完成，不等同于PET官方评分。", 1, "pet.writing.vocabulary"),
    # PET speaking
    ("pet-speaking-interaction", "pet", "英语", "四年级-PET", "PET.SPEAKING.INTERACTION", "口语互动回应", "回应提问、提出建议并追问。",
     "Let's plan a class picnic. Suggest a place and time, say what to bring, then ask me one question.", {"task_points": ["地点时间", "携带物品", "追问"], "keyword_groups": [["park", "school", "saturday", "sunday", "morning", "afternoon"], ["bring", "food", "water", "ball", "blanket"], ["what", "where", "when", "shall", "do you"]], "min_words": 18}, "参考量表只根据转写检查互动任务点，不能判断发音。", 1, "pet.speaking.interaction"),
    ("pet-speaking-extended", "pet", "英语", "四年级-PET", "PET.SPEAKING.EXTENDED_TURN", "口语连续表达", "围绕一个话题连续表达多个相关信息。",
     "Talk for about half a minute about a skill you want to learn. Say what it is, why, how you can practise, and who can help.", {"task_points": ["技能", "原因", "练习", "帮助者"], "keyword_groups": [["learn", "want", "skill"], ["because"], ["practise", "practice", "every day", "week"], ["teacher", "friend", "parent", "mother", "father"]], "min_words": 28}, "参考量表只根据转写检查连续表达内容，不能判断发音或真实流利度。", 2, "pet.speaking.extended_turn"),

    # Competition transfer
    ("oly-transfer", "competition_math", "数学", "四年级竞赛", "OLY.P4.TRANSFER", "陌生情境迁移", "识别表面变化下不变的数量结构。",
     "一辆车每次能运4箱书，运完22箱至少要几次？如果把书换成每盒装4个球、共有22个球，方法哪里相同？", {"keywords": ["6", "除以4", "向上取整", "余数", "相同"]}, "22除以4等于5余2，所以至少6次；两个情境都要把有余数的结果向上取整。", 2, "competition_math.transfer"),

    # Junior Chinese
    ("j3-chinese-literature", "school", "语文", "初三", "J3.CHINESE.LITERATURE", "文学文本鉴赏", "从语言、形象和结构中分析表达效果。",
     "句子‘老屋的灯在风里一闪一闪，却始终没有熄灭’既写景又有何表达作用？请结合‘始终没有熄灭’说明。", {"keywords": ["象征", "希望", "坚持", "坚守", "始终"]}, "‘始终没有熄灭’突出坚守，可象征人物未放弃的希望或信念。", 2, "junior_exam.语文.文学文本鉴赏"),
    ("j3-chinese-classics", "school", "语文", "初三", "J3.CHINESE.CLASSICS", "古诗文理解", "结合语境理解常见文言实词和句意。",
     "‘虽有嘉肴，弗食，不知其旨也’中的‘旨’是什么意思？这句话说明了什么道理？", {"keywords": ["味美", "美味", "实践", "亲自", "学习"]}, "‘旨’是味美；只拥有好东西却不亲自品尝就不知道滋味，强调亲身学习实践。", 1, "junior_exam.语文.古诗文理解"),
    ("j3-chinese-language", "school", "语文", "初三", "J3.CHINESE.LANGUAGE_USE", "语言运用", "根据对象、场合和目的得体表达。",
     "你要在班级群提醒同学明早交社会实践表。请写一句信息完整、语气得体的通知。", {"keywords": ["同学", "明早", "实践表", "请", "谢谢"]}, "示例：请同学们明早到校后把社会实践表交给组长，谢谢大家配合。", 1, "junior_exam.语文.语言运用"),
    ("j3-chinese-writing", "school", "语文", "初三", "J3.CHINESE.WRITING", "写作立意与结构", "围绕中心选择材料并设计结构。",
     "以‘那一步，我终于迈了出去’为题，口述中心立意、开头事件、转折和结尾照应，各用一句话。", {"keywords": ["立意", "开头", "转折", "结尾", "照应"]}, "答案应形成中心明确、事件推进、有转折且结尾照应题目的提纲。", 2, "junior_exam.语文.写作立意与结构"),

    # Junior mathematics
    ("j3-math-number-expression", "school", "数学", "初三", "J3.MATH.NUMBER_EXPRESSION", "数与式", "准确化简代数式并说明条件。",
     "化简 (a²-4)/(a-2)，并说明原式中a不能取什么值。", {"keywords": ["a+2", "a不能等于2", "a≠2", "2"]}, "因式分解为(a-2)(a+2)，约分得a+2，但原式要求a不等于2。", 1, "junior_exam.数学.数与式"),
    ("j3-math-function", "school", "数学", "初三", "J3.MATH.FUNCTION", "函数", "从解析式或图像判断变化关系。",
     "一次函数 y=-2x+5 中，y随x增大怎样变化？图像与y轴交点是多少？", {"keywords": ["减小", "0,5", "(0,5)", "5"]}, "斜率-2小于0，所以y随x增大而减小；与y轴交于(0,5)。", 1, "junior_exam.数学.函数"),
    ("j3-math-geometry-proof", "school", "数学", "初三", "J3.MATH.GEOMETRY_PROOF", "几何证明", "由已知条件组织有依据的证明链。",
     "在等腰三角形ABC中，AB=AC，AD平分∠A并交BC于D。要证明BD=CD，可用哪两个三角形全等？列出三个条件。", {"keywords": ["ABD", "ACD", "AB=AC", "AD", "角BAD", "角CAD", "SAS"]}, "证明△ABD与△ACD全等：AB=AC，AD公共，∠BAD=∠CAD，用SAS得BD=CD。", 2, "junior_exam.数学.几何证明"),
    ("j3-math-statistics", "school", "数学", "初三", "J3.MATH.STATISTICS", "统计与概率", "计算基础统计量并解释随机事件。",
     "袋中有3个红球和2个蓝球，除颜色外相同。随机摸1个球，摸到红球的概率是多少？", {"keywords": ["3/5", "五分之三", "0.6"]}, "共有5个等可能球，其中3个红球，概率为3/5。", 1, "junior_exam.数学.统计与概率"),
    ("j3-math-modeling", "school", "数学", "初三", "J3.MATH.MODELING", "综合建模", "把实际条件转化为方程或函数。",
     "某商品原价100元，连续两次按相同折扣率降价后为81元。设每次降价率为x，只列方程并说明x的取值范围。", {"keywords": ["100(1-x)²=81", "100(1-x)^2=81", "0<x<1", "0≤x≤1"]}, "方程为100(1-x)²=81，降价率应在0到1之间。", 2, "junior_exam.数学.综合建模"),

    # Junior English
    ("j3-english-listening", "school", "英语", "初三", "J3.ENGLISH.LISTENING", "听力", "理解对话主旨、细节和意图。",
     "Listen to the original recording. When and where should they meet?", {"keywords": ["7:30", "seven thirty", "school gate"]}, "They should meet at 7:30 beside the school gate.", 1, "junior_exam.英语.听力"),
    ("j3-english-grammar", "school", "英语", "初三", "J3.ENGLISH.GRAMMAR", "词汇语法", "在语境中选择正确词形和句法。",
     "Complete: If it ___ tomorrow, we will move the sports meeting indoors. Use the correct form of 'rain'.", {"keywords": ["rains"]}, "Use rains: the if-clause uses the present simple for a real future condition.", 1, "junior_exam.英语.词汇语法"),
    ("j3-english-task-reading", "school", "英语", "初三", "J3.ENGLISH.TASK_READING", "任务型阅读", "整合文本信息完成表格或简答。",
     "Read: 'The museum opens at 9:00. Student tickets cost 20 yuan. Photography is allowed without flash.' Give the opening time, student price and photography rule.", {"keywords": ["9:00", "20", "without flash", "no flash"]}, "It opens at 9:00, costs students 20 yuan, and permits photography without flash.", 1, "junior_exam.英语.任务型阅读"),
    ("j3-english-writing", "school", "英语", "初三", "J3.ENGLISH.WRITING", "书面表达", "按任务要求组织连贯、得体的短文。",
     "Write at least 60 words proposing one way to make your school greener. Explain the problem, action and expected result.", {"task_points": ["问题", "行动", "结果"], "keyword_groups": [["problem", "waste", "plastic", "energy", "water"], ["should", "can", "plan", "organize", "reduce", "reuse"], ["result", "so", "therefore", "cleaner", "greener", "save"]], "min_words": 60}, "参考量表检查问题、行动、结果和基本连贯性，不代替学校正式评分。", 2, "junior_exam.英语.书面表达"),

    # Junior physics
    ("j3-physics-motion", "school", "物理", "初三", "J3.PHYSICS.MOTION", "力与运动", "用受力和运动状态关系解释现象。",
     "汽车匀速直线行驶时，水平方向牵引力与阻力是什么关系？为什么？", {"keywords": ["相等", "平衡", "合力为0", "匀速"]}, "匀速直线运动时合力为0，所以牵引力与阻力大小相等、方向相反。", 1, "junior_exam.物理.力与运动"),
    ("j3-physics-density", "school", "物理", "初三", "J3.PHYSICS.DENSITY_PRESSURE", "密度压强浮力", "综合使用密度、压强和浮力概念。",
     "一个物体完全浸没在水中，排开水的体积为0.002立方米。取水的密度1000千克每立方米、g=10，浮力是多少？", {"keywords": ["20N", "20牛", "20"]}, "浮力等于ρgV=1000×10×0.002=20N。", 1, "junior_exam.物理.密度压强浮力"),
    ("j3-physics-energy", "school", "物理", "初三", "J3.PHYSICS.ENERGY", "功和机械能", "计算功、功率并分析机械能转化。",
     "用50牛的力把物体沿力的方向移动4米，用时10秒。做功和功率各是多少？", {"keywords": ["200J", "200焦", "20W", "20瓦"]}, "功W=Fs=200J，功率P=W/t=20W。", 1, "junior_exam.物理.功和机械能"),
    ("j3-physics-electricity", "school", "物理", "初三", "J3.PHYSICS.ELECTRICITY", "电学", "分析基础电路并运用欧姆定律。",
     "一个6欧电阻两端电压为12伏，通过它的电流是多少？若电压不变，电阻增大，电流怎样变化？", {"keywords": ["2A", "2安", "减小"]}, "I=U/R=12/6=2A；电压不变时电阻增大，电流减小。", 1, "junior_exam.物理.电学"),

    # Junior chemistry
    ("j3-chem-particles", "school", "化学", "初三", "J3.CHEM.PARTICLES", "物质构成", "从微粒角度解释物质变化。",
     "水蒸发成为水蒸气时，水分子的种类和分子间隔分别怎样变化？", {"keywords": ["种类不变", "没有变", "间隔变大"]}, "物理变化中水分子种类不变，分子间隔变大。", 1, "junior_exam.化学.物质构成"),
    ("j3-chem-symbols", "school", "化学", "初三", "J3.CHEM.SYMBOLS", "化学用语", "正确使用元素符号、化学式和离子符号。",
     "用化学用语表示：两个氧原子、一个氧分子、一个带两个单位正电荷的镁离子。", {"keywords": ["2O", "O2", "Mg2+", "Mg²+"]}, "依次为2O、O₂、Mg²⁺。", 1, "junior_exam.化学.化学用语"),
    ("j3-chem-substances", "school", "化学", "初三", "J3.CHEM.SUBSTANCES", "常见物质", "根据性质、用途和反应识别常见物质。",
     "实验室检验二氧化碳常把气体通入什么液体？出现什么现象？", {"keywords": ["澄清石灰水", "石灰水", "浑浊"]}, "通入澄清石灰水，石灰水变浑浊。", 1, "junior_exam.化学.常见物质"),
    ("j3-chem-experiment", "school", "化学", "初三", "J3.CHEM.EXPERIMENT_CALC", "实验与计算", "依据实验数据进行守恒和定量计算。",
     "10克碳酸钙充分反应后理论上生成4.4克二氧化碳。若实际收集3.96克，收集率是多少？", {"keywords": ["90%", "百分之九十", "0.9"]}, "收集率=3.96÷4.4×100%=90%。", 2, "junior_exam.化学.实验与计算"),

    # Junior civics
    ("j3-civics-concepts", "school", "道德与法治", "初三", "J3.CIVICS.CONCEPTS", "核心概念", "准确理解权利、义务、规则和责任等概念。",
     "为什么公民享有权利的同时也必须履行义务？请说明权利与义务的关系。", {"keywords": ["统一", "相互依存", "相互促进", "既是权利", "也是义务"]}, "权利与义务相统一、相互依存相互促进，公民既是权利主体也是义务主体。", 1, "junior_exam.道德与法治.核心概念"),
    ("j3-civics-argument", "school", "道德与法治", "初三", "J3.CIVICS.ARGUMENT", "观点论证", "用概念、事实和因果链论证观点。",
     "有人说‘网络发言是我的自由，想说什么都可以’。请用权利边界的观点作出判断并说明理由。", {"keywords": ["错误", "边界", "法律", "他人权利", "义务"]}, "观点错误；自由有法律和他人权利的边界，行使权利不能损害国家、社会和他人合法权益。", 2, "junior_exam.道德与法治.观点论证"),
    ("j3-civics-practice", "school", "道德与法治", "初三", "J3.CIVICS.PRACTICE", "实践应用", "把价值判断转化为具体可执行行动。",
     "发现社区无障碍通道常被车辆占用，作为中学生可以采取哪两个合法、可行的行动？", {"keywords": ["劝阻", "物业", "社区", "反映", "建议", "证据"]}, "可礼貌劝阻或记录情况，向物业、社区等反映并提出规范停车建议，不能采取过激行为。", 1, "junior_exam.道德与法治.实践应用"),

    # Junior history
    ("j3-history-timeline", "school", "历史", "初三", "J3.HISTORY.TIMELINE", "时空线索", "把史事放入正确时间和空间序列。",
     "请按时间先后排列：鸦片战争、辛亥革命、五四运动，并写出最早事件的大致年代。", {"keywords": ["鸦片战争", "辛亥革命", "五四运动", "1840"]}, "顺序是鸦片战争、辛亥革命、五四运动；鸦片战争开始于1840年。", 1, "junior_exam.历史.时空线索"),
    ("j3-history-facts", "school", "历史", "初三", "J3.HISTORY.FACTS", "史实理解", "准确说明重要史事的基本内容。",
     "辛亥革命在政治制度方面最重要的直接成果是什么？", {"keywords": ["清朝", "君主专制", "共和", "中华民国"]}, "辛亥革命推翻清朝统治，结束君主专制制度，建立中华民国。", 1, "junior_exam.历史.史实理解"),
    ("j3-history-source", "school", "历史", "初三", "J3.HISTORY.SOURCE", "材料解释", "区分史料信息与基于证据的解释。",
     "一份工厂记录显示某年女工人数明显增加。仅凭这条材料，能否直接断言所有女性社会地位都提高了？为什么？", {"keywords": ["不能", "证据不足", "局限", "一份", "所有"]}, "不能；单一工厂记录只能说明局部就业变化，不能直接概括所有女性的社会地位。", 2, "junior_exam.历史.材料解释"),
    ("j3-history-compare", "school", "历史", "初三", "J3.HISTORY.COMPARE", "比较与评价", "围绕同一维度比较史事并作有限评价。",
     "比较两次改革时，为什么要先确定同一个比较维度？请举一个可用维度。", {"keywords": ["可比", "同一", "背景", "内容", "影响", "结果"]}, "同一维度才能避免把不同层面混在一起；可比较背景、措施、结果或影响。", 1, "junior_exam.历史.比较与评价"),
)


SPECIALIZED_NODES = tuple(
    (
        f"node-{slug}", track, subject, grade_band, code, title, description,
    )
    for slug, track, subject, grade_band, code, title, description, *_ in SPECIALIZED_DIAGNOSTICS
)

SPECIALIZED_ITEMS = tuple(
    (
        f"diag-{slug}-baseline", f"node-{slug}", prompt,
        "writing" if domain_id in {"pet.writing.organization", "pet.writing.grammar", "pet.writing.vocabulary", "junior_exam.英语.书面表达"} else
        "listening_text" if domain_id == 'junior_exam.英语.听力' else
        "speaking" if domain_id.startswith("pet.speaking.") else "text",
        expected, explanation, difficulty,
    )
    for slug, _track, _subject, _grade_band, _code, _title, _description,
        prompt, expected, explanation, difficulty, domain_id in SPECIALIZED_DIAGNOSTICS
)

# Original script stays in source storage; it is never a visible question or TTS fallback.
SCHOOL_LISTENING_STIMULI = {'diag-j3-english-listening-baseline':
    "The bus leaves at 7:40, but we should meet ten minutes earlier beside the school gate."}
LEGACY_SCHOOL_LISTENING_PROMPT = (
    "Listen: 'The bus leaves at 7:40, but we should meet ten minutes earlier beside the school gate.' "
    'When and where should they meet?')

SPECIALIZED_DOMAIN_HINTS = {
    f"node-{slug}": domain_id
    for slug, _track, _subject, _grade_band, _code, _title, _description,
        _prompt, _expected, _explanation, _difficulty, domain_id in SPECIALIZED_DIAGNOSTICS
}
