"""The speech queue must not revive withdrawn optional behavior guidance."""
from datetime import datetime, timedelta, timezone

import pytest

import home_study_agent.agent_runtime as runtime_module
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.database import Database
from test_behavior_accepted_habit_support import active, offer, NOW
from test_behavior_observation_coverage import managed


@pytest.fixture
def queued(managed, monkeypatch):
    db, service = active(managed)
    clock = [datetime.fromisoformat(NOW)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(runtime_module, 'datetime', Clock)
    decision = offer(service)
    assert decision['strategy'] == 'contextual_habit_ask_first'
    store = AgentRuntimeStore(db)
    first = store.next_proactive_prompt('session')
    assert first['status'] == 'delivered' and first['prompt']['message'] == decision['message']
    return db, service, store, first['prompt'], clock


@pytest.mark.parametrize('change', ['stop', 'context', 'source', 'definition', 'other_child'])
def test_cached_prompt_retry_and_late_ack_recheck_original_source(queued, change):
    db, service, store, prompt, clock = queued
    clock[0] += timedelta(minutes=2)
    if change == 'stop':
        service.stop_habit_experiment('goal', 'child-primary-4', clock[0].isoformat(), 'stop-before-playback')
    else:
        with db.connect() as c:
            if change == 'context':
                c.execute("UPDATE plan_item SET subject='english' WHERE plan_item_id='task'")
            elif change == 'source':
                c.execute("UPDATE homework_item SET confirmation_status='pending'")
            elif change == 'definition':
                c.execute("UPDATE habit_experiment SET child_action='replacement' WHERE experiment_id='goal'")
            else:
                c.execute("UPDATE intervention SET child_id='child-junior-3'")
            c.commit()
    retried = AgentRuntimeStore(Database(db.path)).next_proactive_prompt('session')
    assert not retried.get('prompt') or retried['prompt']['prompt_id'] != prompt['prompt_id'], retried
    ack = store.acknowledge_proactive_prompt('session', prompt['prompt_id'])
    assert ack['status'] == 'expired'
    with db.connect() as c:
        saved = c.execute('SELECT * FROM agent_proactive_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
        assert saved['status'] == 'expired' and saved['acknowledged_at'] is not None
        assert saved['message_text'] == prompt['message'] and saved['delivery_attempts'] == 1


def test_live_playback_check_is_read_only_and_cannot_claim_another_session(queued):
    db, service, store, prompt, clock = queued
    pid = prompt['prompt_id']
    assert store.proactive_prompt_current('session', pid) == {'prompt_id': pid, 'current': True}
    with pytest.raises(KeyError):
        store.proactive_prompt_current('different-session', pid)
    with db.connect() as c:
        assert c.execute('SELECT acknowledged_at,delivery_attempts FROM agent_proactive_prompt WHERE prompt_id=?', (pid,)).fetchone()[:] == (None, 1)
    service.stop_habit_experiment('goal', 'child-primary-4', clock[0].isoformat(), 'stop-now')
    assert store.proactive_prompt_current('session', pid) == {'prompt_id': pid, 'current': False}
    assert AgentRuntimeStore(Database(db.path)).proactive_prompt_current('session', pid)['current'] is False


def test_stale_dedicated_intervention_is_not_first_queued(managed, monkeypatch):
    db, service = active(managed)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(NOW).astimezone(tz or timezone.utc)
    monkeypatch.setattr(runtime_module, 'datetime', Clock)
    decision = offer(service)
    with db.connect() as c:
        c.execute("UPDATE plan_item SET subject='english' WHERE plan_item_id='task'")
        c.commit()
    result = AgentRuntimeStore(db).next_proactive_prompt('session')
    assert not result.get('prompt') or result['prompt']['message'] != decision['message']


@pytest.mark.parametrize('intervention_type', ['contextual_habit_ask_first', 'default_ask_first'])
def test_pending_automatic_stop_blocks_existing_reminder_across_restart_and_keeps_other_state(queued, intervention_type):
    db, _, store, prompt, clock = queued
    with db.connect() as c:
        source = c.execute('SELECT source_id FROM agent_proactive_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()['source_id']
        c.execute('UPDATE intervention SET intervention_type=? WHERE intervention_id=?', (intervention_type, source))
        c.commit()
    assert store.proactive_prompt_current('session', prompt['prompt_id'])['current'] is True
    with db.connect() as c:
        c.execute('''INSERT INTO habit_experiment_event(event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
                     VALUES('synthetic-pending','goal','child-primary-4','auto_stop_pending','{}',?,'synthetic-pending-action')''',
                  (clock[0].isoformat(),))
        c.commit()
    assert store.proactive_prompt_current('session', prompt['prompt_id'])['current'] is False
    assert AgentRuntimeStore(Database(db.path)).proactive_prompt_current('session', prompt['prompt_id'])['current'] is False
    # This is a state-specific stop, not a blanket silence of other support.
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM intervention WHERE intervention_id=?', (source,)).fetchone())
        row['intervention_type'] = 'default_ask_first'
        row['candidate_state'] = 'synthetic-unrelated-state'
        assert AgentRuntimeStore._behavior_reminder_current(c, row, clock[0]) is True
    clock[0] += timedelta(minutes=2)
    retried = AgentRuntimeStore(Database(db.path)).next_proactive_prompt('session')
    assert not retried.get('prompt') or retried['prompt']['prompt_id'] != prompt['prompt_id']


def test_queued_habit_proposal_requires_its_original_current_status_and_owner(queued):
    db, _, store, prompt, _ = queued
    with db.connect() as c:
        c.execute("UPDATE agent_proactive_prompt SET source_type='habit_experiment',source_id='goal' WHERE prompt_id=?", (prompt['prompt_id'],))
        c.execute("UPDATE habit_experiment SET status='proposed' WHERE experiment_id='goal'")
        c.commit()
    assert store.proactive_prompt_current('session', prompt['prompt_id'])['current'] is True
    with db.connect() as c:
        c.execute("UPDATE habit_experiment SET status='active' WHERE experiment_id='goal'")
        c.commit()
    assert store.proactive_prompt_current('session', prompt['prompt_id'])['current'] is False
    with db.connect() as c:
        c.execute("UPDATE habit_experiment SET status='proposed',child_id='child-junior-3' WHERE experiment_id='goal'")
        c.commit()
    assert store.proactive_prompt_current('session', prompt['prompt_id'])['current'] is False


def test_another_child_pending_stop_does_not_silence_this_child(queued):
    db, _, store, prompt, clock = queued
    with db.connect() as c:
        original = dict(c.execute("SELECT * FROM habit_experiment WHERE experiment_id='goal'").fetchone())
        original.update(experiment_id='other-synthetic-goal', child_id='child-junior-3')
        columns = list(original)
        c.execute('INSERT INTO habit_experiment('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')',
                  tuple(original[name] for name in columns))
        c.execute('''INSERT INTO habit_experiment_event(event_id,experiment_id,child_id,event_type,payload_json,occurred_at,client_action_id)
                     VALUES('other-pending','other-synthetic-goal','child-junior-3','auto_stop_pending','{}',?,'other-pending-action')''',
                  (clock[0].isoformat(),))
        c.commit()
    assert store.proactive_prompt_current('session', prompt['prompt_id'])['current'] is True
