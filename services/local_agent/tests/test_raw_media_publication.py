"""Synthetic media only: file lifetime follows durable publication, not response reads."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.database import StudyConnection
from home_study_agent.local_media_ai import LocalMediaAI
from test_diagnostic_prompt_http import environment, start


@pytest.fixture
def publication(environment, monkeypatch):
    settings, db = environment
    settings = replace(settings, data_encryption_key=b'R' * 32)
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav',
        lambda *args, **kwargs: ('synthetic answer', 1000, 'test-only', False))
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        service = client.app.state.media_ai
        def publish(kind):
            if kind == 'voice':
                return service.transcribe(sid, b'synthetic wav', 'agent_command', 'raw-publication', True)
            ok, image = cv2.imencode('.jpg', np.full((100, 100, 3), 180, dtype=np.uint8))
            assert ok
            return service.ocr(sid, image.tobytes(), 'raw-publication', True,
                prepared_result={'quality': {'issues': [], 'messages': []}, 'lines': [], 'engine': 'synthetic'})
        yield db, settings.data_dir, service, publish


@pytest.mark.parametrize('kind,table', [('voice', 'voice_input'), ('ocr', 'capture_artifact')])
@pytest.mark.parametrize('failure', ['insert', 'commit'])
def test_failed_publication_removes_only_its_uncommitted_media(publication, monkeypatch, kind, table, failure):
    db, root, service, publish = publication
    retained = root / 'media' / 'other-child' / 'retained.enc'
    retained.parent.mkdir(parents=True)
    retained.write_bytes(b'previous encrypted media')
    if failure == 'insert':
        with db.connect() as c:
            c.execute(f"CREATE TRIGGER reject_raw_publication BEFORE INSERT ON {table} "
                      "BEGIN SELECT RAISE(ABORT, 'synthetic publication failure'); END")
            c.commit()
    else:
        commit = StudyConnection.commit
        def fail_commit(c):
            if c.in_transaction and c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:
                raise sqlite3.OperationalError('synthetic publication failure')
            return commit(c)
        monkeypatch.setattr(StudyConnection, 'commit', fail_commit)
    with pytest.raises(sqlite3.DatabaseError, match='synthetic publication failure'):
        publish(kind)
    with db.connect() as c:
        assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert list((root / 'media').rglob('*.enc')) == [retained]
    assert retained.read_bytes() == b'previous encrypted media'


@pytest.mark.parametrize('kind,table,reader', [
    ('voice', 'voice_input', '_voice_input'), ('ocr', 'capture_artifact', '_artifact')])
def test_response_read_failure_preserves_committed_media_and_same_action_retry(publication, monkeypatch, kind, table, reader):
    db, root, service, publish = publication
    original = getattr(service, reader)
    monkeypatch.setattr(service, reader, lambda *args: (_ for _ in ()).throw(RuntimeError('response read failed')))
    with pytest.raises(RuntimeError, match='response read failed'):
        publish(kind)
    with db.connect() as c:
        path = Path(c.execute(f'SELECT local_path FROM {table}').fetchone()[0])
    assert path.read_bytes().startswith(b'HSC1')
    monkeypatch.setattr(service, reader, original)
    publish(kind)
    assert list((root / 'media').rglob('*.enc')) == [path]
    with db.connect() as c:
        assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 1


def test_partial_file_write_does_not_leave_unindexed_media(publication, monkeypatch):
    db, root, service, publish = publication
    write = Path.write_bytes
    def partial(path, content):
        if path.suffix == '.enc':
            write(path, content[:15])
            raise OSError('synthetic disk full')
        return write(path, content)
    monkeypatch.setattr(Path, 'write_bytes', partial)
    with pytest.raises(OSError, match='synthetic disk full'):
        publish('voice')
    assert not list((root / 'media').rglob('*.enc'))
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM voice_input').fetchone()[0] == 0


@pytest.mark.parametrize('kind,table', [('voice', 'voice_input'), ('ocr', 'capture_artifact')])
def test_commit_acknowledgement_failure_does_not_erase_durable_file(publication, monkeypatch, kind, table):
    db, root, service, publish = publication
    commit = StudyConnection.commit
    def commit_then_fail(c):
        published = c.in_transaction and c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        commit(c)
        if published:
            raise sqlite3.OperationalError('synthetic commit acknowledgement lost')
    monkeypatch.setattr(StudyConnection, 'commit', commit_then_fail)
    try:
        with pytest.raises(sqlite3.OperationalError, match='acknowledgement lost'):
            publish(kind)
    finally:
        monkeypatch.setattr(StudyConnection, 'commit', commit)
    with db.connect() as c:
        path = Path(c.execute(f'SELECT local_path FROM {table}').fetchone()[0])
    assert path.read_bytes().startswith(b'HSC1')
    publish(kind)
    assert list((root / 'media').rglob('*.enc')) == [path]
