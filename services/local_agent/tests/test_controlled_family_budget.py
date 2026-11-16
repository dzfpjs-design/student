"""Family-day accounting of real local reservations/receipts, never media execution."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from home_study_agent import controlled_content, controlled_game
from home_study_agent.controlled_content import ControlledContentError, ControlledContentService
from home_study_agent.controlled_game import ControlledGameError, ControlledGameService
from home_study_agent.database import Database
from test_controlled_content import seed as seed_video
from test_controlled_game import seed as seed_game


@pytest.fixture
def media_clock(monkeypatch):
    state = {'now': datetime.now(timezone.utc)}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state['now'].astimezone(tz) if tz else state['now'].replace(tzinfo=None)
    monkeypatch.setattr(controlled_content, 'datetime', Clock)
    monkeypatch.setattr(controlled_game, 'datetime', Clock)
    return state


@pytest.fixture(params=['content', 'game'])
def media(request, tmp_path, media_clock):
    db = Database(tmp_path / 'budget.db')
    db.initialize()
    kind = request.param
    if kind == 'content':
        sid, resource = seed_video(db, tmp_path / 'video')
        child, service = 'child-media', ControlledContentService(db)
        service.approve(child, resource, 6)
    else:
        executable = tmp_path / 'Synthetic.exe'
        sid, _ = seed_game(db, executable)
        child, service = 'child-game', ControlledGameService(db)
        service.approve(child, 'Synthetic', str(executable), 6)
    service.update_rule(child, enabled=True, max_daily_minutes=1, max_item_minutes=1)
    # Pin scenarios relative to SQLite's real UTC day so old date('now') queries
    # are genuinely exercised without requiring a particular wall-clock hour.
    with db.connect() as c:
        utc_day = date.fromisoformat(c.execute("SELECT date('now')").fetchone()[0])
    base = datetime.combine(utc_day, time.min, tzinfo=timezone.utc)
    media_clock['now'] = base + timedelta(hours=12)
    return db, kind, service, child, sid, media_clock, base


def start(media, key):
    return media[2].start(media[4], '', key)


def close(media, row, seconds, reason='child_requested'):
    return media[2].end(media[4], reason, seconds, **{media[1] + '_session_id': row[media[1] + '_session_id']})


def snapshot(media):
    with media[0].connect() as c:
        return [dict(row) for row in c.execute(f'SELECT * FROM controlled_{media[1]}_session ORDER BY started_at')]


def test_remaining_single_second_cannot_authorize_another_full_minute(media):
    row = start(media, 'first-59-seconds')
    media[5]['now'] += timedelta(seconds=59)
    close(media, row, 59)
    with pytest.raises((ControlledContentError, ControlledGameError)):
        start(media, 'must-not-grant-second-minute')
    assert len(snapshot(media)) == 1


def test_utc_midnight_does_not_reset_same_nanjing_day(media):
    media[5]['now'] = media[6] - timedelta(minutes=1)
    row = start(media, 'before-utc-midnight')
    media[5]['now'] = media[6]
    close(media, row, 60)
    with pytest.raises((ControlledContentError, ControlledGameError)):
        start(media, 'same-family-day-budget-exhausted')


def test_nanjing_midnight_releases_previous_day_without_rewriting_history(media):
    media[5]['now'] = media[6] + timedelta(hours=15, minutes=58)
    row = start(media, 'previous-family-day')
    media[5]['now'] += timedelta(minutes=1)
    close(media, row, 60)
    before = snapshot(media)[0]
    media[5]['now'] = media[6] + timedelta(hours=16, minutes=1)
    new = start(media, 'new-family-day')
    assert new['allowed_minutes'] == 1
    assert snapshot(media)[0] == before


def test_cross_midnight_settled_seconds_are_split_between_family_days(media):
    media[2].update_rule(media[3], enabled=True, max_daily_minutes=2, max_item_minutes=2)
    media[5]['now'] = media[6] + timedelta(hours=15, minutes=58, seconds=30)
    row = start(media, 'cross-family-midnight')
    assert row['allowed_minutes'] == 2
    media[5]['now'] += timedelta(seconds=120)
    close(media, row, 120)
    before = snapshot(media)[0]
    new = start(media, 'new-day-has-30-accounted-seconds')
    assert new['allowed_minutes'] == 1
    assert snapshot(media)[0] == before


def test_crash_reserves_original_grant_without_fabricating_closed_or_actual_duration(media):
    start(media, 'unconfirmed-first-launch')
    media[5]['now'] += timedelta(seconds=2)
    assert media[2].recover_interrupted() == 1
    before = snapshot(media)[0]
    assert before['status'] == 'interrupted' and before['actual_seconds'] == 2
    for key in ('retry-after-crash', 'retry-after-another-restart'):
        assert media[2].recover_interrupted() == 0
        with pytest.raises((ControlledContentError, ControlledGameError)):
            start(media, key)
    assert snapshot(media) == [before]


def test_cross_day_unconfirmed_reservation_also_constrains_next_day_allocation(media):
    media[2].update_rule(media[3], enabled=True, max_daily_minutes=2, max_item_minutes=2)
    media[5]['now'] = media[6] + timedelta(hours=15, minutes=59, seconds=55)
    row = start(media, 'reserves-five-seconds-today-115-tomorrow')
    assert row['allowed_minutes'] == 2
    media[5]['now'] += timedelta(seconds=1)
    media[2].recover_interrupted()
    before = snapshot(media)[0]
    with pytest.raises((ControlledContentError, ControlledGameError)):
        start(media, 'cannot-spend-tomorrow-reservation-again')
    assert snapshot(media) == [before]


def test_subsecond_close_does_not_create_a_free_full_minute(media):
    row = start(media, 'fractional-second-playback')
    media[5]['now'] += timedelta(milliseconds=100)
    ended = close(media, row, 0)
    assert ended['actual_seconds'] == 0  # Historical field remains unchanged.
    with pytest.raises((ControlledContentError, ControlledGameError)):
        start(media, 'whole-minute-would-exceed-by-fraction')


def test_failed_game_launch_does_not_refund_unconfirmed_reservation(media):
    if media[1] != 'game':
        pytest.skip('Only the game service has a real failed-launch end transition')
    row = start(media, 'failed-launch-reservation')
    media[5]['now'] += timedelta(seconds=2)
    ended = close(media, row, 2, reason='launch_failed')
    assert ended['status'] == 'failed'
    with pytest.raises(ControlledGameError):
        start(media, 'failed-launch-is-not-a-free-budget-reset')


def test_legacy_sqlite_utc_receipt_keeps_budget_and_original_serialization(media):
    row = start(media, 'legacy-utc')
    media[5]['now'] += timedelta(seconds=60)
    close(media, row, 60)
    with media[0].connect() as c:
        c.execute(f'''UPDATE controlled_{media[1]}_session SET
            started_at=strftime('%Y-%m-%d %H:%M:%S',started_at),
            ends_at=strftime('%Y-%m-%d %H:%M:%S',ends_at),
            ended_at=strftime('%Y-%m-%d %H:%M:%S',ended_at)''')
        c.commit()
    before = snapshot(media)
    with pytest.raises((ControlledContentError, ControlledGameError)):
        start(media, 'legacy-utc-budget-still-exhausted')
    assert snapshot(media) == before


@pytest.mark.parametrize('invalid', ['not-a-time', '2026-09-10T12:00:00'])
def test_unverifiable_own_history_blocks_only_own_budget_without_repair(media, invalid):
    row = start(media, 'invalid-history-original')
    media[5]['now'] += timedelta(seconds=60)
    close(media, row, 60)
    table = f'controlled_{media[1]}_session'
    with media[0].connect() as c:
        c.execute(f'UPDATE {table} SET started_at=?', (invalid,))
        c.commit()
    before = snapshot(media)
    with pytest.raises((ControlledContentError, ControlledGameError), match='历史.*额度'):
        start(media, 'cannot-guess-naive-or-malformed')
    with media[0].connect() as c:
        assert controlled_content._daily_budget_minutes(
            c, table, 'child-junior-3', media[5]['now'], 1) == 1
    assert snapshot(media) == before
