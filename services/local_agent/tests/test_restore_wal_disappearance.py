"""Never reinterpret an observed but unread nonempty WAL as safely absent."""
from pathlib import Path

import pytest

from home_study_agent.database import Database
from home_study_agent import registered_asset_snapshot as snapshot
from home_study_agent.restore_publication import initialization_preflight, RestorePublicationError
from test_smtp_owned_journal import settle_synthetic_wal


def test_nonempty_wal_disappearing_before_inspection_is_fail_closed(tmp_path, monkeypatch):
    db = Database(tmp_path / 'synthetic.db')
    db.initialize()
    settle_synthetic_wal(db)
    wal = Path(str(db.path) + '-wal')
    content = b'synthetic nonempty uninspected WAL; never treat it as absent' * 4
    wal.write_bytes(content)
    original = snapshot.HeldFile
    attempts = []
    def disappear(root, relative, policy):
        if relative.endswith('-wal'):
            attempts.append(relative)
            raise FileNotFoundError('synthetic previously observed WAL disappeared')
        return original(root, relative, policy)
    monkeypatch.setattr(snapshot, 'HeldFile', disappear)
    with pytest.raises(RestorePublicationError, match='RESTORE_PUBLICATION_ADMISSION_UNCONFIRMED'):
        with initialization_preflight(db):
            pytest.fail('an unread nonempty WAL was silently ignored')
    assert len(attempts) == 1 and wal.read_bytes() == content
