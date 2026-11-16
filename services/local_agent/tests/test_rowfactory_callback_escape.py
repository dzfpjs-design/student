"""Native SQLite row-factory callbacks also receive a cursor connection."""
import sqlite3
from test_transaction21_independent_audit import captured


def test_custom_cursor_row_factory_cannot_preserve_selective_reuse(captured):
    c, ticket, _ = captured
    sql = 'INSERT INTO output VALUES(?)'
    c.execute(sql, ('first',)).close()
    assert ticket.compare(c)
    c.verified(ticket)
    leaked = []

    def factory(cursor, row):
        leaked.append(cursor.connection)
        return row

    cursor = c.execute('SELECT 1')
    try:
        cursor.row_factory = factory
        assert cursor.fetchone() == (1,)
        raw = leaked[0]
        raw.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
        raw.set_trace_callback(None)
        raw.execute('CREATE TRIGGER late AFTER INSERT ON output BEGIN UPDATE source SET value=2; END').close()
        c.execute(sql, ('second',)).close()
        assert c.execute('SELECT value FROM source').fetchone()[0] == 2
        assert not ticket.compare(c), 'Changed source accepted via native row-factory connection'
    finally:
        cursor.close()
