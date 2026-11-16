"""Late omitted school work must coexist with an evidence-backed due review."""
from datetime import datetime, timedelta
import json
import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from test_due_review_plan_completion import flow, CHILD, OTHER
from test_paused_homework_revision_runtime_http import SemanticGateway, say, post, process_http

WORDS = '还漏了一项语文作业，背诵第二段，预计十分钟'
OMISSION = {'kind': 'homework', 'operation': 'add', 'target_quote': '背诵第二段',
            'changes': {'subject': '语文', 'title': '背诵第二段', 'expected_minutes': 10},
            'change_quotes': {'subject': '语文', 'title': '背诵第二段', 'expected_minutes': '十分钟'}}


@pytest.fixture(autouse=True)
def intake_context_before_plan(monkeypatch):
    from home_study_agent.repository import StudyRepository
    import test_due_review_plan_completion as due_review

    class SchedulingDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime.fromisoformat('2026-10-03T10:00:00+00:00')
            return instant.astimezone(tz) if tz else instant.astimezone().replace(tzinfo=None)

    # The imported flow reserves wall-clock time; 23:30 must not depend on test launch time.
    monkeypatch.setattr(due_review, 'datetime', SchedulingDateTime)
    original = StudyRepository.start_session

    def start(repository, child_id, key):
        session = original(repository, child_id, key)
        repository.save_planning_context(session['session_id'], 'medium', 0, None, None, None, 0, key + '-context')
        return session

    monkeypatch.setattr(StudyRepository, 'start_session', start)


@pytest.mark.parametrize('words,revision,role,confidence', [
    ('不要补上' + WORDS, OMISSION, 'control', .99),
    (WORDS, {**OMISSION, 'changes': {'subject': '语文', 'title': '背诵第二段'},
             'change_quotes': {'subject': '语文', 'title': '背诵第二段'}}, 'control', .99),
    (WORDS, {**OMISSION, 'changes': {**OMISSION['changes'], 'expected_minutes': 20}}, 'control', .99),
    (WORDS, OMISSION, 'unknown', .99),
    (WORDS, OMISSION, 'mixed', .99),
    (WORDS, OMISSION, 'control', .5),
])
def test_late_omission_never_guesses_or_escapes_uncertain_semantics(flow, tmp_path, words, revision, role, confidence):
    db, sid = flow['db'], flow['session']
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(role=role, confidence=confidence, revision=revision))) as client:
        post(client, '/v1/plan-items/' + flow['item']['plan_item_id'] + '/start', 'guard-start')
        post(client, '/v1/plan-items/' + flow['item']['plan_item_id'] + '/pause', 'guard-pause')
        with db.connect() as c:
            before = {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                      for table in ('homework_item', 'plan_item', 'learning_evidence')}
        say(client, sid, words, 'uncertain-omission')
    with db.connect() as c:
        missing_duration = role == 'control' and confidence >= .82 and 'expected_minutes' not in revision['changes']
        assert c.execute('SELECT COUNT(*) FROM learning_input_proposal').fetchone()[0] == int(missing_duration)
        if missing_duration:
            draft = c.execute('SELECT state,proposal_json FROM learning_input_proposal').fetchone()
            assert draft['state'] == 'clarification_required'
            assert 'expected_minutes' not in json.loads(draft['proposal_json'])['changes']
        assert c.execute('SELECT COUNT(*) FROM learning_input_revision_event').fetchone()[0] == 0
        assert {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in before} == before


def test_carryover_uses_family_day_and_still_rejects_a_stale_family_day(flow, monkeypatch):
    from home_study_agent import learning_input_replan as replan
    from home_study_agent.learning_input_carryover import LearningInputCarryoverService, CarryoverConflict
    now = datetime.fromisoformat('2026-09-10T17:00:00+00:00')
    monkeypatch.setattr(replan, 'utc_now', lambda: now)
    assert replan.family_time(now).date().isoformat() == '2026-09-11'
    with flow['db'].connect() as c:
        c.execute('UPDATE daily_session SET started_at=? WHERE session_id=?', (now.isoformat(), flow['session']))
        c.execute("UPDATE daily_plan SET plan_date='2026-09-11' WHERE session_id=?", (flow['session'],))
        service = LearningInputCarryoverService(flow['db'])
        assert service._target(c, CHILD, flow['session'])[5] == '2026-09-11'
        c.execute("UPDATE daily_plan SET plan_date='2026-09-10' WHERE session_id=?", (flow['session'],))
        with pytest.raises(CarryoverConflict, match='CARRYOVER_PLAN_DAY_OR_COUNT_INVALID'):
            service._target(c, CHILD, flow['session'])


