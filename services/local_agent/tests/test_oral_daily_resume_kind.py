"""Semantic names do not replace source/subject/unique-target validation."""
import pytest

from home_study_agent.task_start_binding import TaskStartBinding


@pytest.mark.parametrize('stored_kind', ['oral', 'review'])
@pytest.mark.parametrize('spoken_kind', ['oral', 'review'])
def test_daily_oral_review_has_both_semantic_names(stored_kind, spoken_kind):
    item = dict(plan_item_id='check', source_type='oral_daily_check', task_type=stored_kind, subject='数学')
    snapshot = dict(items=[item], status='ready', item=item)
    selector = dict(subject='math', task_kind=spoken_kind, relative_order=None)
    selected = TaskStartBinding.select_resume_target(snapshot, selector)
    assert selected['status'] == 'ready' and selected['item'] == item
    assert item['task_type'] == stored_kind


@pytest.mark.parametrize('fault', ['other_subject', 'other_kind', 'nonoral_review', 'ambiguous', 'different_prompt'])
def test_oral_alias_cannot_choose_an_unrelated_or_ambiguous_task(fault):
    item = dict(plan_item_id='check', source_type='oral_daily_check', task_type='review', subject='数学',
        goal_prompt_binding={'prompt_id': 'original'})
    snapshot = dict(items=[item], status='ready', item=item, displayed_question={'prompt_id': 'original'})
    selector = dict(subject='math', task_kind='oral', relative_order=None)
    if fault == 'other_subject':
        selector['subject'] = 'english'
    elif fault == 'other_kind':
        selector['task_kind'] = 'writing'
    elif fault == 'nonoral_review':
        item['source_type'] = 'agent_retention'
    elif fault == 'ambiguous':
        snapshot['items'].append({**item, 'plan_item_id': 'other'})
    else:
        selector['relative_order'] = 'displayed_question'
        snapshot['displayed_question'] = {'prompt_id': 'other'}
    selected = TaskStartBinding.select_resume_target(snapshot, selector)
    assert selected['status'] == 'ambiguous' and selected['item'] is None
