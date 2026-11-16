"""Authored, self-contained reasoning tasks, not exam or curriculum certification.

IDs/content are immutable within v1. Corrections require a new item/version so
previous responses are never silently reassessed against a different question.
"""

VERSION = 'school_oral_authored_v1'

# slug, node, subject, grade, task family, prompt, criteria, teaching explanation
TASKS = (
    ('j3-math-roots', 'node-j3-math-discriminant', '数学', '初三', 'mathematical_process',
     '小林认为方程 x²-6x+5=0 有两个相等的实数根，因为一次项系数是偶数。请口头判断他的说法，说明如何检验；不必求出两个根。',
     ('识别a=1、b=-6、c=5，不能用一次项奇偶性判断根。', '计算b²-4ac=36-20=16。', '由判别式大于0推出两个不相等实数根，并指出原判断错误。'),
     '先问“根的个数与哪个量的符号有关”，再请孩子自己代入。判别式16大于0，有两个不相等实数根。偶数一次项不是判据。若孩子只说有两个根，追问相等与否及依据；不给出结论后立即让孩子复述来充当独立检查。'),
    ('j3-math-parameter', 'node-j3-math-discriminant', '数学', '初三', 'mathematical_process',
     '方程 x²+2x+k=0 有实数根。小夏说“k只能等于1”。请说明k的范围，并解释边界值与范围内其他值的区别。',
     ('判别式为4-4k，存在实数根要求其大于或等于0。', '解得k小于或等于1，不是只取1。', 'k=1为两个相等实数根，k<1为两个不相等实数根。'),
     '从“有实数根”翻译成判别式非负，而不是判别式等于零。4-4k≥0得到k≤1。分别讨论等号与严格小于；可追问k=0是否满足以检验“只能等于1”的说法。'),
    ('j3-chinese-action', 'node-j3-chinese-reading-evidence', '语文', '初三', 'textual_interpretation',
     '读自编短文：“我把落选的通知揉成一团。哥哥没有问名次，只把我散在地上的设计图逐张摊平，说：周末再试试这座桥怎么搭。”哥哥的做法怎样回应了“我”的处境？请结合两个具体细节说明，不补写人物经历。',
     ('从揉通知识别失落或受挫处境，不捏造其他经历。', '结合没有问名次、逐张摊平设计图、邀请再试等至少两个细节。', '说明行动如何传达理解、尊重努力或鼓励继续实践，允许有文本支持的不同表达。'),
     '先让孩子指出文本里实际发生的动作，再把“没有追问名次”和“整理图纸、邀请再试”与失落联系。可以理解为不以结果责备、仍重视努力并提供支持。仅答“哥哥很好”缺少依据；不能把答案限制成必须出现某几个词。'),
    ('j3-chinese-evidence', 'node-j3-chinese-reading-evidence', '语文', '初三', 'textual_interpretation',
     '读自编短文：“志愿者说，只要把修好的旧书送回流动书架，就能让它再陪一个读者。我原想买新书，听完便取出胶带，把脱落的书页一页页贴好。”有人概括为“我再也不买新书了”。这个概括是否得到原文支持？请给出更准确的概括和依据。',
     ('指出再也不买新书扩大了原文信息。', '引用听完后取胶带修书的转变，区分本次行为与永久决定。', '概括对修复再利用或共享价值的理解及行动，不强求唯一措辞。'),
     '比较“这一次做了什么”和“以后永远怎样”。文本只表明受到提醒后开始修书、使旧书继续被利用，没有永不买书的证据。请孩子删除概括中无法在文中找到支持的词。'),
    ('j3-english-purpose', 'node-j3-english-reading-purpose', '英语', '初三', 'textual_interpretation',
     'Read this original notice: "Garden volunteers: Saturday\'s planting will begin at 10, not 9, because the soil delivery is late. Wear old shoes. We will provide gloves." Explain the purpose of the notice and distinguish what volunteers should bring from what is provided.',
     ('说明通知更改种植活动开始时间并交代准备事项。', '新时间10点而非9点；迟到的是泥土配送，不据此推断活动取消。', '穿旧鞋，手套由组织方提供；以原文为据，允许用自己的话解释。'),
     'Ask what changed, what the reader must do, and what the organiser provides. Planting still happens, at 10. Volunteers wear old shoes; they need not infer that gloves must be brought. Check understanding separately from small language errors.'),
    ('j3-english-limit', 'node-j3-english-reading-purpose', '英语', '初三', 'textual_interpretation',
     'Read this original message: "To visitors: The east entrance is closed until Friday. Please use the west gate. Wheelchair users can ask the office for help with the temporary ramp." Does this mean the whole building is closed? Explain the message and what two groups of visitors can do.',
     ('区分关闭东入口与关闭整栋楼，否定无根据的扩大解释。', '一般访客使用西门。', '轮椅使用者可向办公室寻求临时坡道帮助，引用相关信息而不编造开放时间。'),
     'Locate the exact noun modified by closed: the east entrance. The instruction to use the west gate supports continued access. Separate the general direction from the optional accessibility assistance; do not infer that every visitor must report to the office.'),
    ('j3-physics-confound', 'node-j3-physics-experiment-reasoning', '物理', '初三', 'scientific_explanation',
     '两根镍铬合金丝温度相同：甲长20厘米、横截面积0.2平方毫米，乙长40厘米、横截面积0.4平方毫米。小宁想只比较电阻与长度的关系。这组选材能直接检验吗？请说明问题，并描述应怎样更换乙来控制变量。',
     ('长度与横截面积同时改变，不能把结果单独归因于长度。', '乙应与甲材料、横截面积及温度相同，长度不同。', '说明测量比较电阻而非仅凭长度猜结论，不编造实测数据。'),
     '请孩子逐项列出甲乙的共同点和不同点。这里有两个变量同时变化。可把乙换成同材料、同温度、截面积0.2平方毫米而长度40厘米的合金丝，再测电阻。不能从这道选材题编出实验已证明的结论。'),
    ('j3-physics-data', 'node-j3-physics-experiment-reasoning', '物理', '初三', 'scientific_explanation',
     '一次控制材料、横截面积和温度相同的导线实验记录为：长度10厘米时电阻2欧，20厘米时4欧，30厘米时6欧。请概括记录支持的关系，并评价“任何导线只要长30厘米，电阻就一定是6欧”这句话。',
     ('在本实验控制条件下，给定三组数据电阻随长度成正比。', '用两组或比值作为依据，如长度倍增时电阻倍增。', '结论受材料、横截面积和温度等条件限制，不能推广到任意导线。'),
     '先比较长度和电阻的倍数或R/L，再强调相同的控制条件。三组记录支持本条件下的比例关系；另一种材料或另一截面积的导线可能不是6欧。观察到的规律与无条件断言要分开。'),
    ('j3-chem-methane', 'node-j3-chem-equation', '化学', '初三', 'scientific_explanation',
     '题给反应事实：甲烷CH₄在足量氧气中点燃，生成CO₂和H₂O。请口头说出配平后的化学方程式、各物质系数比，并按元素核对原子数。这里只讨论题给反应，不进行实验。',
     ('依据题给产物写出CH₄+2O₂→CO₂+2H₂O，说明点燃条件。', '系数比1:2:1:2；不通过改变化学式下标配平。', '左右C各1、H各4、O各4，说明原子守恒。'),
     '先定化学式，再看碳、氢，最后数氧。一个CH₄对应一个CO₂和两个H₂O，右侧共4个氧，故左侧2O₂。用元素逐项核对解释配平，而不是只背系数。'),
    ('j3-chem-aluminium', 'node-j3-chem-equation', '化学', '初三', 'scientific_explanation',
     '已知铝Al在氧气O₂中点燃生成氧化铝Al₂O₃。小冬为配平把产物改写成AlO。为什么不可以？请给出正确的系数比，并核对两种元素。这里只做纸面分析。',
     ('产物化学式由反应事实决定，不能为配平更改为AlO。', '正确方程式4Al+3O₂→2Al₂O₃并保留点燃条件，系数比4:3:2。', '两侧Al各4、O各6，区分系数与下标。'),
     '追问“改变下标后还是题给的物质吗”。氧原子取2与3的公倍数6，得到3O₂与2Al₂O₃，再补4Al。配平只调整系数，不改变物质身份。'),
    ('j3-civics-procedure', 'node-j3-civics-material', '道德与法治', '初三', 'civic_argument',
     '自编班级协商材料：班级约定活动方案须先征求每组意见，再表决。组长没询问两名缺席同学就说“我们组全体赞成”。请依据材料区分事实与未经核实的表态，提出既能推进活动又尊重参与的两步建议。',
     ('指出没有征求两名同学意见，不能宣称全体赞成。', '联系题给先征求再表决的程序与尊重参与，不臆测缺席者一定反对。', '提出补充征求并如实记录意见、按约定表决等可执行步骤。'),
     '先区分不知道意见与意见相反。可以联系缺席同学、说明方案并记录回应，再按规则表决；若暂时联系不上，应如实标记而非代替表态。这里依据的是题给协商规则，不是对现实纠纷作法律裁决。'),
    ('j3-civics-facts', 'node-j3-civics-material', '道德与法治', '初三', 'civic_argument',
     '自编材料：班级规定借走共用剪刀要登记。有人看到剪刀不见，就在群里说“最后离开的小周偷走了”。登记本尚未查看，小周也未被询问。请指出现有证据能说明什么、不能说明什么，并建议怎样处理。',
     ('目前只能说剪刀未在原处或待查，不能凭最后离开认定偷走。', '建议查登记、询问相关同学、寻找物品，区分核查与公开指责。', '建议停止传播未经核实的指责，若已误传应澄清；不作有罪推断。'),
     '把“看到的事”与“推测的原因”列开。先查借用登记并询问，不给同学贴标签；若公开信息有误要更正。可追问还有哪些可能原因，练习有依据地判断而不是猜测动机。'),
    ('j3-history-sequence', 'node-j3-history-causality', '历史', '初三', 'historical_explanation',
     '以下是练习用的虚构历史材料，不是真实史事：某城多年运输不便；一次洪水冲断旧桥后，议事会决定建新桥；新桥建成后商队通行次数增加。请区分长期背景、直接促发因素、决策和后续变化，并说明仅凭这段材料能否断定居民收入都提高了。',
     ('多年运输不便是背景，洪水断桥是直接促发因素，建桥是决策。', '商队通行增加是材料记载的后续变化，不与背景混淆。', '没有居民收入资料，不能断定人人增收；时序也不单独证明所有因果。'),
     '按发生前、触发时、采取行动、随后四栏归类。直接记载的是通行变化，没有每位居民收入。引导孩子说明证据还缺哪一类，而不是补一个听起来合理的结论。'),
    ('j3-history-explanations', 'node-j3-history-causality', '历史', '初三', 'historical_explanation',
     '自编虚构历史材料：一地多年税收不足，同时有多年关于改税的讨论；某年灾害使收入骤减，官员随即提出新方案。甲说“改革只因灾害”，乙说“灾害与改革无关”。请比较两种说法的问题，并指出还需什么资料检验新方案的影响。',
     ('长期财政困难和讨论是背景，灾害可以是促发因素，不能只保留其中一层。', '材料有时间联系，但不能夸大成唯一原因，也不能武断排除灾害。', '应查实施情况、前后收支及其他变化等资料，不能把提出方案等同于改革成功。'),
     '分别圈出多年和随即，讨论不同层次的原因。再问“提了方案等于执行了吗”。检验影响需要实施和结果资料，不能从提案直接推断成功。'),
    ('p4-math-grouping', 'node-p4-open-math-reasoning', '数学', '四年级', 'mathematical_process',
     '有4盒彩笔，每盒12支，送给两个小组各9支。小青列式4×12-9，小明列式4×12-2×9。请口头判断谁的算式符合题意，解释每一部分，再算剩余支数。',
     ('4×12表示原有48支，两个小组各9支共送出18支。', '选择4×12-2×9并解释为什么不能只减9。', '剩30支，单位正确，可用30+18=48核对。'),
     '先问送给几个小组、每组多少。用两组各9支帮助区分一个9与两个9。48-18=30支，可用剩余加送出核对总数。给出提示后的回答只说明在帮助下理解。'),
    ('p4-math-constraint', 'node-p4-open-math-reasoning', '数学', '四年级', 'mathematical_process',
     '买3本同样的练习本付出20元，找回2元。小雨算20÷3，认为每本约6元多。请说明哪里漏用了条件，算出每本价格并检查。',
     ('20元是付出的钱，不是实际花费；需要先减找回的2元。', '实际18元，18÷3=6元一本。', '用3×6+2=20或等价关系核对，区分总价和单价。'),
     '让孩子把付出、花掉、找回分别说清。买本子共花18元，再平均分到3本。核对时把花费与找回的钱合起来，而不是用近似结果掩盖漏掉条件。'),
    ('p4-chinese-action', 'node-p4-open-chinese-reading', '语文', '四年级', 'textual_interpretation',
     '读自编短文：“放学时，雨越下越大。小禾走到门口，又折回教室，把窗台上还没干的画搬到书架上，才撑开伞。”小禾为什么折回？哪两个细节帮助你理解？',
     ('联系雨越下越大与窗台上未干的画，理解保护画的意图。', '依据折回、搬到书架等实际动作，不编造老师布置。', '用连贯的话连接细节与理由，允许有依据的不同说法。'),
     '先找小禾做了什么，再看当时的天气和画的位置。她把画搬到书架，可以避免窗边的雨打湿画。文中没有说老师命令她，不需要补写。'),
    ('p4-chinese-main', 'node-p4-open-chinese-reading', '语文', '四年级', 'textual_interpretation',
     '读自编短文：“小凯第一次折纸船，船底歪了。他拆开对齐折痕，再试一次。第二只放入水盆，稳稳浮着。”小亮说这段话主要讲水盆很大。你同意吗？请用文中事情说明主要写什么。',
     ('指出水盆很大在文中没有依据，也不是主要事件。', '按第一次失败、调整折痕、第二次成功概括。', '可以理解为调整方法继续尝试，但不把一次成功说成从不失败。'),
     '让孩子找前后发生变化的对象，是纸船的折法和结果，不是水盆。用“先……接着……最后……”概括，再说尝试和调整的作用。'),
    ('p4-english-location', 'node-p4-open-english-reading', '英语', '四年级', 'textual_interpretation',
     'Read this original note: "Ben, your cap is not in the bag. It is on the chair next to the door. Please bring it to the park. Mum." Where is the cap now, and what should Ben do? Explain using the note.',
     ('帽子现在在门旁的椅子上，而不是包里或公园。', 'Ben应把帽子带到公园，区分现在的位置与目的地。', '回答对应原文，不要求逐字背诵，理解与英语表达错误分别看待。'),
     'Ask “Where is it now?” before “Where should it go?”. The chair is the current location; the park is the destination. Let the child point out the words supporting each answer, using Chinese to clarify meaning if needed.'),
    ('p4-english-reason', 'node-p4-open-english-reading', '英语', '四年级', 'textual_interpretation',
     'Read this original story: "Lily takes two cups to the garden. Her little brother has no cup, so she gives one to him. They drink water together." Why does Lily give a cup to her brother? Does the story say she gives him both cups?',
     ('因为弟弟没有杯子，引用has no cup的意思。', '只给一个，不是两个；依据gives one。', '联系两人一起喝水理解行为，不补造弟弟生病等原因。'),
     'Find the reason after brother and check the number after gives. She gives one because he has no cup. Keep the explanation tied to this short story; do not require a memorised moral.'),
)


def items():
    from .open_response_profile import profile_for
    result = []
    for slug, node, subject, grade, family, prompt, points, explanation in TASKS:
        profile = profile_for(subject, task_family=family, response_mode='spoken_explanation',
            **({'grade':grade, 'source_schema':'reviewed_open_response_source_v1'} if grade=='四年级' else {}))
        rubric = dict(task_points=list(points), review_profile=profile['profile_id'],
            profile_version=profile['version'], task_family=family, response_mode='spoken_explanation',
            context_requirement='self_contained')
        result.append(('diag-authored-oral-v1-'+slug, node, prompt, 'open_response', rubric,
                       explanation, 2 if grade=='初三' else 1, None, None, 'open_response_rubric'))
    return tuple(result)
