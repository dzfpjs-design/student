"""Isolated ownership fences, with no second learning or model ledger."""
from contextlib import contextmanager
import sqlite3

import pytest

from home_study_agent.agent_request_ownership import claim, preserve_live_claims, AgentRequestOwnershipError, ACTION


def test_same_source_unknown_can_resume_only_local_work(db):
    from home_study_agent.agent_request_ownership import execution_scope, authorize_external_dispatch, LocalHandoffDispatchDenied
    args = (db, 'session', 'action', 'original')
    assert claim(*args, allow_local_resume=True)
    assert not claim(*args, allow_local_resume=True)
    with execution_scope(False) as authority:
        with pytest.raises(LocalHandoffDispatchDenied):
            authorize_external_dispatch()
        assert not authority.external_started


def test_terminal_unsent_permit_has_one_atomic_consumer_and_restore_keeps_consumption(db):
    from concurrent.futures import ThreadPoolExecutor
    from home_study_agent.agent_request_ownership import permit_local_retry, USED
    args = (db, 'session', 'action', 'original')
    claim(*args, voice_input_id='voice')
    permit_local_retry(db, 'session', 'action', 'voice')
    with pytest.raises(AgentRequestOwnershipError, match='SOURCE_CONFLICT'):
        claim(db, 'session', 'action', 'changed', voice_input_id='voice', allow_local_resume=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(*args, voice_input_id='voice', allow_local_resume=True), range(2)))
    assert sorted(results) == [False, True]
    assert not claim(*args, voice_input_id='voice', allow_local_resume=True)
    with db.connect() as live:
        restored = sqlite3.connect(':memory:')
        live.backup(restored)
        restored.execute('DELETE FROM audit_event WHERE action=?', (USED,))
        preserve_live_claims(live, restored)
        assert restored.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (USED,)).fetchone()[0] == 1
        assert all('original' not in row[0] for row in restored.execute('SELECT summary_json FROM audit_event'))
        restored.close()


@pytest.mark.parametrize('method', ['complete', 'complete_on_provider', 'complete_on_fallback'])
def test_local_resume_denies_all_model_routes_before_adapter_is_called(method):
    import asyncio
    from home_study_agent.agent_request_ownership import OwnedModelGateway, execution_scope, LocalHandoffDispatchDenied
    class Gateway:
        calls = 0
        async def complete(self, *args):
            self.calls += 1
        complete_on_provider = complete
        complete_on_fallback = complete
    adapter = Gateway()
    gateway = OwnedModelGateway(adapter)
    args = ('provider', object()) if method == 'complete_on_provider' else (object(),)
    with execution_scope(False):
        with pytest.raises(LocalHandoffDispatchDenied):
            asyncio.run(getattr(gateway, method)(*args))
    assert adapter.calls == 0
    with execution_scope(True) as authority:
        asyncio.run(getattr(gateway, method)(*args))
        assert authority.external_started
    assert adapter.calls == 1


def test_nested_and_thread_scope_cannot_upgrade_a_local_resume():
    import asyncio
    from home_study_agent.agent_request_ownership import execution_scope, authorize_external_dispatch, LocalHandoffDispatchDenied
    async def in_thread():
        with execution_scope(True):
            await asyncio.to_thread(authorize_external_dispatch)
    with execution_scope(False) as outer:
        with pytest.raises(LocalHandoffDispatchDenied):
            asyncio.run(in_thread())
        assert not outer.external_started
    with execution_scope(True) as outer:
        asyncio.run(in_thread())
        assert outer.external_started
    authorize_external_dispatch()


def test_known_immutable_rejection_is_bound_and_not_a_general_model_retry(db):
    from home_study_agent.agent_request_ownership import terminal_rejection, REJECTED
    code = 'ANSWER_WRITING_DAILY_SOURCE_UNAVAILABLE'
    claim(db, 'session', 'action', 'original', voice_input_id='voice')
    assert terminal_rejection(db, 'session', 'action', 'voice') is None
    assert terminal_rejection(db, 'session', 'action', 'voice', record_code=code) == code
    assert terminal_rejection(db, 'session', 'action', 'voice') == code
    with pytest.raises(AgentRequestOwnershipError, match='SOURCE_CONFLICT'):
        terminal_rejection(db, 'other', 'action', 'voice')
    with pytest.raises(AgentRequestOwnershipError, match='SOURCE_CONFLICT'):
        terminal_rejection(db, 'session', 'action', 'different')
    with pytest.raises(ValueError, match='not terminal'):
        terminal_rejection(db, 'session', 'unconfirmed', None, record_code='AGENT_HANDOFF_UNCONFIRMED')
    with db.connect() as live:
        staged = sqlite3.connect(':memory:')
        live.backup(staged)
        staged.execute('DELETE FROM audit_event WHERE action=?', (REJECTED,))
        preserve_live_claims(live, staged)
        assert staged.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (REJECTED,)).fetchone()[0] == 1
        staged.close()


