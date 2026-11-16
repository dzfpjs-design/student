"""Read-only PET opportunity allocation; practice counts never certify skill.

The seven-day protection window is an explicit scheduling policy, not an
estimate of learning efficacy or an official exam readiness threshold.
"""
from datetime import date, datetime, timezone

from .family_clock import calendar_as_of, family_time
from .learning_feedback import build_learning_feedback
from .learning_state import LearningStateService
from .specialized_program import SpecializedProgramService


SKILLS = ('listening', 'speaking', 'reading', 'writing')
LABELS = dict(listening='听力', speaking='口语', reading='阅读', writing='写作')


def allocate_pet_practice(connection, child_id, minutes, due_cards, *, today=None, as_of=None, requested_skill=None):
    if requested_skill is not None and requested_skill not in SKILLS:
        raise ValueError('unknown PET skill')
    now = datetime.fromisoformat(as_of.replace('Z', '+00:00')) if as_of is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('timezone-aware PET planning time required')
    today, now = calendar_as_of(today, now)
    if not connection.in_transaction:
        connection.execute('BEGIN')
        try:
            return allocate_pet_practice(connection, child_id, minutes, due_cards, today=today, as_of=now.isoformat(), requested_skill=requested_skill)
        finally:
            connection.rollback()
    budget = max(0, int(minutes))
    feedback = build_learning_feedback(connection, child_id, now=now)
    from .pet_writing_stage_plan import project_writing_stage_plan, WRITING_STAGE_MINUTES
    writing = project_writing_stage_plan(connection, child_id, as_of=now.isoformat())
    unplaced = list(writing['unplaced_obligations'])
    weaknesses = {node['knowledge_node_id']: node['status'] for node in feedback['nodes'] if node['track'] == 'pet'}
    rows = connection.execute("""SELECT di.*,kn.title,kn.subject,kn.knowledge_node_id
        FROM diagnostic_item di
        JOIN knowledge_node kn USING(knowledge_node_id) WHERE kn.track='pet'
          AND kn.status='active' AND di.status='active' ORDER BY di.diagnostic_item_id""").fetchall()
    available = {skill: [] for skill in SKILLS}
    from .pet_task_execution import freeze_pet_question, PetTaskExecutionError
    question_bindings = {}
    for row in rows:
        domain = SpecializedProgramService._mastery_domain('pet', row)
        skill = domain.split('.')[1] if domain and domain.startswith('pet.') else None
        if skill not in available or not LearningStateService._eligible_diagnostic(connection, child_id, row['diagnostic_item_id']):
            continue
        if skill == 'listening' and not str(row['stimulus_text'] or '').strip():
            # A missing/unverified audio asset cannot be silently substituted by reading text.
            continue
        if skill != 'writing':
            try:
                question_bindings[row['diagnostic_item_id']] = freeze_pet_question(connection, child_id, row['diagnostic_item_id'])
            except PetTaskExecutionError:
                continue
        available[skill].append(row)
    attempts = connection.execute(
        """SELECT da.diagnostic_item_id,da.created_at,kn.knowledge_node_id,kn.subject,kn.title FROM diagnostic_attempt da
        JOIN diagnostic_item di USING(diagnostic_item_id) JOIN knowledge_node kn USING(knowledge_node_id)
        WHERE da.child_id=? AND kn.track='pet'""", (child_id,)).fetchall()
    dates = {skill: set() for skill in SKILLS}
    prior_attempts = {}
    for attempt in attempts:
        domain = SpecializedProgramService._mastery_domain('pet', attempt)
        skill = domain.split('.')[1] if domain and domain.startswith('pet.') else None
        try:
            occurred = datetime.fromisoformat(attempt['created_at'].replace('Z', '+00:00'))
            day = family_time(occurred).date()
        except (AttributeError, TypeError, ValueError):
            continue
        if occurred > now:
            continue
        if skill in dates and day <= today:
            dates[skill].add(day)
            item_id = attempt['diagnostic_item_id']
            prior_attempts[item_id] = prior_attempts.get(item_id, 0) + 1
    opportunity = []
    for skill in SKILLS:
        age = (today - max(dates[skill])).days if dates[skill] else None
        weak = any(weaknesses.get(row['knowledge_node_id']) in ('recheck_needed', 'supported_only') for row in available[skill])
        opportunity.append(dict(skill=skill, days_since_practice=age, protection_due=age is None or age >= 7,
            practice_days_14=sum((today - day).days < 14 for day in dates[skill]), reliable_weakness=weak,
            available_items=len(writing['items']) if skill == 'writing' else len(available[skill])))
    ranked = sorted(opportunity, key=lambda x: (not x['protection_due'], not x['reliable_weakness'],
        x['practice_days_14'], -(x['days_since_practice'] if x['days_since_practice'] is not None else 10000), SKILLS.index(x['skill'])))
    chosen = next((item for item in ranked if item['available_items'] and (requested_skill is None or item['skill']==requested_skill)), None)
    skill_minutes = budget - min(5, budget // 3) if chosen and budget >= 5 and due_cards else budget if chosen else 0
    if skill_minutes < 5:
        skill_minutes = 0
    vocabulary_minutes = budget - skill_minutes if due_cards else 0
    if chosen and chosen['skill'] == 'writing':
        if budget >= WRITING_STAGE_MINUTES:
            skill_minutes = WRITING_STAGE_MINUTES
            vocabulary_minutes = min(5, budget // 3, budget - skill_minutes) if due_cards else 0
        else:
            skill_minutes = 0
            vocabulary_minutes = budget if due_cards else 0
            unplaced.append({**writing['items'][0], 'reason_code': 'pet_writing_budget_insufficient',
                'reason': '剩余时间不足以安排完整写作任务，保留原题待安排，不压缩题目或增加学习时长。',
                'blockers': []})
    items = []
    if vocabulary_minutes:
        cards = min(int(due_cards), 12, vocabulary_minutes)
        items.append(dict(homework_id=None, subject='英语', title=f'PET词汇间隔复习 · {cards}张到期',
            minutes=vocabulary_minutes, source_type='pet_daily', source_ref=f'pet-vocabulary-due:{cards}',
            completion_standard=f'通过语音完成{cards}张到期词汇卡，每张保存作答结果', cognitive_load='medium',
            task_type='memorization', status='pending'))
    if chosen and skill_minutes and chosen['skill'] == 'writing':
        items.append(writing['items'][0])
    elif chosen and skill_minutes:
        candidates = sorted(available[chosen['skill']], key=lambda row: (
            weaknesses.get(row['knowledge_node_id']) not in ('recheck_needed', 'supported_only'),
            prior_attempts.get(row['diagnostic_item_id'], 0), row['difficulty'], row['diagnostic_item_id']))
        selected = candidates[0]
        items.append(dict(homework_id=None, subject='英语', title=f"PET{LABELS[chosen['skill']]} · {selected['title']}",
            minutes=skill_minutes, source_type='pet_daily', source_ref=selected['diagnostic_item_id'],
            completion_standard='完成原题并保存自己的回答；讲解或练习时长不代表技能已掌握。', cognitive_load='medium',
            task_type='speaking' if chosen['skill'] in ('listening', 'speaking') else chosen['skill'], status='pending',
            planning_evidence={'pet_question_binding': question_bindings[selected['diagnostic_item_id']]}))
    return dict(items=items, opportunities=opportunity, version='pet_opportunity_v2',
        missing_skills=[item['skill'] for item in opportunity if not item['available_items']
            and (item['skill'] != 'writing' or not writing['source_available'])],
        unplaced_obligations=unplaced,
        unallocated_minutes=budget - sum(item['minutes'] for item in items),
        mastery_claim_allowed=False, cloud_transmission_allowed=False)
