"""Independent semantic service initialization owns its media schema dependencies."""
import sqlite3

import pytest

from home_study_agent.controlled_voice import resolve, snapshot
from home_study_agent.database import Database
from home_study_agent.voice_control_intent import VoiceControlIntent
from home_study_agent.config import Settings
from home_study_agent.repository import StudyRepository
from test_agent_continuation import Gateway, orchestrator_for
from test_agent_runtime import seed_session


@pytest.mark.parametrize('entry', ['orchestrator', 'voice_control'])
def test_cold_service_initializes_dependencies_without_granting_media(tmp_path, entry):
    db = Database(tmp_path / 'cold.db')
    db.initialize()
    sid = seed_session(db)
    with db.connect() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='controlled_content_rule'").fetchone() is None
    if entry == 'orchestrator':
        agent, gateway = orchestrator_for(db, tmp_path, {})
        service = agent.voice_control_intent
    else:
        gateway = Gateway({})
        service = VoiceControlIntent(db, StudyRepository(db), gateway,
                                     Settings(data_dir=tmp_path, database_path=db.path))
        service.initialize()
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        before = snapshot(c, 'child-runtime', sid)
        for kind, control in [('content', 'controlled_content.play'), ('games', 'controlled_game.play')]:
            assert before[kind]['rule'] is None
            assert before[kind]['sources'] == []
            assert before[kind]['activities'] == []
            with pytest.raises(ValueError, match='^CONTROL_EXTERNAL_PERMISSION_REQUIRED$'):
                resolve(c, 'child-runtime', sid, control, {'query': 'anything'}, before)
        assert c.total_changes == 0
    # Reopening initialization is not authority to add rules, grants or activities.
    service.initialize()
    with Database(db.path).connect() as c:
        assert snapshot(c, 'child-runtime', sid) == before
        assert c.execute('SELECT COUNT(*) FROM voice_control_intent_request').fetchone()[0] == 0
    assert gateway.requests == []


def test_initialized_schema_damage_is_not_treated_as_empty_permission(tmp_path):
    db = Database(tmp_path / 'damaged.db')
    db.initialize()
    sid = seed_session(db)
    orchestrator_for(db, tmp_path, {})
    with db.connect() as c:
        c.execute('DROP TABLE controlled_content_rule')
        c.commit()
        with pytest.raises(sqlite3.OperationalError, match='no such table: controlled_content_rule'):
            snapshot(c, 'child-runtime', sid)