@pytest.fixture
def db(tmp_path):
    class Store:
        path = tmp_path / 'isolated.db'
        @contextmanager
        def connect(self):
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            try:
                yield c
            finally:
                c.close()
    database = Store()
    with database.connect() as c:
        c.executescript('''CREATE TABLE daily_session(session_id TEXT,child_id TEXT);
            CREATE TABLE agent_turn(client_action_id TEXT);
            CREATE TABLE audit_event(audit_id TEXT PRIMARY KEY,actor TEXT,action TEXT,child_id TEXT,summary_json TEXT,occurred_at TEXT);
            INSERT INTO daily_session VALUES ('session','child'),('other','other-child');''')
    return database


def test_claim_never_contains_original_text_and_unknown_is_not_time_based(db):
    claim(db, 'session', 'action', '原话不能放进认领记录', voice_input_id='voice')
    with db.connect() as c:
        rows = c.execute('SELECT summary_json FROM audit_event').fetchall()
        assert rows and all('原话' not in row[0] for row in rows)
        c.execute("UPDATE audit_event SET occurred_at='2000-01-01T00:00:00+00:00'")
        c.commit()
    with pytest.raises(AgentRequestOwnershipError, match='UNCONFIRMED'):
        claim(db, 'session', 'action', '原话不能放进认领记录', voice_input_id='voice')


@pytest.mark.parametrize('changes', [
    {'transcript': 'changed'}, {'voice_input_id': 'other-voice'}, {'session_id': 'other'},
    {'playback_overlap_id': 'overlap'}, {'pending_evaluation_turn_id': 'pending'},
    {'local_recovery_only': True}, {'action_id': 'new-engine-key'},
])
def test_action_and_original_source_both_have_one_owner(db, changes):
    values = dict(session_id='session', action_id='action', transcript='original', voice_input_id='voice')
    claim(db, **values)
    with pytest.raises(AgentRequestOwnershipError, match='SOURCE_CONFLICT'):
        claim(db, **{**values, **changes})
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0] == 2


def test_different_new_input_is_not_blocked_by_unknown_prior_claim(db):
    claim(db, 'session', 'old', 'original', voice_input_id='old-voice')
    claim(db, 'session', 'new', '停止监听', voice_input_id='new-voice')


def test_exact_safety_only_transaction_can_recover_without_model_authority(db):
    values = dict(voice_input_id='rescue-voice', local_recovery_only=True)
    claim(db, 'session', 'rescue', '需要大人帮忙', **values)
    claim(db, 'session', 'rescue', '需要大人帮忙', **values)
    with pytest.raises(AgentRequestOwnershipError, match='SOURCE_CONFLICT'):
        claim(db, 'session', 'rescue', '需要大人帮忙', voice_input_id='rescue-voice')


def test_existing_turn_remains_only_replay_result_authority(db):
    claim(db, 'session', 'action', 'original', voice_input_id='voice')
    with db.connect() as c:
        c.execute("INSERT INTO agent_turn VALUES ('action')")
        c.commit()
    claim(db, 'session', 'action', 'original', voice_input_id='voice')
    with pytest.raises(AgentRequestOwnershipError, match='SOURCE_CONFLICT'):
        claim(db, 'session', 'action', 'changed', voice_input_id='voice')


def test_restore_preserves_live_consumed_fences_without_restoring_results(db):
    claim(db, 'session', 'action', 'original', voice_input_id='voice')
    with db.connect() as live, sqlite3.connect(':memory:') as staged:
        staged.execute('CREATE TABLE audit_event(audit_id TEXT PRIMARY KEY,actor TEXT,action TEXT,child_id TEXT,summary_json TEXT,occurred_at TEXT)')
        preserve_live_claims(live, staged)
        count = staged.execute('SELECT COUNT(*) FROM audit_event WHERE action=?', (ACTION,)).fetchone()[0]
        assert count > 0
        preserve_live_claims(live, staged)
        assert staged.execute('SELECT COUNT(*) FROM audit_event').fetchone()[0] == count
        assert not staged.execute("SELECT 1 FROM sqlite_master WHERE name='agent_turn'").fetchone()


def test_restore_refuses_conflicting_fence_without_overwriting_history(db):
    claim(db, 'session', 'action', 'original', voice_input_id='voice')
    with db.connect() as live, sqlite3.connect(':memory:') as staged:
        staged.execute('CREATE TABLE audit_event(audit_id TEXT PRIMARY KEY,actor TEXT,action TEXT,child_id TEXT,summary_json TEXT,occurred_at TEXT)')
        preserve_live_claims(live, staged)
        staged.execute("UPDATE audit_event SET summary_json='{}'")
        before = staged.execute('SELECT * FROM audit_event ORDER BY audit_id').fetchall()
        with pytest.raises(AgentRequestOwnershipError, match='RESTORE_CONFLICT'):
            preserve_live_claims(live, staged)
        assert staged.execute('SELECT * FROM audit_event ORDER BY audit_id').fetchall() == before
