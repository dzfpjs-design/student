"""Run lazy schema statements without sqlite3.executescript's implicit commit."""
import sqlite3


def execute_schema_statements(connection, script: str) -> None:
    pending = ''
    for character in script:
        pending += character
        if character == ';' and sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ''
    if pending.strip():
        raise ValueError('INCOMPLETE_SCHEMA_STATEMENT')
