"""Real public source deletion must drain a transient original SQL reader."""
import threading

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent import restore_publication as publication
from test_knowledge_source_revision_migration import old_database
from test_knowledge_source_revision_http import parent


def test_source_delete_waits_for_original_reader_without_replaying_business(tmp_path, monkeypatch):
    database = old_database(tmp_path)
    database.initialize()
    settings = Settings(data_dir=tmp_path, database_path=database.path, maintenance_enabled=False)
    opened, observed_busy, release = (threading.Event() for _ in range(3))
    errors = []
    original = publication._writer_precheck
    gate = publication._gate(publication._path(database.path))

    def observed_original(checked_gate, actor):
        try:
            return original(checked_gate, actor)
        except publication.RestorePublicationError as error:
            if checked_gate is gate and error.args == ('RESTORE_PUBLICATION_BUSY',):
                observed_busy.set()
            raise

    with TestClient(create_app(settings), client=('127.0.0.1', 53007)) as client:
        headers = parent(client, True)
        monkeypatch.setattr(publication, '_writer_precheck', observed_original)

        def reader():
            try:
                with database.connect() as connection:
                    assert connection.execute('SELECT COUNT(*) FROM child_profile').fetchone()[0] == 2
                    opened.set()
                    assert observed_busy.wait(3), 'original admission never observed held reader'
                release.set()
            except BaseException as error:
                errors.append(error)
                release.set()

        thread = threading.Thread(target=reader)
        thread.start()
        assert opened.wait(2)
        try:
            response = client.post('/v1/parent/children/child-primary-4/knowledge-sources/old/delete',
                headers=headers, json={'confirmation': '确认删除', 'client_action_id': 'original-reader-delete'})
        finally:
            observed_busy.set()
            thread.join(4)
        assert not thread.is_alive() and not errors
        assert release.is_set() and observed_busy.is_set()
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed', response.text
        repeated = client.post('/v1/parent/children/child-primary-4/knowledge-sources/old/delete',
            headers=headers, json={'confirmation': '确认删除', 'client_action_id': 'original-reader-delete'})
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['deletion_id'] == response.json()['deletion_id']
    with database.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM deletion_job').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM child_profile').fetchone()[0] == 2
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
