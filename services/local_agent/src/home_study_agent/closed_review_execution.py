"""Runtime-owned exact-answer reviews reuse the existing evidence and due checks."""
from .diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
from .repository import PlanInputError, StudyRepository


def is_closed_review(c, item):
    if item['source_type'] != 'due_review':
        return False
    from .school_open_reviews import is_school_review
    from .pet_task_execution import is_generic_pet_task
    if is_school_review(c,item) or is_generic_pet_task(c,item):
        return False
    source=c.execute('''SELECT d.answer_type FROM review_task r JOIN diagnostic_item d USING(diagnostic_item_id)
        WHERE r.review_task_id=?''',(item['source_ref'],)).fetchone()
    return bool(source and source['answer_type'] not in {'writing','listening_text'})


def resolve_execution(c,session_id,plan_item_id):
    if not c.in_transaction:
        c.execute('BEGIN')
        try:
            return resolve_execution(c,session_id,plan_item_id)
        finally:
            c.rollback()
    blocked={'status':'blocked','reason_code':'closed_review_source_changed',
        'reason':'本次复习的题目、到期依据或原结果已变化，请核对原任务；不会直接开始计时或另换一题。',
        'local_only':True,'cloud_transmission_allowed':False,'mastery_claim_allowed':False}
    task=c.execute('''SELECT pi.*,dp.child_id,dp.status AS plan_status,ds.status AS session_status,
        ds.child_id AS session_child,ds.state FROM plan_item pi JOIN daily_plan dp USING(plan_id)
        JOIN daily_session ds ON ds.session_id=dp.session_id WHERE pi.plan_item_id=? AND dp.session_id=?''',
        (plan_item_id,session_id)).fetchone()
    if (not task or not is_closed_review(c,task) or task['child_id']!=task['session_child']
            or task['plan_status']!='active' or task['session_status']!='active' or task['state']!='study'
            or task['status'] not in {'pending','active','paused','completed'}):
        return blocked
    try:
        StudyRepository._validate_review_source(c,task['source_ref'],task['child_id'])
        review=DiagnosticPromptService._review(c,task['child_id'],task['source_ref'],allowed_statuses=('pending','completed'))
        item=DiagnosticPromptService._item(c,task['child_id'],review['diagnostic_item_id']) if review else None
        if not item or item['subject']!=task['subject'] or task['task_type']!='review':
            return blocked
    except (PlanInputError,ValueError,TypeError,KeyError):
        return blocked
    binding={'version':'closed_review_binding_v1',**review,'item_version':_version(item)}
    return {**blocked,'status':'ready','child_id':task['child_id'],'review_task_id':review['review_task_id'],
        'diagnostic_item_id':item['diagnostic_item_id'],'item_version':_version(item),
        'diagnostic_item':{k:item[k] for k in _ITEM_FIELDS},'binding':binding}
