"""Reference-only school response profiles; no scoring or provenance authority."""
import json

from .learning_rubric import SCORE_ANCHORS, rubric_dimensions

VERSION = 'school_open_response_profiles_v1'
VISUAL_VERSION = 'school_visual_response_profiles_v1'
PRIMARY_VERSION = 'school_primary_open_response_profiles_v1'
PRIMARY_CRITERIA = {
    'mathematical_process': (
        ('understanding','说清本题已知条件和要解决的问题，不补造条件。'),
        ('reasoning','用分步计算、比较或举例说明理由，解释每一步依据。'),
        ('accuracy','核对计算、数量关系和单位，不只凭最后数字判断过程。'),
        ('reflection','用原题条件或等价关系检查答案；确有疑点时说明还需核实之处，不要求正确验算后额外编造疑点。')),
    'textual_interpretation': (
        ('understanding','回应本题所问，准确理解原文人物、事件或信息，不要求超出年级的术语。'),
        ('evidence','引用或转述原文中的具体信息，不把自己的猜测当成原文事实。'),
        ('reasoning','说明所选原文信息怎样支持回答，不以出现关键词代替解释。'),
        ('expression','用清楚连贯的语句表达理解；不由语言形式单独推断阅读理解。')),
    'communicative_writing': (
        ('task_response','围绕题目和写作目的表达，人物、事件或对象清楚。'),
        ('evidence','用具体细节、事例或理由充实表达，不以字数或套话代替内容。'),
        ('organization','安排合适顺序，使句子和段落之间的意思连贯。'),
        ('language','用符合表达目的的词句让读者看明白，区分局部笔误与意思不清。')),
}
FAMILIES = {
    '数学':('mathematical_process',),
    '语文':('textual_interpretation','communicative_writing'),
    '英语':('textual_interpretation','communicative_writing'),
    '物理':('scientific_explanation',),
    '化学':('scientific_explanation',),
    '生物':('scientific_explanation',),
    '历史':('historical_explanation',),
    '地理':('geographic_explanation',),
    '道德与法治':('civic_argument',),
}
SUBJECT_CODES = dict(zip(FAMILIES,('math','chinese','english','physics','chemistry','biology','history','geography','civics')))
CRITERIA = {
    '物理':(
        ('claim','结论回答本题，物理量、单位和适用条件明确，不由最终数字相同推断过程正确。'),
        ('evidence','区分控制变量、观测量及实际观察，引用题给事实，不编造实验或测量。'),
        ('reasoning','说明物理关系如何连接观察与结论，核对方向、量纲及条件。'),
        ('limitations','区分已经支持的结论和仍未检验的假设，不把相关关系直接当作因果。')),
    '化学':(
        ('claim','准确回应反应现象、物质性质或变化要求，不以出现物质名称代替解释。'),
        ('evidence','根据题给现象和数据作判断，不虚构实验步骤或产物。'),
        ('reasoning','用适当的物质变化、微粒模型或守恒关系连接依据与结论，核对反应条件。'),
        ('limitations','区分可观察现象与推断，说明结论所依赖的条件或尚缺检验。')),
    '生物':(
        ('claim','回应本题的生命现象或生物学问题，明确讨论层次与对象。'),
        ('evidence','使用题给观察、对照或数据支持解释，不把未观察到的过程写成已测事实。'),
        ('reasoning','说明结构、功能、环境或调节机制之间的联系，不用目的性叙述代替机制。'),
        ('limitations','识别对照、样本或情境限制，不从有限观察推广到所有个体。')),
    '历史':(
        ('claim','核心判断回应题意，时间顺序、事件和主体不混淆。'),
        ('evidence','准确使用题给史料或已明确的史实，区分材料记述与自己的推断。'),
        ('causality','区分背景、直接原因、事件和影响，并解释联系，不只罗列年代或结论。'),
        ('limitations','说明史料视角与解释边界，避免以单一材料证明所有原因或唯一结果。')),
    '地理':(
        ('claim','回应题目的区域、尺度或空间分布问题，方向、位置与量值表述清楚。'),
        ('evidence','使用题给地图、图表或事实；没有实际材料时不捏造读图结果。'),
        ('reasoning','解释自然与人文因素的作用及联系，不把地名或术语的堆积当作因果过程。'),
        ('limitations','考虑区域、时间或尺度条件，不将一个地区的解释无条件推广。')),
    '道德与法治':(
        ('claim','针对材料中的问题提出清楚观点，区分事实判断与规范性建议。'),
        ('evidence','准确引用材料行为和情境，不虚构人物动机或额外经历。'),
        ('reasoning','说明相关概念如何支持判断，考虑权利、责任、程序和适用条件。'),
        ('action','建议回应具体问题且可实行；不依据回答推断孩子人格、心理状态或政治倾向。')),
}


