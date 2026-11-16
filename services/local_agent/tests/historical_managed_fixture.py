"""Verified v84 historical core projected to the pre-managed component contract.

This synthetic fixture is not a claim of a complete historical v82 database.
The original 82/84 markers select explicit migration scenarios; the source is
pinned, actually bootstrapped v84 SQL, never current-schema authority stripped.
"""
import sqlite3
from historical_database_fixture import seed_empty_historical_v84

MANAGED83_TABLES = frozenset({
    'ocr_job', 'ocr_cache', 'ocr_cache_epoch', 'ocr_request_tombstone',
    'ocr_restore_fence', 'ocr_legacy_reference', 'ocr_review_item',
    'ocr_review_event', 'ocr_review_batch', 'ocr_review_legacy_import',
    'kg_companion_graph_meta', 'kg_companion_graph_context_event',
    'kg_local_course_context_event',
})


def seed_pre_managed_v84_core(path):
    # Original historical helper enforces absent canonical synthetic DB, fixed
    # SQL hash, empty business tables, no future control tables or pending work.
    seed_empty_historical_v84(path)
    with sqlite3.connect(path) as connection:
        present = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert MANAGED83_TABLES <= present and 'observation_control_action' in present
        assert all(connection.execute('SELECT COUNT(*) FROM "'+name+'"').fetchone()[0] == 0
                   for name in MANAGED83_TABLES | {'observation_control_action'})
        # These exact objects are the verified original 83 managed migration and
        # 84 observation additions; no arbitrary supplied name or data is used.
        for name in sorted(MANAGED83_TABLES | {'observation_control_action'}):
            connection.execute('DROP TABLE "'+name+'"')
        connection.execute('ALTER TABLE daily_session DROP COLUMN observation_revision')
        connection.execute('DELETE FROM schema_migration WHERE version>82')
        connection.commit()
        assert connection.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 82
        assert connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
