import json
import sqlite3

import pytest

from home_study_agent.diagnostic_history_identity import (
    HistoryIdentityUnavailable, historical_identity, ensure_schema,
)
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from test_diagnostic_prompt import flow, prepare, CHILD, OTHER, ITEM, SECOND


def saved(db, prompt):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone())


def test_full_seal_private_and_immutable_same_transaction(flow):
    db, service, sid = flow
    response = prepare(service, sid)
    prompt = response['prompt']
    assert 'identity_snapshot_json' not in json.dumps(response)
    row = saved(db, prompt)
    proof = json.loads(row['identity_snapshot_json'])['proof']
    assert proof['item_version'] == prompt['item_version']
    assert proof['item']['expected_answer_json'] and proof['child_id'] == CHILD
    with db.connect() as c:
        initial = c.total_changes
        c.execute('BEGIN IMMEDIATE')
        identity = historical_identity(c, CHILD, row, 'another-identity')
        assert c.in_transaction and c.total_changes == initial
        with pytest.raises(HistoryIdentityUnavailable):
            historical_identity(c, OTHER, row, identity)
        c.rollback()
        with pytest.raises(sqlite3.IntegrityError, match='DIAGNOSTIC_IDENTITY_IMMUTABLE'):
            c.execute("UPDATE diagnostic_prompt SET identity_snapshot_json='{}' WHERE prompt_id=?", (prompt['prompt_id'],))


@pytest.mark.parametrize('fault', ['json', 'digest', 'scope', 'full_item', 'public_only'])
def test_corrupt_or_substituted_evidence_never_falls_back(flow, fault):
    db, service, sid = flow
    row = saved(db, prepare(service, sid)['prompt'])
    envelope = json.loads(row['identity_snapshot_json'])
    if fault == 'json':
        row['identity_snapshot_json'] = '{broken'
    else:
        if fault == 'digest':
            envelope['sha256'] = '0' * 64
        elif fault == 'scope':
            envelope['proof']['child_id'] = OTHER
        elif fault == 'full_item':
            envelope['proof']['item']['explanation'] = 'not the original'
        else:
            envelope['proof']['item'] = json.loads(row['item_json'])
        if fault != 'digest':
            from home_study_agent.diagnostic_history_identity import _hash
            envelope['sha256'] = _hash(envelope['proof'])
        row['identity_snapshot_json'] = json.dumps(envelope)
    with db.connect() as c, pytest.raises(HistoryIdentityUnavailable):
        historical_identity(c, CHILD, row, 'different')


def test_source_erasure_is_atomic_and_does_not_leave_full_copy(flow):
    db, service, sid = flow
    prompt = prepare(service, sid)['prompt']
    original = saved(db, prompt)['identity_snapshot_json']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE diagnostic_prompt SET item_json='{}',status='invalidated',reason='source_deleted' WHERE prompt_id=?", (prompt['prompt_id'],))
        assert c.execute('SELECT identity_snapshot_json FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()[0] is None
        c.rollback()
    assert saved(db, prompt)['identity_snapshot_json'] == original
    with db.connect() as c:
        c.execute("UPDATE diagnostic_prompt SET item_json='{}',status='invalidated',reason='source_deleted' WHERE prompt_id=?", (prompt['prompt_id'],))
        c.commit()
    DiagnosticPromptService(db)
    row = saved(db, prompt)
    assert row['identity_snapshot_json'] is None
    with db.connect() as c, pytest.raises(HistoryIdentityUnavailable):
        historical_identity(c, CHILD, row, 'different')


def test_legacy_schema_ddl_and_new_prompt_failure_both_rollback(flow):
    db, service, sid = flow
    with db.connect() as c:
        # Before the added column existed: no migration backfill, no implicit commit.
        legacy = sqlite3.connect(':memory:')
        legacy.execute('CREATE TABLE diagnostic_prompt(prompt_id TEXT, item_json TEXT, status TEXT, reason TEXT)')
        legacy.execute("INSERT INTO diagnostic_prompt VALUES('old','{}','cancelled',NULL)")
        legacy.commit()
        legacy.execute('BEGIN IMMEDIATE')
        ensure_schema(legacy)
        assert legacy.in_transaction and legacy.execute('SELECT identity_snapshot_json FROM diagnostic_prompt').fetchone()[0] is None
        legacy.rollback()
        assert 'identity_snapshot_json' not in {r[1] for r in legacy.execute('PRAGMA table_info(diagnostic_prompt)')}
        legacy.close()
        c.execute("CREATE TRIGGER fail_prompt BEFORE INSERT ON diagnostic_prompt BEGIN SELECT RAISE(ABORT,'injected'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        prepare(service, sid)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_prompt')
        c.commit()
    prompt = prepare(service, sid)['prompt']
    assert saved(db, prompt)['identity_snapshot_json']


def test_full_snapshot_returns_same_identity_after_item_metadata_change(flow):
    db, service, sid = flow
    row = saved(db, prepare(service, sid)['prompt'])
    with db.connect() as c:
        identity = historical_identity(c, CHILD, row, 'other')
        assert historical_identity(c, CHILD, row, identity) == identity
        c.execute("UPDATE diagnostic_item SET explanation='changed' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        assert historical_identity(c, CHILD, row, identity) == identity


def test_distinct_item_alias_never_becomes_an_unseen_question(flow):
    db, service, sid = flow
    original = prepare(service, sid)['prompt']
    service.cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
    with db.connect() as c:
        c.execute('''UPDATE diagnostic_item SET prompt=(SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?),
            stimulus_text=(SELECT stimulus_text FROM diagnostic_item WHERE diagnostic_item_id=?),
            stimulus_asset=(SELECT stimulus_asset FROM diagnostic_item WHERE diagnostic_item_id=?)
            WHERE diagnostic_item_id=?''', (ITEM, ITEM, ITEM, SECOND))
        c.commit()
    alias = service.prepare(sid, track='school', preferred_item_id=SECOND, client_action_id='alias')['prompt']
    assert alias['diagnostic_item_id'] != original['diagnostic_item_id']
    assert alias['item_version'] != original['item_version']
    a, b = saved(db, original), saved(db, alias)
    with db.connect() as c:
        identity = historical_identity(c, CHILD, b, 'different')
        assert historical_identity(c, CHILD, a, identity) == identity
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
