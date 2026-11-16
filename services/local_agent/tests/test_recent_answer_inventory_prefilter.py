from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from home_study_agent.learning_obligations import recent_same_item_answer, _item_exposure
from home_study_agent import goal_question_inventory as inventory


NOW = datetime(2026, 9, 11, 8, tzinfo=timezone.utc)


@pytest.fixture
def c():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.executescript('''CREATE TABLE diagnostic_attempt(diagnostic_item_id,child_id,created_at,session_id,evidence_id);
        CREATE TABLE daily_session(session_id,child_id);
        CREATE TABLE learning_evidence(evidence_id,child_id,session_id);
        CREATE TABLE mistake_attempt(diagnostic_item_id,child_id,created_at,mistake_id,evidence_id);
        CREATE TABLE mistake_case(mistake_id,child_id);''')
    try:
        yield c
    finally:
        c.close()


@pytest.mark.parametrize('kind', ['diagnostic', 'mistake'])
@pytest.mark.parametrize('age,expected', [(0, True), (1, True), (86400, False), (86401, False), (-1, False)])
def test_same_item_prefilter_has_original_owned_time_window(c, monkeypatch, kind, age, expected):
    stamp = (NOW - timedelta(seconds=age)).isoformat()
    c.execute("INSERT INTO daily_session VALUES('s','child')")
    c.execute("INSERT INTO learning_evidence VALUES('e','child','s')")
    if kind == 'diagnostic':
        c.execute("INSERT INTO diagnostic_attempt VALUES('item','child',?,'s','e')", (stamp,))
    else:
        c.execute("INSERT INTO mistake_case VALUES('m','child')")
        c.execute("INSERT INTO mistake_attempt VALUES('item','child',?,'m','e')", (stamp,))
    monkeypatch.setattr('home_study_agent.learning_obligations._tutor_exposure_index', lambda *_: {})
    monkeypatch.setattr('home_study_agent.learning_obligations._tutor_refs', lambda *_: [])
    original = _item_exposure(c, 'child', 'item', NOW.isoformat(), {}, {})
    assert recent_same_item_answer(c, 'child', 'item', NOW.isoformat()) is expected
    assert original['answer_repeated'] is expected
    assert not recent_same_item_answer(c, 'other', 'item', NOW.isoformat())
    assert not recent_same_item_answer(c, 'child', 'alias-item', NOW.isoformat())


@pytest.mark.parametrize('corrupt', ['session-child', 'evidence-child', 'evidence-session', 'bad-time'])
def test_wrong_ownership_and_malformed_time_do_not_reject_unrelated_candidate(c, corrupt):
    c.execute("INSERT INTO daily_session VALUES('s',?)", ('other' if corrupt == 'session-child' else 'child',))
    c.execute("INSERT INTO learning_evidence VALUES('e',?,?)", (
        'other' if corrupt == 'evidence-child' else 'child', 'wrong' if corrupt == 'evidence-session' else 's'))
    c.execute("INSERT INTO diagnostic_attempt VALUES('item','child',?,'s','e')", (
        'bad-time' if corrupt == 'bad-time' else NOW.isoformat(),))
    assert not recent_same_item_answer(c, 'child', 'item', NOW.isoformat())


def test_prefilter_miss_keeps_full_alias_tutor_checks_and_allow_recent_mode(c, monkeypatch):
    monkeypatch.setattr(inventory, 'validate_item', lambda *_: {'answer_type': 'open_response'})
    calls = []
    monkeypatch.setattr(inventory, 'had_recent_item_exposure', lambda *_: calls.append('full') or True)
    assert inventory.practice_item(c, 'child', 'item', NOW) is None
    assert calls == ['full']
    monkeypatch.setattr(inventory, 'pet_skill_gate', lambda *_: {'modality': 'writing'})
    monkeypatch.setattr(inventory, '_item_source', lambda *_: ({'stage': 'baseline', 'score_qualified': True}, None))
    assert inventory.practice_item(c, 'child', 'item', NOW, allow_recent=True)
    assert calls == ['full']


def test_same_item_reject_avoids_catalog_but_current_source_check_still_runs(c, monkeypatch):
    c.execute("INSERT INTO daily_session VALUES('s','child')")
    c.execute("INSERT INTO learning_evidence VALUES('e','child','s')")
    c.execute("INSERT INTO diagnostic_attempt VALUES('item','child',?,'s','e')", (NOW.isoformat(),))
    checked = []
    monkeypatch.setattr(inventory, 'validate_item', lambda *_: checked.append('source') or {})
    monkeypatch.setattr(inventory, 'had_recent_item_exposure', lambda *_: pytest.fail('unnecessary full catalog'))
    assert inventory.practice_item(c, 'child', 'item', NOW) is None
    assert checked == ['source']


def test_sql_errors_propagate():
    class Broken:
        def execute(self, *_):
            raise sqlite3.OperationalError('synthetic storage')
    with pytest.raises(sqlite3.OperationalError, match='synthetic storage'):
        recent_same_item_answer(Broken(), 'child', 'item', NOW.isoformat())
