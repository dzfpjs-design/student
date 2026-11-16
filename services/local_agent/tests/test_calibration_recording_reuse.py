"""Request-local reuse must recheck managed audio even with unchanged SQLite."""
from pathlib import Path
import os
import json

import pytest

from home_study_agent.database import LocalMediaRuntime
from home_study_agent.rubric_calibration_evidence import source_snapshot
from home_study_agent.school_spoken_score import _recording
from home_study_agent.rubric_calibration_transaction import (
    _CAPTURE, _Transcript, calibration_connection, validation_session,
)
from test_school_open_reviews import school_case, CHILD
from test_school_spoken_reviews import spoken_review


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
@pytest.mark.parametrize('fault', ['missing','same_stat_replacement','key','root','io'])
def test_unchanged_database_does_not_hide_recording_change(school_case,monkeypatch,fault):
    saved, _, _, _, _ = spoken_review(school_case,monkeypatch)
    db = school_case[0]
    with validation_session(), calibration_connection(db) as c:
        c.execute('BEGIN')
        # Full source/file authority is validated live outside dependency capture.
        # This unit tests supported managed-media reuse, not caching C-level proof.
        source = source_snapshot(c,CHILD,saved['attempt']['evidence_id'])
        prompts = c.execute('SELECT * FROM diagnostic_prompt WHERE child_id=? AND attempt_id=?',
            (CHILD,source['attempt_id'])).fetchall()
        assert len(prompts) == 1
        prompt = prompts[0]
        action = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?',
            (prompt['authorized_turn_id'],)).fetchone()[0])
        voice = action['source_voice_input_id']
        expected_hash = c.execute('SELECT wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?',
            (voice,)).fetchone()[0]
        transcript = _Transcript(c)
        token = _CAPTURE.set(transcript)
        try:
            with c.recording(transcript):
                _recording(transcript,prompt,voice,expected_hash)
        finally:
            _CAPTURE.reset(token)
        assert transcript.supported and len(transcript.recordings) == 1
        assert transcript.recording_bytes > 0
        c.verified(transcript)
        assert transcript.compare(c)
        path = Path(next(iter(transcript.recordings)))
        assert path.is_relative_to(school_case[-1].data_dir)
        before = path.stat()
        if fault == 'missing':
            path.unlink()
        elif fault == 'same_stat_replacement':
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            assert path.stat().st_size == before.st_size
        elif fault == 'key':
            c._connection.local_media_runtime = LocalMediaRuntime(school_case[-1].data_dir,None)
        elif fault == 'root':
            c._connection.local_media_runtime = LocalMediaRuntime(school_case[-1].data_dir/'other',school_case[-1].data_encryption_key)
        else:
            def unavailable(*args,**kwargs):
                raise PermissionError('Synthetic transient I/O')
            monkeypatch.setattr('home_study_agent.pet_media_evidence._read_audio',unavailable)
        if fault == 'io':
            with pytest.raises(PermissionError):
                transcript.compare(c)
        else:
            assert transcript.compare(c) is False
        c.rollback()
