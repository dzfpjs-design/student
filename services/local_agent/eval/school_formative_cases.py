"""Developer-authored semantic probes, not child data or calibration labels.

Freeze expectations before requests. Heldout tasks are separate from regression
tasks; these small probes never authorize scores or certify a school subject.
"""
from home_study_agent.school_oral_catalog import TASKS
from home_study_agent.open_response_profile import profile_for

VERSION = 'school_formative_probes_v1'
SUPPORTED_CRITERIA = {
    'grouping-correct':(0,1,2),'grouping-misread':(),'grouping-bare':(),
    'roots-correct':(0,1,2),'roots-contradiction':(1,),
    'chinese-alternative':(0,1,2),'physics-confounded':(),'physics-controlled':(0,1,2),
    'price-correct':(0,1,2),'price-cancelled-errors':(),
    'english-correct':(0,1,2),'english-invented':(),
}
# id, original task slug, original response, required passing/failing dimensions
REGRESSION = (
    ('grouping-correct','p4-math-grouping',
     '我选小明的。4乘12是原来48支，两个组各9支要送18支，所以48减18剩30支。小青只减了一组。30加18还是48支，检查对了。',
     ('understanding','reasoning','accuracy','reflection'),()),
    ('grouping-misread','p4-math-grouping',
     '我选小青。原来48支，两个小组一共只送9支，所以48减9剩39支。检查就是39加9等于48，所以肯定对。',
     (),('understanding','reasoning','accuracy','reflection')),
    ('grouping-bare','p4-math-grouping','剩30支。',(),('reasoning','reflection')),
    ('roots-correct','j3-math-roots',
     '他的结论不对。a等于1，b等于负6，c等于5，判别式是负6的平方减去4乘1乘5，等于16。因为16大于零，所以有两个不相等的实数根。一次项是偶数不能判断两根相等。',
     ('answer','method','reasoning'),()),
    ('roots-contradiction','j3-math-roots',
     'a是1，b是负6，c是5，判别式36减20等于16。16大于零，所以两个实数根相等，小林说得对。',
     (),('answer','reasoning')),
    ('chinese-alternative','j3-chinese-action',
     '揉掉通知说明我对落选很难过。哥哥没有追问名次，不想拿结果再让我难堪。他还一张张铺好设计图，约我周末继续搭桥，说明他没有否定我的努力，而是愿意陪我继续试。',
     ('task','evidence','reasoning','expression'),()),
    ('physics-confounded','j3-physics-confound',
     '能直接检验，因为两根都是镍铬丝而且温度一样。乙长度和面积都变成两倍，电阻一样，所以已经证明长度对电阻没有影响，不需要更换。',
     (),('claim','reasoning','limitations')),
    ('physics-controlled','j3-physics-confound',
     '不能直接检验。乙的长度和横截面积同时改变，结果不能单独归因于长度。应换成同温度的镍铬丝，横截面积仍是0.2平方毫米，长度可为40厘米，和甲比较。现在这组材料不能单独说明长度的作用。',
     ('claim','evidence','reasoning','limitations'),()),
)
HELDOUT = (
    ('price-correct','p4-math-constraint',
     '20元是付出的钱，要先减掉找回的2元，实际花18元。18除以3，每本6元。3乘6再加2等于20，和付的钱一致。',
     ('understanding','reasoning','accuracy','reflection'),()),
    ('price-cancelled-errors','p4-math-constraint',
     '直接20除以3等于6，所以每本6元，找回2元不用管。',(),('reasoning','accuracy','reflection')),
    ('english-correct','p4-english-reason',
     'Her brother has no cup, so Lily gives one cup to him. It says gives one, not two. They can each have one to drink water together.',
     ('understanding','evidence','reasoning','expression'),()),
    ('english-invented','p4-english-reason',
     'She gives him both cups because he is sick and needs two cups of medicine. The story tells us he is sick.',
     (),('understanding','evidence','reasoning')),
)


def cases(suite):
    definitions = {'regression':REGRESSION,'heldout':HELDOUT}[suite]
    tasks = {task[0]:task for task in TASKS}
    results = []
    for name,slug,response,passed,failed in definitions:
        _,_,subject,grade,family,question,points,_ = tasks[slug]
        profile=profile_for(subject,task_family=family,response_mode='spoken_explanation',
            **({'grade':grade,'source_schema':'reviewed_open_response_source_v1'} if grade=='四年级' else {}))
        results.append(dict(case_id=name,task_slug=slug,subject=subject,grade=grade,
            payload=dict(question=question,response=response,
                rubric=dict(dimensions=profile['dimensions'],task_points=list(points))),
            expected_pass=list(passed),expected_fail=list(failed)))
    return results


def compare(case,evaluation):
    scores={d['dimension_id']:d['score'] for d in evaluation['rubric_dimensions']}
    false_pass=[key for key in case['expected_fail'] if scores[key]>=2]
    false_fail=[key for key in case['expected_pass'] if scores[key]<2]
    return dict(passed=not false_pass and not false_fail,false_pass=false_pass,false_fail=false_fail,scores=scores)


def compare_review(case,evaluation):
    score=compare(case,evaluation)
    supported={c['criterion_index'] for c in evaluation['criterion_review']['checks'] if c['status']=='supported'}
    expected=set(SUPPORTED_CRITERIA[case['case_id']])
    return {**score,'passed':score['passed'] and supported==expected,
        'false_support':sorted(supported-expected),'missed_support':sorted(expected-supported)}