@pytest.mark.parametrize('room', [True, False])
def test_late_omission_readback_appends_within_budget_without_losing_long_term_obligations(flow, tmp_path, monkeypatch, room):
    db, sid = flow['db'], flow['session']
    from home_study_agent import learning_input_replan
    monkeypatch.setattr(learning_input_replan, 'utc_now', lambda: datetime.fromisoformat(flow['clock'][0]))
    with db.connect() as c:
        planned = c.execute("SELECT total_minutes FROM daily_plan WHERE session_id=? AND status='active'", (sid,)).fetchone()[0]
        c.execute("UPDATE family_rule SET latest_end_time='23:30',max_learning_minutes=? WHERE child_id=?", (120 if room else planned, CHILD))
        journal = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        c.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,
            ended_at,last_checkpoint_at,client_action_id) VALUES ('sibling-omission',?,'completed','reflection',
            ?,?,?,'sibling-omission')""", (OTHER, *([flow['clock'][0]] * 3)))
        c.execute("""INSERT INTO homework_item(homework_id,child_id,session_id,subject,task_type,title,
            expected_minutes,confirmation_status,status,created_at,client_action_id) VALUES
            ('sibling-same-title',?,'sibling-omission','语文','memorization','背诵第二段',10,
            'child_confirmed','pending',?,'sibling-same-title')""", (OTHER, flow['clock'][0]))
        sibling = tuple(c.execute("SELECT * FROM homework_item WHERE homework_id='sibling-same-title'").fetchone())
        c.commit()
    with TestClient(create_app(Settings(data_dir=tmp_path / 'app', database_path=db.path,
            maintenance_enabled=False), model_gateway=SemanticGateway(revision=OMISSION))) as client:
        # Real due-review task and its original scored evidence, not an invented weak label.
        post(client, '/v1/plan-items/' + flow['item']['plan_item_id'] + '/start', 'due-start')
        post(client, '/v1/plan-items/' + flow['item']['plan_item_id'] + '/pause', 'due-pause')
        with db.connect() as c:
            before = [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY rowid')]
            homework_count = c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0]
        _, proposal = say(client, sid, WORDS, 'late-omission')
        assert proposal['execution']['result'].get('status') == 'awaiting_confirmation', proposal
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0] == homework_count
        _, confirmed = say(client, sid, '确认', 'late-confirm')
        result = confirmed['execution']['result']
        assert result['status'] == 'applied', result
        assert result['replan']['receipt']['allocated_minutes'] == (10 if room else 0), result['replan']
        assert sum(row['minutes'] for row in result['replan']['unplaced']) == (0 if room else 10)
        with db.connect() as c:
            assert [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY rowid LIMIT ?', (len(before),))] == before
            assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
            item = c.execute('SELECT child_id,subject,title,status FROM homework_item WHERE homework_id=?', (result['target_id'],)).fetchone()
            assert tuple(item) == (CHILD, '语文', '背诵第二段', 'pending')
            assert tuple(c.execute("SELECT * FROM homework_item WHERE homework_id='sibling-same-title'").fetchone()) == sibling
        if not room:
            # Explicit end-of-day deferral preserves the due review, never grades it.
            workspace = client.get('/v1/sessions/' + sid).json()
            for row in workspace['plan_items']:
                if row['status'] in ('pending', 'paused'):
                    post(client, '/v1/plan-items/' + row['plan_item_id'] + '/reschedule',
                         'day-end-' + row['plan_item_id'], reason='Explicitly defer unfinished work at day end')
            post(client, '/v1/sessions/' + sid + '/transition', 'end-study',
                 expected_state='study', target_state='reflection', reason='Today ends without claiming unfinished work complete')
            post(client, '/v1/sessions/' + sid + '/reflection/complete', 'day-close',
                 learned_text='', difficult_text='', feeling='okay', next_action='', skipped=False)
            summary = client.get('/v1/sessions/' + sid + '/learning-summary').json()
            assert next(row for row in summary['homework'] if row['source']['source_id'] == result['target_id'])['status'] == 'unfinished'
            flow['clock'][0] = (datetime.fromisoformat(flow['clock'][0]) + timedelta(days=1)).isoformat()
            next_sid = flow['repo'].start_session(CHILD, 'next-omission-day')['session_id']
            post(client, '/v1/sessions/' + next_sid + '/transition', 'next-recall',
                 expected_state='check_in', target_state='classroom_recall', reason='Next-day intake')
            post(client, '/v1/sessions/' + next_sid + '/recalls/none', 'no-recall')
            post(client, '/v1/sessions/' + next_sid + '/transition', 'next-homework',
                 expected_state='classroom_recall', target_state='homework_capture', reason='Review saved work')
            view = client.get('/v1/sessions/' + next_sid).json()['learning_input_unplaced']
            pending = next(row for row in view['items'] if row['obligation_id'] == result['replan']['unplaced'][0]['obligation_id'])
            assert pending['source_current'] and pending['remaining_minutes'] == 10
            assert 'continue_today' in pending['allowed_actions']
            say(client, next_sid, '没排完的作业有哪些', 'next-list')
            say(client, next_sid, '第一项', 'next-select')
            _, accepted = say(client, next_sid, '今天继续', 'next-accept')
            assert accepted['execution']['result']['status'] == 'accepted_for_planning', accepted
            carried_id = accepted['execution']['result']['target_homework_id']
            assert carried_id != result['target_id']
            plan = flow['repo'].generate_plan(next_sid, 'next-with-omission')
            assert any(row['source_type'] == 'school_homework' and row['source_ref'] == carried_id
                       for row in plan['plan_items'])
            next_view = client.get('/v1/sessions/' + next_sid).json()
            due = next(row for row in next_view['constraint_decisions']
                       if row['obligation_type'] == 'due_review' and row['source_ref'] == flow['item']['source_ref'])
            assert (due['decision'], due['requested_minutes'], due['allocated_minutes']) == ('deferred', 10, 0)
            assert '预算不足' in due['reason'] and '未取消' in due['reason']
            restarted = process_http(db, tmp_path / 'nextday-process', next_sid,
                datetime.fromisoformat(flow['clock'][0]), [
                    {'kind': 'http', 'path': '/v1/sessions/' + next_sid},
                    {'kind': 'http', 'path': '/v1/sessions/' + sid + '/learning-summary'},
                    {'kind': 'http', 'path': '/v1/sessions/' + next_sid + '/agent-request-receipts/daily-proof-next-accept'}])
            assert restarted['model_requests'] == 0
            assert restarted['results'][0]['plan_items'] == next_view['plan_items']
            assert restarted['results'][0]['constraint_decisions'] == next_view['constraint_decisions']
            assert next(row for row in restarted['results'][1]['homework']
                        if row['source']['source_id'] == result['target_id'])['status'] == 'unfinished'
            with db.connect() as c:
                event = c.execute("SELECT result_json FROM learning_input_carryover_event WHERE obligation_id=? AND operation='continue_today'",
                                  (pending['obligation_id'],)).fetchone()
                assert json.loads(event[0])['target_homework_id'] == carried_id
                source = c.execute('SELECT subject,title,status FROM homework_item WHERE homework_id=?', (result['target_id'],)).fetchone()
                carried = c.execute('SELECT subject,title,status FROM homework_item WHERE homework_id=?', (carried_id,)).fetchone()
                assert tuple(source) == tuple(carried) == ('语文', '背诵第二段', 'pending')
                assert c.execute('SELECT status FROM review_task WHERE review_task_id=?',
                                 (flow['item']['source_ref'],)).fetchone()[0] == 'pending'
                assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
                assert tuple(c.execute("SELECT * FROM homework_item WHERE homework_id='sibling-same-title'").fetchone()) == sibling


@pytest.mark.parametrize('case', ['replay_duplicate_undo', 'changed_plan', 'cancel'])
def test_omission_proposal_and_commands_survive_process_restart_without_duplicate_or_stale_write(flow, tmp_path, monkeypatch, case):
    db, sid = flow['db'], flow['session']
    from home_study_agent import learning_input_replan
    now = datetime.fromisoformat(flow['clock'][0])
    monkeypatch.setattr(learning_input_replan, 'utc_now', lambda: now)
    with db.connect() as c:
        c.execute("UPDATE family_rule SET latest_end_time='23:30',max_learning_minutes=120 WHERE child_id=?", (CHILD,))
        evidence = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        before = c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0]
        c.commit()
    first = process_http(db, tmp_path / 'process-data', sid, now, [
        {'kind': 'http', 'method': 'POST', 'path': '/v1/plan-items/' + flow['item']['plan_item_id'] + '/start',
         'json': {'client_action_id': 'process-due-start'}},
        {'kind': 'http', 'method': 'POST', 'path': '/v1/plan-items/' + flow['item']['plan_item_id'] + '/pause',
         'json': {'client_action_id': 'process-due-pause'}},
        {'kind': 'say', 'text': WORDS, 'key': 'omission-proposal'}], revision=OMISSION)
    assert first['results'][-1]['result']['execution']['result']['status'] == 'awaiting_confirmation'
    if case == 'changed_plan':
        with db.connect() as c:
            c.execute('UPDATE daily_plan SET version=version+1 WHERE session_id=?', (sid,))
            c.commit()
    second = process_http(db, tmp_path / 'process-data', sid, now, [
        {'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-omission-proposal'},
        {'kind': 'say', 'text': '取消' if case == 'cancel' else '确认', 'key': 'omission-confirm'}], revision=OMISSION)
    result = second['results'][-1]['result']['execution']['result']
    assert result['status'] == {'changed_plan': 'conflict', 'cancel': 'cancelled', 'replay_duplicate_undo': 'applied'}[case]
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0] == before + int(case == 'replay_duplicate_undo')
        committed = [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY rowid')]
    third = process_http(db, tmp_path / 'process-data', sid, now, [
        {'kind': 'http', 'path': '/v1/sessions/' + sid + '/agent-request-receipts/daily-proof-omission-confirm'},
        {'kind': 'say', 'text': '取消' if case == 'cancel' else '确认', 'key': 'omission-confirm'}], revision=OMISSION)
    assert third['results'][-1]['result']['execution']['result'] == result
    assert len({first['pid'], second['pid'], third['pid']}) == 3
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY rowid')] == committed
    if case == 'replay_duplicate_undo':
        fourth = process_http(db, tmp_path / 'process-data', sid, now,
            [{'kind': 'say', 'text': WORDS, 'key': 'omission-duplicate'}], revision=OMISSION)
        assert fourth['results'][0]['result']['execution']['result']['reason'] == 'matching_homework_exists'
        undo = {'kind': 'homework', 'operation': 'undo', 'target_quote': '背诵第二段', 'changes': {}, 'change_quotes': {}}
        fifth = process_http(db, tmp_path / 'process-data', sid, now, [
            {'kind': 'say', 'text': '撤销刚才补录的背诵第二段', 'key': 'omission-undo'},
            {'kind': 'say', 'text': '确认撤销', 'key': 'omission-undo-confirm'}], revision=undo)
        assert fifth['results'][-1]['result']['execution']['result']['status'] == 'applied'
        with db.connect() as c:
            assert c.execute('SELECT status FROM homework_item WHERE homework_id=?', (result['target_id'],)).fetchone()[0] == 'cancelled'
            assert c.execute('SELECT COUNT(*) FROM homework_item').fetchone()[0] == before + 1
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == evidence
