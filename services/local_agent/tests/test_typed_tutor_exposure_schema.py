"""Optional typed prompt storage and legacy Tutor column upgrade boundaries."""
from datetime import datetime, timezone

from home_study_agent.database import Database
from home_study_agent.learning_obligations import project_item_exposure
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository


CHILD = 'child-primary-4'
ITEM = 'diag-p4-arithmetic-closed-1'


def family(tmp_path, *, historical=False):
    db = Database(tmp_path / 'typed-schema.db')
    if historical:
        from historical_database_fixture import seed_empty_historical_v84
        seed_empty_historical_v84(db.path)
    else:
        db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    sid = repo.start_session(CHILD, 'minimal-schema-session')['session_id']
    return db, repo, sid


def test_legacy_tutor_column_upgrade_keeps_plain_hint_without_fabricating_typed_history(tmp_path):
    # A Tutor-column projection, not a whole historical v45 database.
    db, repo, sid = family(tmp_path, historical=True)
    with db.connect() as c:
        question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone()[0]
    hint = repo.save_tutor_turn(sid, None, '数学', question, 'Synthetic original hint.',
        'local', 'synthetic', 'legacy-plain-hint')
    with db.connect() as c:
        c.execute('ALTER TABLE tutor_turn DROP COLUMN output_envelope_json')
        c.execute('ALTER TABLE tutor_turn DROP COLUMN ocr_confirmation_id')
        c.execute('DELETE FROM schema_migration WHERE version>=46')
        before = dict(c.execute('SELECT * FROM tutor_turn WHERE turn_id=?', (hint['turn_id'],)).fetchone())
        c.commit()
    db.initialize()
    with db.connect() as c:
        after = dict(c.execute('SELECT * FROM tutor_turn WHERE turn_id=?', (hint['turn_id'],)).fetchone())
        assert {key: after[key] for key in before} == before
        assert after['output_envelope_json'] is after['ocr_confirmation_id'] is None
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone()
        result = project_item_exposure(c, CHILD, ITEM, as_of=datetime.now(timezone.utc).isoformat())
        assert result['tutor_assisted']
        assert result['tutor_refs'][0]['matched_by'] == 'exact_question'


def test_material_typed_audit_without_prompt_storage_is_not_backfilled(tmp_path):
    db, repo, sid = family(tmp_path)
    from home_study_agent.teaching_question_target import VERSION, _digest, _hash
    with db.connect() as c:
        item = dict(c.execute("SELECT * FROM diagnostic_item WHERE stimulus_text IS NOT NULL AND trim(stimulus_text)<>'' LIMIT 1").fetchone())
    target = {'version': VERSION, 'status': 'resolved', 'child_id': CHILD, 'session_id': sid,
        'subject': '英语', 'question_hash': _hash(item['prompt'].strip()),
        'binding': {'diagnostic_item_id': item['diagnostic_item_id'], 'prompt_id': 'missing', 'item_version': 'missing'}}
    target['fingerprint'] = _digest(target)
    repo.save_tutor_turn(sid, None, '英语', item['prompt'], 'Synthetic material hint.',
        'local', 'synthetic', 'unavailable-typed-history',
        output_envelope={'local_teaching_control': {'question_target': target}})
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone()
        before = '\n'.join(c.iterdump())
        result = project_item_exposure(c, CHILD, item['diagnostic_item_id'], as_of=datetime.now(timezone.utc).isoformat())
        assert not result['tutor_assisted']
        assert '\n'.join(c.iterdump()) == before
