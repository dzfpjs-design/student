"""Actual signed synthetic protocol: reused authority still depends on live media."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from home_study_agent.database import LocalMediaRuntime
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_transaction import (
    assert_prepared_dependencies, calibration_connection, validation_session,
)
from test_school_spoken_signed_replay import checkpoint_runtime


@pytest.mark.parametrize('fault', ['same_stat', 'late_media', 'key', 'io', 'trust', 'expiry', 'revoke', 'writer'])
def test_signed_oral_reuse_does_not_outlive_authority(tmp_path,monkeypatch,fault):
    meta, settings, db = checkpoint_runtime(tmp_path,monkeypatch)
    service = RubricCalibrationService(db)
    now = datetime.now(timezone.utc)
    reads = []
    real_read = service._read_rows
    def counted(*args,**kwargs):
        reads.append(True)
        return real_read(*args,**kwargs)
    monkeypatch.setattr(service,'_read_rows',counted)
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        row = service._owned(c,meta['child_id'],meta['protocol_id'])
        first = service._read(c,row,now)
        assert first['score_qualified']
        assert service._read(c,row,now)['score_qualified']
        assert len(reads) == 1, 'Second read must exercise actual reuse'
        audio = Path(c.execute('SELECT local_path FROM voice_input WHERE child_id=? ORDER BY voice_input_id LIMIT 1',
            (meta['child_id'],)).fetchone()[0])
        assert audio.resolve().is_relative_to(settings.data_dir)
        original, stat = audio.read_bytes(), audio.stat()
        changes = c.total_changes
        def replace_media():
            changed = bytearray(original)
            changed[-1] ^= 1
            audio.write_bytes(changed)
            os.utime(audio,ns=(stat.st_atime_ns,stat.st_mtime_ns))
            assert audio.stat().st_size == stat.st_size
        try:
            if fault == 'same_stat':
                replace_media()
            elif fault == 'late_media':
                live = service._live
                def changed_after_live(*args,**kwargs):
                    result = live(*args,**kwargs)
                    replace_media()
                    return result
                monkeypatch.setattr(service,'_live',changed_after_live)
            elif fault == 'key':
                c._connection.local_media_runtime = LocalMediaRuntime(settings.data_dir,None)
            elif fault == 'io':
                def unavailable(*args,**kwargs):
                    raise PermissionError('Synthetic transient I/O')
                monkeypatch.setattr('home_study_agent.pet_media_evidence._read_audio',unavailable)
            elif fault == 'trust':
                trust = tmp_path/'revoked-trust.json'
                trust.write_text(json.dumps({'version':'rubric_authority_trust_v1','issuers':[]}))
                monkeypatch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE',str(trust))
            elif fault == 'expiry':
                now += timedelta(days=2)
            elif fault == 'writer':
                c.execute('UPDATE voice_input SET local_path=? WHERE local_path=?',
                    (str(settings.data_dir/'missing.wav'),str(audio)))
                with pytest.raises(ValueError,match='PREPARED_EVIDENCE_CHANGED'):
                    assert_prepared_dependencies(c,{'protocol_id':meta['protocol_id']})
            else:
                c.rollback()
                result = service.revoke(meta['child_id'],meta['protocol_id'],
                    expected_revision=first['revision'],client_action_id='synthetic-reuse-revoke')
                assert not result['current']['score_qualified']
                c.execute('BEGIN')
                row = service._owned(c,meta['child_id'],meta['protocol_id'])
            if fault in {'key','io'}:
                with pytest.raises(PermissionError):
                    service._read(c,row,now)
            elif fault == 'trust':
                with pytest.raises(ValueError,match='TRUST|AUTHORITY'):
                    service._read(c,row,now)
            else:
                assert not service._read(c,row,now)['score_qualified']
            assert c.total_changes == changes + (1 if fault == 'writer' else 0)
        finally:
            if fault in {'same_stat','late_media'}:
                audio.write_bytes(original)
                os.utime(audio,ns=(stat.st_atime_ns,stat.st_mtime_ns))
            c.rollback()
