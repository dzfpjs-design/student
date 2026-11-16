"""A stored audio pathname is not proof that the captured bytes still exist."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from home_study_agent.database import LocalMediaRuntime
from home_study_agent.school_open_reviews import _anchor, reconcile_school_reviews
from home_study_agent.rubric_calibration_evidence import source_snapshot
from home_study_agent.learning_feedback import build_learning_feedback
from test_school_open_reviews import school_case, CHILD
from test_school_spoken_reviews import spoken_review


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
@pytest.mark.parametrize('fault', ['missing','changed','different_pcm'])
def test_recording_loss_invalidates_score_not_original_answer(school_case, monkeypatch, fault):
    saved, _, _, _, voice = spoken_review(school_case,monkeypatch)
    db = school_case[0]
    db.local_media_runtime = LocalMediaRuntime(school_case[-1].data_dir,school_case[-1].data_encryption_key)
    evidence = saved['attempt']['evidence_id']
    with db.connect() as c:
        before = source_snapshot(c,CHILD,evidence)
        original_answer = c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE evidence_id=?',(evidence,)).fetchone()[0]
        task = c.execute('SELECT review_task_id FROM review_task WHERE source_evidence_id=?',(evidence,)).fetchone()[0]
        path = Path(c.execute('SELECT local_path FROM voice_input WHERE voice_input_id=?',(voice,)).fetchone()[0])
    assert path.is_relative_to(school_case[-1].data_dir)
    if fault == 'missing':
        path.unlink()
    elif fault == 'changed':
        payload = bytearray(path.read_bytes())
        payload[-1] ^= 1
        path.write_bytes(payload)
    else:
        payload = bytearray((school_case[-1].data_dir/'synthetic-runtime.wav').read_bytes())
        payload[-1] ^= 1
        path.write_bytes(payload)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError):
            source_snapshot(c,CHILD,evidence)
        with pytest.raises(ValueError):
            _anchor(c,CHILD,evidence,datetime.now(timezone.utc))
        assert reconcile_school_reviews(c,CHILD)['cancelled'] == [task]
        assert build_learning_feedback(c,CHILD)['nodes'] == []
        attempt = c.execute('SELECT submitted_answer,correctness FROM diagnostic_attempt WHERE evidence_id=?',(evidence,)).fetchone()
        assert attempt[0] == original_answer and attempt[1] is None
        assert before['modality'] == 'original_spoken_text'
        c.commit()


@pytest.mark.parametrize('school_case', ['spoken'], indirect=True)
@pytest.mark.parametrize('fault', ['io','key_unavailable'])
def test_transient_recording_io_does_not_cancel_obligation(school_case,monkeypatch,fault):
    saved, _, _, _, _ = spoken_review(school_case,monkeypatch)
    db = school_case[0]
    with db.connect() as c:
        task = c.execute('SELECT review_task_id FROM review_task WHERE source_evidence_id=?',
            (saved['attempt']['evidence_id'],)).fetchone()[0]
    def unavailable(*args, **kwargs):
        raise PermissionError('Synthetic transient media access')
    with monkeypatch.context() as io:
        if fault == 'io':
            io.setattr('home_study_agent.pet_media_evidence._read_audio',unavailable)
        else:
            io.setattr(db,'local_media_runtime',LocalMediaRuntime(school_case[-1].data_dir,None))
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            with pytest.raises(PermissionError):
                reconcile_school_reviews(c,CHILD)
            c.rollback()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert reconcile_school_reviews(c,CHILD)['retained'] == [task]
        c.commit()
