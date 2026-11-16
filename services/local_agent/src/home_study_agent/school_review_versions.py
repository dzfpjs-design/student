"""Version school review obligations without rewriting their historical authority."""
import re
import sqlite3


INTERVALS = "'school_open_near','school_open_far','school_open_7d','writing_stage_7d'"
EXTRA = ('score_authority_id', 'score_status', 'predecessor_review_task_id')


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def _indexes(c):
    c.execute(f'''CREATE UNIQUE INDEX IF NOT EXISTS review_task_other_interval
        ON review_task(source_evidence_id,interval_label) WHERE interval_label NOT IN ({INTERVALS})''')
    c.execute(f'''CREATE UNIQUE INDEX IF NOT EXISTS review_task_school_current
        ON review_task(source_evidence_id,interval_label)
        WHERE interval_label IN ({INTERVALS}) AND status!='cancelled' ''')
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS review_task_single_successor
        ON review_task(predecessor_review_task_id) WHERE predecessor_review_task_id IS NOT NULL''')


def migrate(c):
    """Idle startup/staging only; atomic rebuild preserves incoming FK targets."""
    if c.in_transaction:
        raise RuntimeError('SCHOOL_REVIEW_MIGRATION_REQUIRES_IDLE_CONNECTION')
    info = c.execute('PRAGMA table_info(review_task)').fetchall()
    if not info:
        return
    if all(name in {r['name'] for r in info} for name in EXTRA):
        _indexes(c)
        return
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='review_task'").fetchone()[0]
    known = {'review_task_id','child_id','knowledge_node_id','diagnostic_item_id','source_evidence_id',
             'interval_label','due_at','status','completed_at','result_evidence_id','created_at'}
    if (set(r['name'] for r in info) != known
            or any(r['type'].upper() != 'TEXT' or r['dflt_value'] is not None for r in info)
            or [r['name'] for r in info if r['pk']] != ['review_task_id']
            or re.search(r'\b(CHECK|COLLATE|DEFERRABLE|GENERATED|STRICT)\b|\bWITHOUT\s+ROWID\b|\bON\s+CONFLICT\b', sql, re.I)
            or len(c.execute('PRAGMA table_xinfo(review_task)').fetchall()) != len(info)):
        raise RuntimeError('SCHOOL_REVIEW_UNSUPPORTED_SCHEMA')
    unique = [tuple(r['name'] for r in c.execute('PRAGMA index_info(' + _q(i['name']) + ')'))
              for i in c.execute('PRAGMA index_list(review_task)').fetchall()
              if i['unique'] and i['origin'] == 'u']
    if unique != [('source_evidence_id', 'interval_label')]:
        raise RuntimeError('SCHOOL_REVIEW_UNSUPPORTED_UNIQUENESS')
    definitions = [_q(r['name']) + ' TEXT' + (' NOT NULL' if r['notnull'] else '') for r in info]
    definitions.extend(_q(name) + ' TEXT' for name in EXTRA)
    definitions.append('PRIMARY KEY (review_task_id)')
    for fk in c.execute('PRAGMA foreign_key_list(review_task)').fetchall():
        if fk['seq'] != 0:
            raise RuntimeError('SCHOOL_REVIEW_UNSUPPORTED_FOREIGN_KEY')
        definitions.append('FOREIGN KEY (' + _q(fk['from']) + ') REFERENCES ' + _q(fk['table'])
            + '(' + _q(fk['to']) + ') ON DELETE ' + fk['on_delete'] + ' ON UPDATE ' + fk['on_update'])
    # Predecessor is an identity link, not a new score or a deletion dependency.
    ancillary = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE tbl_name='review_task' AND type IN ('index','trigger') AND sql IS NOT NULL")]
    foreign_keys = c.execute('PRAGMA foreign_keys').fetchone()[0]
    c.execute('PRAGMA foreign_keys=OFF')
    try:
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE review_task_rebuilt (' + ','.join(definitions) + ')')
        columns = ','.join(_q(r['name']) for r in info)
        c.execute(f'INSERT INTO review_task_rebuilt(rowid,{columns}) SELECT rowid,{columns} FROM review_task')
        c.execute('DROP TABLE review_task')
        c.execute('ALTER TABLE review_task_rebuilt RENAME TO review_task')
        for statement in ancillary:
            c.execute(statement)
        _indexes(c)
        if c.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('SCHOOL_REVIEW_MIGRATION_FOREIGN_KEY_FAILED')
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.execute(f'PRAGMA foreign_keys={int(foreign_keys)}')
