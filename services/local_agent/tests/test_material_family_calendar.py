from datetime import date, datetime, timezone

import pytest

from home_study_agent import family_clock, curriculum_supply, open_response_materials
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.database import Database
from home_study_agent.local_supply import LocalSupplyError, validate_item
from test_open_response_supply_http import CHILD, prepared, publish_open


def clock(monkeypatch, hour):
    instant = datetime(2026, 9, 9, hour, 0, tzinfo=timezone.utc)

    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    class HostDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 9)

    monkeypatch.setattr(family_clock, 'datetime', Clock)
    for module in (curriculum_supply, open_response_materials):
        monkeypatch.setattr(module, 'date', HostDate, raising=False)


@pytest.mark.parametrize('hour,first,last,allowed', [
    (15, None, '2026-09-09', True),
    (16, None, '2026-09-09', False),
    (16, '2026-09-10', None, True),
])
@pytest.mark.parametrize('gate', ['material', 'question'])
def test_source_effective_dates_use_the_childs_calendar(tmp_path, monkeypatch, hour, first, last, allowed, gate):
    db, supply, spec, _, source_id = prepared(tmp_path, '物理')
    if gate == 'material':
        imported = KnowledgeBaseService(db).import_source(CHILD, dict(source_type='school_material',
            title='Isolated calendar material', subject='物理', grade_band='初三', version_label='calendar',
            source_ref='isolated:calendar', license_status='family_owned_index', parent_reviewed=True,
            content_text='Both conductors have the same cross-sectional area.'))
        source_id = imported['source_id']
    with db.connect() as c:
        c.execute('UPDATE knowledge_source SET effective_from=?,effective_to=? WHERE source_id=?',
                  (first, last, source_id))
        excerpt_id = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (source_id,)).fetchone()[0]
        c.commit()
    clock(monkeypatch, hour)
    if gate == 'material':
        with db.connect() as c:
            c.execute('BEGIN')
            if allowed:
                result = open_response_materials.bind_material_excerpt(c, child_id=CHILD, excerpt_id=excerpt_id, material_key='given')
                assert result['source_id'] == source_id
            else:
                with pytest.raises(LocalSupplyError, match='OPEN_RESPONSE_MATERIAL_SOURCE_EXPIRED'):
                    open_response_materials.bind_material_excerpt(c, child_id=CHILD, excerpt_id=excerpt_id, material_key='given')
    elif allowed:
        assert publish_open(supply, spec)['state'] == 'published'
    else:
        result = supply.generate_draft(CHILD, spec, author='author', action_id='calendar-draft')
        assert result['state'] == 'blocked'
        assert 'CURRICULUM_TEXTBOOK_EXPIRED' in result['blockers']


def test_published_source_expires_at_family_midnight_without_rewriting_history(tmp_path, monkeypatch):
    db, supply, spec, _, source_id = prepared(tmp_path, '物理')
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET effective_to='2026-09-09' WHERE source_id=?", (source_id,))
        c.commit()
    clock(monkeypatch, 15)
    published = publish_open(supply, spec)
    item_id = published['diagnostic_item_id']
    with db.connect() as c:
        assert validate_item(c, CHILD, item_id)['diagnostic_item_id'] == item_id
        source_before = tuple(c.execute('SELECT * FROM knowledge_source WHERE source_id=?', (source_id,)).fetchone())
        events_before = [tuple(row) for row in c.execute('SELECT * FROM curriculum_supply_event ORDER BY rowid')]
    clock(monkeypatch, 16)
    # The host still reports September 9; the family has crossed midnight.
    with Database(db.path).connect() as c:
        c.execute('BEGIN')
        c.execute('PRAGMA query_only=ON')
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, item_id)
        assert tuple(c.execute('SELECT * FROM knowledge_source WHERE source_id=?', (source_id,)).fetchone()) == source_before
        assert [tuple(row) for row in c.execute('SELECT * FROM curriculum_supply_event ORDER BY rowid')] == events_before
