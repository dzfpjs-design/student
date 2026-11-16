"""Use real persisted plan rows; no family data or fabricated attention."""
from datetime import date, datetime, time, timedelta, timezone
import pytest
from home_study_agent.daily_study_duration import project_recorded_task_time
from home_study_agent.family_clock import FAMILY_ZONE
from test_combined_report_real_sources_fixture import synthetic_combined_family


def test_completed_task_timer_is_child_and_family_day_scoped(synthetic_combined_family):
    f=synthetic_combined_family
    end=datetime.combine(date.fromisoformat(f['day']),time(12),FAMILY_ZONE).astimezone(timezone.utc)
    start=end-timedelta(minutes=3)
    with f['db'].connect() as c:
        item=c.execute('SELECT i.plan_item_id FROM plan_item i JOIN daily_plan p ON p.plan_id=i.plan_id WHERE p.session_id=?',(f['session'],)).fetchone()[0]
        c.execute('UPDATE plan_item SET started_at=?,completed_at=?,active_seconds=120,actual_minutes=20 WHERE plan_item_id=?',(start.isoformat(),end.isoformat(),item))
        c.commit()
        c.execute('BEGIN')
        result=project_recorded_task_time(c,f['child'],f['day'])
        assert result['recorded_active_seconds']==120
        assert result['tasks'][0]['reported_minutes']==20
        assert project_recorded_task_time(c,'other-child',f['day'])['tasks']==[]
        from home_study_agent.daily_report_visual_submission import _render
        body={'generated_at':'synthetic', 'projection':{'visual':{'pages':[]}, 'timing':{'pages':[]},
              'task_time':result, 'samples':{'sources':{}}}, 'details':{'pages':[], 'questions':[]}}
        _, text, _ = _render(f['day'],body,'standard')
        assert '已记录任务计时合计：2.0 分钟' in text
        assert '任务计时和填写时长不等于实际专注或学习时长' in text


def test_cross_day_and_impossible_timers_are_not_counted(synthetic_combined_family):
    f=synthetic_combined_family
    with f['db'].connect() as c:
        item=c.execute('SELECT i.plan_item_id FROM plan_item i JOIN daily_plan p ON p.plan_id=i.plan_id WHERE p.session_id=?',(f['session'],)).fetchone()[0]
        end=datetime.combine(date.fromisoformat(f['day']),time(12),FAMILY_ZONE).astimezone(timezone.utc)
        c.execute('UPDATE plan_item SET started_at=?,completed_at=?,active_seconds=500 WHERE plan_item_id=?',((end-timedelta(seconds=30)).isoformat(),end.isoformat(),item)); c.commit()
        c.execute('BEGIN')
        result=project_recorded_task_time(c,f['child'],f['day'])
        assert result['recorded_active_seconds']==0 and result['omitted_count']==1
        c.rollback()
        c.execute('UPDATE plan_item SET started_at=?,active_seconds=1 WHERE plan_item_id=?',((end-timedelta(days=2)).isoformat(),item)); c.commit()
        c.execute('BEGIN')
        assert project_recorded_task_time(c,f['child'],f['day'])['omitted_count']==1


def test_requires_transaction(synthetic_combined_family):
    f=synthetic_combined_family
    with f['db'].connect() as c:
        with pytest.raises(ValueError,match='TASK_TIME_TRANSACTION_REQUIRED'):
            project_recorded_task_time(c,f['child'],f['day'])


def test_parent_preview_exposes_task_timer_only_for_its_child(synthetic_combined_family):
    from home_study_agent.parent_daily_visual_preview import parent_daily_visual_preview
    f=synthetic_combined_family
    result=parent_daily_visual_preview(f['db'],f['child'],f['day'])['recorded_task_time']
    assert len(result['tasks'])==1 and result['tasks'][0]['reported_minutes']==20
    assert 'plan_item_id' not in result['tasks'][0]
    assert parent_daily_visual_preview(f['db'],'child-primary-4',f['day'])['recorded_task_time']['tasks']==[]
