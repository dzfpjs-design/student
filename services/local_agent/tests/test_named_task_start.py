"""Named starts preserve the child's target and the captured schedule."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.task_start_binding import TaskStartBinding
from test_mistake_review_task_entry import paper_task, request
from test_mistake_voice_journey import ok, focus


@pytest.mark.parametrize('quote,expected', [
    ('开始今天的错题复习', 'paper-review'),
    ('开始今天复查', 'paper-review'),
    ('我先做英语那项', 'paper-review'),
    ('先开始数学作业', 'later-school'),
    ('开始下一项', 'later-school'),
    ('开始物理错题', None),
    ('数学作业然后英语错题都开始', None),
    ('不是错题，开始作业', None),
])
def test_named_start_does_not_silently_take_first_task(paper_task, monkeypatch, quote, expected):
    config, db, sid, mistake, gateway = paper_task
    with db.connect() as c:
        c.execute("UPDATE plan_item SET position=0 WHERE plan_item_id='later-school'")
        c.commit()
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        # Control inference is synthetic; actual selection, execution and writes
        # run through the same voice/agent HTTP path as the renderer.
        from test_task_resume_voice_http import hear
        turn = hear(client, config, monkeypatch, gateway, sid, None, quote, 'named-start', 'task.start')
        result = ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        with db.connect() as c:
            active = [r[0] for r in c.execute("SELECT plan_item_id FROM plan_item WHERE status='active'")]
            assert active == ([expected] if expected else [])
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
        if expected == 'paper-review':
            assert result['execution']['result']['mistake_dialogue']['mistake_id'] == mistake
            assert focus(client, sid)['state']['submitted_answer'] == 'B'


@pytest.mark.parametrize('change', ['subject', 'source', 'position', 'add_match'])
def test_named_start_rejects_changed_schedule_before_execution(paper_task, monkeypatch, change):
    config, db, sid, _, gateway = paper_task
    with TestClient(create_app(config, model_gateway=gateway)) as client:
        url = request(client, paper_task, monkeypatch, '开始英语错题复习', 'named-race', 'task.start')
        with db.connect() as c:
            if change == 'subject':
                c.execute("UPDATE plan_item SET subject='数学' WHERE plan_item_id='paper-review'")
            elif change == 'source':
                c.execute("UPDATE plan_item SET source_ref='new-paper' WHERE plan_item_id='paper-review'")
            elif change == 'position':
                c.execute("UPDATE plan_item SET position=4 WHERE plan_item_id='paper-review'")
            else:
                c.execute("UPDATE plan_item SET subject='英语',source_type='mistake_review',task_type='review' WHERE plan_item_id='later-school'")
            c.commit()
        assert client.post(url).status_code == 409
        with db.connect() as c:
            assert not c.execute("SELECT 1 FROM plan_item WHERE status='active'").fetchone()
            assert not c.execute('SELECT 1 FROM mistake_dialogue WHERE session_id=?', (sid,)).fetchone()


@pytest.mark.parametrize('situation,reason', [
    ('duplicate', 'requested_task_ambiguous'),
    ('break', 'planned_break_first'),
    ('paused', 'paused_task'),
])
def test_explicit_target_keeps_ambiguity_break_and_pause_boundaries(paper_task, situation, reason):
    _, db, sid, _, _ = paper_task
    with db.connect() as c:
        if situation == 'duplicate':
            c.execute("UPDATE plan_item SET source_type='mistake_review',task_type='review' WHERE plan_item_id='later-school'")
        elif situation == 'break':
            c.execute("UPDATE plan_item SET source_type='planned_break',position=0 WHERE plan_item_id='later-school'")
        else:
            c.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id='later-school'")
        snapshot = TaskStartBinding.start_snapshot(c, sid, 'child-primary-4')
        selected = TaskStartBinding.select_start_target(snapshot, TaskStartBinding.requested_start_filter('开始错题复习'))
        assert selected['status'] == 'blocked' and selected['reason'] == reason
        assert selected['item'] is None


@pytest.mark.parametrize('source,kind,quote', [
    ('pet_daily', 'speaking', '开始英语口语'),
    ('oral_daily_check', 'review', '开始英语口语'),
    ('oral_daily_check', 'oral', '开始英语复习'),
])
def test_spoken_task_names_match_existing_scheduled_type_aliases(paper_task, source, kind, quote):
    _, db, sid, _, _ = paper_task
    with db.connect() as c:
        c.execute('UPDATE plan_item SET subject=?,source_type=?,task_type=? WHERE plan_item_id=?',
                  ('英语', source, kind, 'later-school'))
        c.execute("UPDATE plan_item SET subject='数学' WHERE plan_item_id='paper-review'")
        snapshot = TaskStartBinding.start_snapshot(c, sid, 'child-primary-4')
        selected = TaskStartBinding.select_start_target(snapshot, TaskStartBinding.requested_start_filter(quote))
        assert selected['status'] == 'ready' and selected['item']['plan_item_id'] == 'later-school'
