"""Local behavior input binding, without report/snapshot circular dependencies."""
import hashlib
import json
import sqlite3
from uuid import uuid4

from .behavior_model.report_dependencies import REPORT_DEPENDENCY_VERSION, habit_dependencies, ownership_issues


def daily_behavior_fingerprint(c, child_id, session_id):
    return _daily_behavior_fingerprint(c, child_id, session_id)


def _daily_behavior_fingerprint(c, child_id, session_id, *, habits=None, habit_json=None):
    from .behavior_model.service import DAILY_PROJECTION_VERSION

    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    payload = {'version': 'daily_behavior_source_v1', 'child_id': child_id,
               'session_id': session_id, 'projection_version': DAILY_PROJECTION_VERSION,
               'dependencies_version': REPORT_DEPENDENCY_VERSION}

    def rows(table, sql, args):
        if table not in tables:
            return []
        from .report_source_reads import report_dict_rows
        return report_dict_rows(c, sql, args)

    payload['habits'] = habit_dependencies(c, child_id, tables) if habits is None else habits
    payload['ownership_issues'] = ownership_issues(c, child_id, session_id)
    scope = 'oe.child_id=? AND oe.session_id IS ?'
    args = (child_id, session_id)
    payload['observations'] = rows('observable_event',
        f'SELECT oe.* FROM observable_event oe WHERE {scope} ORDER BY oe.event_id', args)
    for table, order in [('probabilistic_state', 'state_id'), ('behavior_retraction', 'event_id')]:
        payload[table] = rows(table, f'''SELECT v.* FROM {table} v
            JOIN observable_event oe ON oe.event_id=v.event_id WHERE {scope} ORDER BY v.{order}''', args) if 'observable_event' in tables else []
    payload['interventions'] = rows('intervention', '''SELECT intervention_id,child_id,session_id,source_event_id,status,
        feedback,feedback_at,feedback_note FROM intervention WHERE child_id=? AND session_id=?
        ORDER BY intervention_id''', (child_id, session_id))
    if 'intervention' in tables:
        from .behavior_model.intervention_withdrawal import PENDING_REASON
        pending = rows('intervention', '''SELECT intervention_id FROM intervention WHERE child_id=? AND session_id=?
            AND status='active' AND withdrawal_reason=? ORDER BY intervention_id''',
            (child_id, session_id, PENDING_REASON))
        if pending:
            payload['pending_intervention_withdrawals'] = pending
    payload['feedback_sources'] = rows('observable_event', '''SELECT oe.* FROM observable_event oe
        WHERE EXISTS(SELECT 1 FROM intervention i WHERE i.child_id=? AND i.session_id IS ?
        AND i.source_event_id=oe.event_id) ORDER BY oe.event_id''', args) if 'intervention' in tables else []
    payload['legacy_feedback'] = rows('intervention_record', '''SELECT intervention_id,trigger_state,
        child_response,confidence,COALESCE(responded_at,created_at) AS occurred_at
        FROM intervention_record WHERE child_id=? AND child_response IS NOT NULL
        ORDER BY COALESCE(responded_at,created_at) DESC,intervention_id LIMIT 3''', (child_id,))
    if habit_json is None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()
    else:
        # Identical canonical JSON bytes; only the shared raw subtree is encoded
        # once. Each session still reads and serializes its own live sources.
        encoded = ('{' + ','.join(json.dumps(key) + ':' + (habit_json if key == 'habits'
            else json.dumps(payload[key], ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False)) for key in sorted(payload)) + '}').encode()
    return hashlib.sha256(encoded).hexdigest()


def _with_daily_behavior_batch(c, child_id, consume):
    return _with_behavior_source_batch(c, child_id, lambda daily, period: consume(daily))


def _with_behavior_source_batch(c, child_id, consume):
    """Share raw habit proof only inside this synchronous borrowed transaction."""
    from .period_behavior_source import period_behavior_fingerprint, _period_behavior_fingerprint
    from .report_source_reads import report_read_connection, report_read_epoch
    if not report_read_connection(c) or not c.in_transaction:
        return consume(daily_behavior_fingerprint, period_behavior_fingerprint)
    savepoint = 'behavior_read_' + uuid4().hex
    epoch = report_read_epoch(c)
    if epoch is None:
        try:
            c.execute('SAVEPOINT ' + savepoint)
        except sqlite3.DatabaseError as error:
            if str(error) == 'not authorized':
                return consume(daily_behavior_fingerprint, period_behavior_fingerprint)
            raise
    def same_transaction():
        return c.in_transaction and (epoch is None or report_read_epoch(c) == epoch)
    active = True
    stable = False
    changes = c.total_changes
    try:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        proof = habit_dependencies(c, child_id, tables)
        proof_json = json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

        def read(connection, child, session):
            if (not active or connection is not c or child != child_id
                    or not same_transaction() or c.total_changes != changes):
                return daily_behavior_fingerprint(connection, child, session)
            return _daily_behavior_fingerprint(c, child, session, habits=proof, habit_json=proof_json)

        def read_period(connection, child, start, end):
            if (not active or connection is not c or child != child_id
                    or not same_transaction() or c.total_changes != changes):
                return period_behavior_fingerprint(connection, child, start, end)
            return _period_behavior_fingerprint(c, child, start, end, habits=proof)

        result = consume(read, read_period)
        # Recheck the proof, including its live time/source checks. No cached
        # qualification or external source authority survives the callback.
        tables_after = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        stable = (same_transaction() and c.total_changes == changes and tables_after == tables
                  and json.dumps(habit_dependencies(c, child_id, tables_after), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':'), allow_nan=False) == proof_json
                  and c.total_changes == changes)
    finally:
        active = False
        if epoch is not None:
            if not same_transaction():
                raise RuntimeError('BEHAVIOR_READ_TRANSACTION_CHANGED')
        else:
            try:
                c.execute('RELEASE SAVEPOINT ' + savepoint)
            except sqlite3.OperationalError as error:
                if 'no such savepoint' in str(error):
                    raise RuntimeError('BEHAVIOR_READ_TRANSACTION_CHANGED') from error
                raise
    return result if stable else consume(daily_behavior_fingerprint, period_behavior_fingerprint)


def refreshed_daily_behavior(database, child_id, session_id):
    """Refresh local derived advice and bind it to stable raw inputs, not old prose."""
    from .behavior_model import BehaviorModelService

    for _ in range(2):
        with database.connect() as c:
            c.execute('BEGIN')
            before = daily_behavior_fingerprint(c, child_id, session_id)
            available = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observable_event'").fetchone()
        context = BehaviorModelService(database).report_safe_context(child_id, session_id) if available else {}
        with database.connect() as c:
            c.execute('BEGIN')
            after = daily_behavior_fingerprint(c, child_id, session_id)
        if before == after:
            return {**context, 'behavior_source_fingerprint': after}
    from .repository import ReportNotReadyError
    raise ReportNotReadyError('行为依据正在修订，请稍后重新生成报告。')
