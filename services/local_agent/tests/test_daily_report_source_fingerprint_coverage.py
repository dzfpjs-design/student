"""Synthetic adversarial changes outside the preview's display limits."""
from hashlib import sha256
import json

import pytest
from home_study_agent import daily_report_observation
from home_study_agent.repository import utc_now
from test_daily_report_candidate import synthetic


@pytest.mark.parametrize('prefix_length', [1200, 12000])
def test_full_ocr_content_change_invalidates_fingerprint(synthetic, prefix_length):
    db, day = synthetic
    original = '一' * prefix_length + '甲'
    changed = '一' * prefix_length + '乙'
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text=? WHERE artifact_id='art'", (original,))
        c.execute("UPDATE document_page_revision SET content_sha256=? WHERE artifact_id='art'", (sha256(original[:12000].encode()).hexdigest(),))
        c.commit()
        daily_report_observation.prepare(c, 'a', day, 'recipient-a')
        c.execute("UPDATE capture_artifact SET extracted_text=? WHERE artifact_id='art'", (changed,))
        c.execute("UPDATE document_page_revision SET content_sha256=? WHERE artifact_id='art'", (sha256(changed[:12000].encode()).hexdigest(),))
        c.commit()
        assert not daily_report_observation.inspect_current(c, 'a', day, 'recipient-a')['current']


def test_full_event_metadata_is_bound(synthetic):
    db, day = synthetic
    with db.connect() as c:
        c.execute("""INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at)
            VALUES('event','a','s','writing_like',.9,'synthetic',0,'synthetic',0,?)""", (utc_now(),))
        c.commit()
        daily_report_observation.prepare(c, 'a', day, 'recipient-a')
        c.execute("UPDATE child_state_event SET confidence=.2 WHERE state_event_id='event'")
        c.commit()
        assert not daily_report_observation.inspect_current(c, 'a', day, 'recipient-a')['current']


def test_change_beyond_first_100_samples_invalidates_fingerprint(synthetic):
    db, day = synthetic
    now = utc_now()
    with db.connect() as c:
        for n in range(101):
            c.execute("""INSERT INTO child_state_event(state_event_id,child_id,session_id,state,confidence,reason,duration_ms,model_version,shadow,observed_at)
                VALUES(?,'a','s','writing_like',.9,'synthetic',0,'synthetic',0,?)""", (f'e-{n:04d}', now))
        c.commit()
        daily_report_observation.prepare(c, 'a', day, 'recipient-a')
        source_ids = json.loads(c.execute('SELECT source_ids_json FROM daily_observation_report_source').fetchone()[0])
        assert any(value == 'e-0100' for _, value in source_ids)
        c.execute("UPDATE child_state_event SET state_event_id='z-replacement' WHERE state_event_id='e-0100'")
        c.commit()
        assert not daily_report_observation.inspect_current(c, 'a', day, 'recipient-a')['current']


def test_full_question_content_change_invalidates_fingerprint(synthetic):
    db, day = synthetic
    first = '一' * 1200 + '甲'
    second = '一' * 1200 + '乙'
    page = first + '\n' + second
    now = utc_now()
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text=? WHERE artifact_id='art'", (page,))
        c.execute("UPDATE document_page_revision SET content_sha256=? WHERE artifact_id='art'", (sha256(page.encode()).hexdigest(),))
        revision = c.execute("SELECT revision_id FROM document_page_revision WHERE artifact_id='art'").fetchone()[0]
        c.execute("""INSERT INTO observed_question(question_id,child_id,question_fingerprint,canonical_text,first_revision_id,latest_revision_id,latest_artifact_id,first_seen_at,last_seen_at)
          VALUES('q','a',?,?,?,?,'art',?,?)""", (sha256(first.encode()).hexdigest(),first,revision,revision,now,now))
        c.execute("""INSERT INTO observed_question_occurrence(occurrence_id,question_id,child_id,revision_id,artifact_id,confidence,observed_at)
          VALUES('o','q','a',?,'art',.9,?)""", (revision,now))
        c.commit()
        daily_report_observation.prepare(c, 'a', day, 'recipient-a')
        c.execute("UPDATE observed_question SET canonical_text=?,question_fingerprint=? WHERE question_id='q'", (second,sha256(second.encode()).hexdigest()))
        c.commit()
        assert not daily_report_observation.inspect_current(c, 'a', day, 'recipient-a')['current']