def profile_for(subject, *, task_family, response_mode, grade=None, source_schema=None):
    """Return independent mutable descriptors, never a graded response or grant."""
    if source_schema not in (None, 'reviewed_open_response_source_v1',
                             'reviewed_open_response_source_v2', 'reviewed_open_response_source_v3'):
        raise ValueError('OPEN_RESPONSE_PROFILE_SCOPE_INVALID')
    primary = grade == '四年级' and source_schema in {'reviewed_open_response_source_v1', 'reviewed_open_response_source_v2'}
    if grade == '四年级' and not primary:
        if (subject not in {'数学','科学'} or task_family != ('mathematical_process' if subject=='数学' else 'scientific_explanation')
                or response_mode not in {'written','spoken_explanation'}):
            raise ValueError('OPEN_RESPONSE_PROFILE_SCOPE_INVALID')
        criteria = (('given','说清图中实际标记的数、形状或实验条件；不把看起来像的关系当成已给条件。'),
            ('evidence','指出所用的图中标记或题给观察；区分实际观察与自己的猜测。'),
            ('reasoning','用适合本题的分步计算或比较说明理由，能说清每一步依据。'),
            ('check','回到原图核对结论；看不清或条件不足时说明还需要什么。'))
        return {'profile_id':'school_p4_visual_'+('math' if subject=='数学' else 'science')+'_v1',
            'version':VISUAL_VERSION,'grade':grade,'subject':subject,'task_family':task_family,'response_mode':response_mode,
            'dimensions':[{'dimension_id':key,'criterion':criterion,'max_score':3,'pass_score':2,'score_anchors':dict(SCORE_ANCHORS)} for key,criterion in criteria],
            'score_status':'reference','qualification_status':'needs_review','human_review_required_for_formative_feedback':False,
            'original_requirement':'written_original' if response_mode=='written' else 'original_answer_voice',
            'automatic_certification':False,'mastery_claim_allowed':False,'local_only':True,'cloud_transmission_allowed':False}
    if not primary and grade not in {None,'初三'}:
        raise ValueError('OPEN_RESPONSE_PROFILE_SCOPE_INVALID')
    if (not all(isinstance(value,str) for value in (subject,task_family,response_mode))
            or subject not in FAMILIES or task_family not in FAMILIES[subject]
            or response_mode not in {'written','spoken_explanation'}):
        raise ValueError('OPEN_RESPONSE_PROFILE_SCOPE_INVALID')
    if primary:
        if (subject, task_family) not in {('数学','mathematical_process'), ('语文','textual_interpretation'),
                ('语文','communicative_writing'), ('英语','textual_interpretation')}:
            raise ValueError('OPEN_RESPONSE_PROFILE_SCOPE_INVALID')
        dimensions = [{'dimension_id':key,'criterion':criterion,'max_score':3,'pass_score':2,
                       'score_anchors':dict(SCORE_ANCHORS)} for key,criterion in PRIMARY_CRITERIA[task_family]]
    elif task_family=='textual_interpretation':
        dimensions = rubric_dimensions('语文')
    elif subject in {'数学','语文','英语'}:
        dimensions = rubric_dimensions(subject)
        if subject=='语文' and task_family=='communicative_writing':
            next(d for d in dimensions if d['dimension_id']=='evidence')['criterion'] = (
                '根据写作目的选择具体细节、事例或理由支撑表达；不得把字数或套用词语当作内容充分。')
    else:
        dimensions = [{'dimension_id':key,'criterion':criterion,'max_score':3,'pass_score':2,
                       'score_anchors':dict(SCORE_ANCHORS)} for key,criterion in CRITERIA[subject]]
    written = response_mode=='written'
    return {**({'grade':grade} if grade else {}),'profile_id':('school_p4_open_' if primary else 'school_open_')+SUBJECT_CODES[subject]+'_'+task_family+'_v1',
        'version':PRIMARY_VERSION if primary else VERSION,'subject':subject,'task_family':task_family,'response_mode':response_mode,
        'dimensions':dimensions,'score_status':'reference','qualification_status':'needs_review',
        'human_review_required_for_formative_feedback':False,
        'original_requirement':'written_original' if written else 'original_answer_voice',
        'required_origin_fields':(['original_artifact_id','attempt_binding','exact_written_response_hash'] if written else
            ['source_voice_input_id','voice_capture_binding','exact_confirmed_transcript']),
        'written_authorship_allowed':written,'acoustic_assessment_allowed':False,
        'automatic_certification':False,'mastery_claim_allowed':False,
        'local_only':True,'cloud_transmission_allowed':False}


