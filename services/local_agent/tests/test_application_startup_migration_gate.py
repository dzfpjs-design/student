import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from home_study_agent.app import create_app
from home_study_agent.database import Database
from test_mistake_voice_journey import settings


def test_failed_database_gate_cannot_create_auxiliary_stores(tmp_path, monkeypatch):
    root = tmp_path / 'unopened-family'
    config = settings(root)
    calls = []
    class SyntheticGateFailure(RuntimeError):
        pass
    def fail(_self, *, startup_wait_seconds=0):
        assert startup_wait_seconds == 120
        calls.append(True)
        raise SyntheticGateFailure('synthetic upgrade failure')
    monkeypatch.setattr(Database, 'initialize', fail)
    with pytest.raises(SyntheticGateFailure):
        create_app(config)
    assert len(calls) == 1
    assert not root.exists()


def test_database_gate_precedes_service_construction_and_is_not_repeated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.knowledge_graph.host_diagnostic_bridge import HostDiagnosticKnowledgeGraphBridge
    calls = []
    initialize = Database.initialize
    construct = HostDiagnosticKnowledgeGraphBridge.__init__
    def init(database, *, startup_wait_seconds=0):
        assert startup_wait_seconds == 120
        calls.append('database')
        return initialize(database, startup_wait_seconds=startup_wait_seconds)
    def service(self, database, *args, **kwargs):
        assert calls == ['database']
        calls.append('service')
        return construct(self, database, *args, **kwargs)
    monkeypatch.setattr(Database, 'initialize', init)
    monkeypatch.setattr(HostDiagnosticKnowledgeGraphBridge, '__init__', service)
    application = create_app(settings(tmp_path))
    assert calls == ['database', 'service']
    with TestClient(application) as client:
        assert client.get('/v1/health').status_code == 200
    assert calls == ['database', 'service']


def test_newer_database_is_rejected_before_any_service_store_is_created(tmp_path):
    from home_study_agent.database import LATEST_SCHEMA_VERSION, UnsupportedDatabaseVersionError
    config = settings(tmp_path)
    with closing(sqlite3.connect(config.database_path)) as c:
        c.execute('CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at TEXT)')
        c.execute('INSERT INTO schema_migration VALUES (?, ?)', (LATEST_SCHEMA_VERSION + 1, 'synthetic'))
        c.commit()
    original = config.database_path.read_bytes()
    before = set(tmp_path.iterdir())
    with pytest.raises(UnsupportedDatabaseVersionError):
        create_app(config)
    assert config.database_path.read_bytes() == original
    assert set(tmp_path.iterdir()) == before


def test_failed_upgrade_restores_original_before_auxiliary_service_construction(tmp_path, monkeypatch):
    from home_study_agent.database import LATEST_SCHEMA_VERSION
    config = settings(tmp_path)
    with closing(sqlite3.connect(config.database_path)) as c:
        c.execute('CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at TEXT)')
        c.execute('INSERT INTO schema_migration VALUES (?, ?)', (LATEST_SCHEMA_VERSION - 1, 'synthetic'))
        c.execute('CREATE TABLE original_response (text TEXT)')
        c.execute("INSERT INTO original_response VALUES ('saved synthetic response')")
        c.commit()
    def broken(database):
        with database.connect() as c:
            c.execute('CREATE TABLE interrupted_upgrade (value TEXT)')
            c.execute("DELETE FROM original_response")
            c.commit()
        raise RuntimeError('synthetic committed migration failure')
    monkeypatch.setattr(Database, '_initialize_schema', broken)
    with pytest.raises(RuntimeError, match='synthetic committed migration failure'):
        create_app(config)
    with closing(sqlite3.connect(config.database_path)) as c:
        assert c.execute('SELECT text FROM original_response').fetchone()[0] == 'saved synthetic response'
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('interrupted_upgrade', 'ocr_review_item')").fetchone()
    assert not (tmp_path / 'cpu-resource-queue.db').exists()
    assert list((tmp_path / 'upgrade-backups').glob('pre-upgrade-*.json'))


@pytest.mark.parametrize('stored_mode,requested_mode', [
    ('trial', 'formal'), ('formal', 'trial'), ('invalid', 'formal'),
])
def test_scope_rejection_precedes_auxiliary_writes(tmp_path, stored_mode, requested_mode):
    from home_study_agent.database import LATEST_SCHEMA_VERSION
    from home_study_agent.runtime_data_scope import RuntimeDataScopeError
    config = replace(settings(tmp_path), runtime_mode=requested_mode)
    with closing(sqlite3.connect(config.database_path)) as c:
        c.execute('CREATE TABLE schema_migration (version INTEGER PRIMARY KEY, applied_at TEXT)')
        c.execute('INSERT INTO schema_migration VALUES (?, ?)', (LATEST_SCHEMA_VERSION, 'synthetic'))
        c.execute('CREATE TABLE runtime_data_scope (mode TEXT)')
        c.execute('INSERT INTO runtime_data_scope VALUES (?)', (stored_mode,))
        c.commit()
    original = config.database_path.read_bytes()
    before = set(tmp_path.iterdir())
    with pytest.raises(RuntimeDataScopeError):
        create_app(config)
    assert config.database_path.read_bytes() == original
    assert set(tmp_path.iterdir()) == before


def test_corrupt_database_is_not_replaced_by_auxiliary_bootstrap(tmp_path):
    config = settings(tmp_path)
    original = b'synthetic corrupt database; preserve for explicit recovery'
    config.database_path.write_bytes(original)
    before = set(tmp_path.iterdir())
    with pytest.raises(sqlite3.DatabaseError):
        create_app(config)
    assert config.database_path.read_bytes() == original
    assert set(tmp_path.iterdir()) == before
