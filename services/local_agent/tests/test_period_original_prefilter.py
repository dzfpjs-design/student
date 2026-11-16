"""Research labels lack original-review authority; prefilter never grants it."""
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from home_study_agent import period_written_observations as observations


@pytest.fixture
def case(tmp_path):
    c = sqlite3.connect(tmp_path / 'precheck.db')
    c.row_factory = sqlite3.Row
    c.executescript('''CREATE TABLE rubric_assessment(assessment_id TEXT,child_id TEXT,step_id TEXT,payload_json TEXT);
        CREATE TABLE agent_step(step_id TEXT,plan_id TEXT,arguments_json TEXT);
        CREATE TABLE agent_plan(plan_id TEXT,child_id TEXT);
        CREATE TABLE rubric_review_event(event_id TEXT,assessment_id TEXT,revision INTEGER,operation TEXT,payload_json TEXT);
        CREATE TABLE period_report_evidence_state(child_id TEXT,evidence_id TEXT,is_retracted INTEGER);
        CREATE TABLE review_task(child_id TEXT,result_evidence_id TEXT,source_evidence_id TEXT);
        CREATE TABLE mistake_attempt(child_id TEXT,evidence_id TEXT,mistake_id TEXT);
        CREATE TABLE mistake_case(mistake_id TEXT,child_id TEXT,source_evidence_id TEXT);''')
    c.execute('INSERT INTO agent_plan VALUES(?,?)', ('plan', 'child'))
    c.execute('INSERT INTO agent_step VALUES(?,?,?)', ('step', 'plan', json.dumps({'evidence_id': 'answer'})))
    c.execute('INSERT INTO rubric_assessment VALUES(?,?,?,?)',
        ('assessment', 'child', 'step', json.dumps({'source_evidence_id': 'answer'})))
    row = dict(evidence_id='answer', evidence_ref='opaque', subject='math', title='roots',
        occurred_at='2026-09-01T00:00:00+00:00')
    try:
        yield c, row
    finally:
        c.rollback()
        c.close()


def read(c, row):
    return observations.qualified_written_observations(c, 'child', [row],
        now=datetime(2026, 9, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize('operation,profile', [('approve', None), ('retract', 'school_open_written_v1')])
def test_research_label_or_retracted_original_never_enters_expensive_validation(case, monkeypatch, operation, profile):
    c, row = case
    c.execute('INSERT INTO rubric_review_event VALUES(?,?,?,?,?)',
        ('review', 'assessment', 1, operation, json.dumps({'review_profile': profile})))
    def forbidden(*args, **kwargs):
        pytest.fail('Known missing original-review authority still scanned as a learning original')
    monkeypatch.setattr(observations, 'written_score_candidates', forbidden)
    assert read(c, row) == []


@pytest.mark.parametrize('profile', [None, 'school_open_written_v1', 'pet_writing_text_v1'])
def test_possible_original_still_requires_live_deep_validation(case, monkeypatch, profile):
    c, row = case
    if profile:
        c.execute('INSERT INTO rubric_review_event VALUES(?,?,?,?,?)',
            ('review', 'assessment', 1, 'approve', json.dumps({'review_profile': profile})))
    visits = []
    def live(*args, **kwargs):
        visits.append(True)
        raise ValueError('Invalid actual source or review binding')
    monkeypatch.setattr(observations, 'written_score_candidates', live)
    assert read(c, row) == [] and visits == [True]


def test_same_connection_new_human_review_and_retraction_are_immediate(case, monkeypatch):
    c, row = case
    visits = []
    monkeypatch.setattr(observations, 'written_score_candidates', lambda *a, **kw: visits.append(True) or [])
    c.execute('INSERT INTO rubric_review_event VALUES(?,?,?,?,?)', ('blind', 'assessment', 1, 'approve', '{}'))
    assert read(c, row) == [] and visits == []
    c.execute('INSERT INTO rubric_review_event VALUES(?,?,?,?,?)',
        ('original', 'assessment', 2, 'approve', '{"review_profile":"school_open_written_v1"}'))
    assert read(c, row) == [] and visits == [True]
    c.execute('INSERT INTO rubric_review_event VALUES(?,?,?,?,?)', ('withdraw', 'assessment', 3, 'retract', '{}'))
    assert read(c, row) == [] and visits == [True]


def test_other_child_and_malformed_payload_cannot_supply_an_original(case, monkeypatch):
    c, row = case
    c.execute('UPDATE rubric_assessment SET child_id=?', ('other-child',))
    monkeypatch.setattr(observations, 'written_score_candidates', lambda *a, **kw: pytest.fail('cross-child prefilter'))
    assert read(c, row) == []


def test_corrupt_payload_origin_step_still_reaches_history_guard(case, monkeypatch):
    c, row = case
    c.execute("UPDATE rubric_assessment SET payload_json='not-json'")
    seen = []
    def reject(*args, **kwargs):
        seen.append(True)
        raise ValueError('Corrupt assessment history')
    monkeypatch.setattr(observations, 'written_score_candidates', reject)
    assert read(c, row) == [] and seen == [True]
