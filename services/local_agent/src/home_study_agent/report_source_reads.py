"""Owned report connections may reuse audited calibration dependencies only."""
from contextlib import contextmanager
import sqlite3

from .database import Database, StudyConnection
from .rubric_calibration_transaction import (
    _OwnedConnection, calibration_connection, scoped_validation, validation_session,
)


def report_read_connection(connection):
    return (isinstance(connection, sqlite3.Connection)
            or (type(connection) is _OwnedConnection and connection.reusable))


def report_read_epoch(connection):
    """Only the audited owner's transaction trace can replace a SQL marker."""
    if type(connection) is _OwnedConnection and connection.reusable and connection.in_transaction:
        return connection.epoch
    return None


def report_dict_rows(connection, sql, args=()):
    if (type(connection) is _OwnedConnection and connection.reusable
            and connection.row_factory is sqlite3.Row):
        cursor = connection.execute(sql, args)
        try:
            return [dict(row) for row in cursor]
        finally:
            cursor.close()
    cursor = connection.cursor()
    try:
        if cursor.row_factory is not sqlite3.Row:
            cursor.row_factory = sqlite3.Row
        return [dict(row) for row in cursor.execute(sql, args)]
    finally:
        cursor.close()


@contextmanager
def report_source_connection(database):
    # Never take authorizer ownership from a borrowed/unknown adapter. Ordinary
    # behavior-only databases retain the original native connection fast path.
    if type(database) is not Database:
        with database.connect() as connection:
            yield connection
        return
    with database.connect() as connection:
        possible = (type(connection) is StudyConnection and not connection.in_transaction
            and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rubric_calibration_protocol'").fetchone()
            and connection.execute('SELECT 1 FROM rubric_calibration_protocol LIMIT 1').fetchone())
        if not possible:
            yield connection
            return
    # The preliminary read admits only the optimization, never a score. The
    # actual newly opened connection repeats full/live qualification as needed.
    with validation_session(), calibration_connection(database) as connection:
        yield connection