def profile_for_item(item, node):
    """After validate_item, resolve the exact stored contract, not subject alone."""
    def require(value):
        if not value:
            raise ValueError('OPEN_RESPONSE_ITEM_PROFILE_CHANGED')
    require(item.get('status')=='active' and node.get('status')=='active'
        and node.get('track')=='school' and node.get('grade_band') in {'初三','四年级'}
        and item.get('knowledge_node_id')==node.get('knowledge_node_id')
        and item.get('answer_type')=='open_response' and item.get('assessment_mode')=='open_response_rubric')
    def unique(pairs):
        result = {}
        for key,value in pairs:
            require(key not in result)
            result[key] = value
        return result
    try:
        rubric = json.loads(item['expected_answer_json'],object_pairs_hook=unique)
    except (ValueError,TypeError,KeyError) as error:
        raise ValueError('OPEN_RESPONSE_ITEM_PROFILE_CHANGED') from error
    visual = isinstance(rubric,dict) and rubric.get('material_schema')=='reviewed_open_response_source_v3'
    material = isinstance(rubric,dict) and rubric.get('material_schema') in {'reviewed_open_response_source_v2','reviewed_open_response_source_v3'}
    require(isinstance(rubric,dict) and set(rubric)=={'task_points','review_profile','profile_version',
        'task_family','response_mode','context_requirement'} |
        ({'material_schema','material_bindings','material_semantics'} if material else set()))
    require(isinstance(rubric['task_points'],list) and rubric['task_points']
        and all(isinstance(point,str) and point.strip() for point in rubric['task_points']))
    profile = profile_for(node['subject'],task_family=rubric['task_family'],response_mode=rubric['response_mode'],
        **({'grade':node['grade_band'], 'source_schema':rubric.get('material_schema','reviewed_open_response_source_v1')}
           if visual or node['grade_band']=='四年级' else {}))
    require(rubric['profile_version']==profile['version'] and rubric['review_profile']==profile['profile_id'])
    if rubric['context_requirement']!='self_contained':
        if not material:
            raise ValueError('OPEN_RESPONSE_MATERIAL_BINDING_REQUIRED')
        from .open_response_materials import validate_bindings
        if visual:
            from .open_response_materials import validate_visual_bindings
            validate_visual_bindings(rubric['material_bindings'])
        else:
            validate_bindings(rubric['material_bindings'])
        require(rubric['context_requirement']=='external_material' and item.get('stimulus_text') and not item.get('stimulus_asset'))
        if visual:
            require(rubric['material_semantics']=='image_required')
        elif rubric['material_semantics']!='text_only':
            raise ValueError('OPEN_RESPONSE_IMAGE_SEMANTICS_REQUIRED')
    else:
        require(not material)
    return profile
