import json
import sqlite3

import pytest

from home_study_agent import learning_obligations as obligations


@pytest.fixture
def catalog():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.execute('CREATE TABLE diagnostic_item (diagnostic_item_id PRIMARY KEY, source_ref, prompt, stimulus_text, stimulus_asset, expected_answer_json)')
    c.execute('CREATE TABLE specialized_supply_candidate (candidate_id PRIMARY KEY, diagnostic_item_id, prompt)')
    c.execute('CREATE TABLE curriculum_supply_package (candidate_id PRIMARY KEY, payload_json, source_snapshot_json)')
    for number in range(3):
        key = str(number)
        c.execute('INSERT INTO diagnostic_item VALUES (?,?,?,?,?,?)', (key, 'parent_approved_specialized:' + key, 'current ' + key, None, None, '{}'))
        c.execute('INSERT INTO specialized_supply_candidate VALUES (?,?,?)', (key, key, 'original ' + key))
        c.execute('INSERT INTO curriculum_supply_package VALUES (?,?,?)', (key,
            json.dumps({'open_response': {'schema': 'reviewed_open_response_source_v2'}}),
            json.dumps({'open_response_materials': {'stimulus_text': 'material ' + key}})))
    try:
        yield c
    finally:
        c.close()


def unbatched(c):
    items, identities = {}, {}
    for raw in c.execute('SELECT * FROM diagnostic_item'):
        item = dict(raw)
        items[item['diagnostic_item_id']] = item
        try:
            identities[item['diagnostic_item_id']] = obligations.question_identity(
                obligations._source_question(c, item) or item, connection=c)
        except (TypeError, ValueError):
            pass
    return items, identities


def test_catalog_batch_keeps_original_identities_and_bounds_queries(catalog):
    expected = unbatched(catalog)
    queries = []
    catalog.set_trace_callback(queries.append)
    assert obligations._question_catalog(catalog) == expected
    assert len(queries) <= 5


@pytest.mark.parametrize('mutation', [
    "UPDATE specialized_supply_candidate SET prompt='changed' WHERE candidate_id='1'",
    "UPDATE curriculum_supply_package SET source_snapshot_json='{}' WHERE candidate_id='1'",
    "UPDATE curriculum_supply_package SET payload_json='invalid' WHERE candidate_id='1'",
    "DELETE FROM curriculum_supply_package WHERE candidate_id='1'",
    "DELETE FROM specialized_supply_candidate WHERE candidate_id='1'",
])
def test_catalog_rebuilds_after_own_write_and_rollback(catalog, mutation):
    original = obligations._question_catalog(catalog)
    catalog.execute('SAVEPOINT changed')
    catalog.execute(mutation)
    assert obligations._question_catalog(catalog) == unbatched(catalog)
    catalog.execute('ROLLBACK TO changed')
    catalog.execute('RELEASE changed')
    assert obligations._question_catalog(catalog) == original


def test_catalog_sql_errors_still_propagate(catalog):
    catalog.execute('ALTER TABLE curriculum_supply_package RENAME COLUMN payload_json TO broken')
    with pytest.raises(sqlite3.OperationalError):
        obligations._question_catalog(catalog)


def test_no_transaction_uses_original_queries(catalog):
    catalog.commit()
    assert obligations._question_catalog(catalog) == unbatched(catalog)


def test_read_scope_detects_own_write(catalog):
    from home_study_agent.question_source_rows import QuestionSourceRows
    lookups = QuestionSourceRows(catalog, [dict(r) for r in catalog.execute('SELECT * FROM diagnostic_item')])
    catalog.execute("UPDATE specialized_supply_candidate SET prompt='changed'")
    with pytest.raises(ValueError, match='QUESTION_SOURCE_READ_CHANGED'):
        lookups.execute('SELECT 1 FROM sqlite_master WHERE name=?', ('specialized_supply_candidate',))


@pytest.mark.parametrize('adapter', ['borrowed', 'restricted', 'in_transaction', 'total_changes', 'row_factory'])
def test_unknown_adapter_constructor_and_catalog_use_original_sql(catalog, adapter):
    from types import SimpleNamespace
    from home_study_agent.diagnostic_prompt import _BorrowedConnection
    from home_study_agent.question_source_rows import QuestionSourceRows
    class Restricted:
        @property
        def in_transaction(self):
            return catalog.in_transaction
        def execute(self, sql, parameters=()):
            return catalog.execute(sql, parameters)
    borrowed = _BorrowedConnection(catalog) if adapter == 'borrowed' else Restricted()
    if adapter not in {'borrowed', 'restricted'}:
        metadata = dict(in_transaction=True, total_changes=catalog.total_changes, row_factory=sqlite3.Row)
        metadata.pop(adapter)
        borrowed = SimpleNamespace(execute=catalog.execute, **metadata)
    items = [dict(row) for row in catalog.execute('SELECT * FROM diagnostic_item')]
    queries = []
    catalog.set_trace_callback(queries.append)
    rows = QuestionSourceRows(borrowed, items)
    assert not rows.enabled and not rows.rows and queries == []
    expected = unbatched(borrowed)
    original_queries = queries[:]
    queries.clear()
    assert obligations._question_catalog(borrowed) == expected
    assert queries == original_queries
    assert catalog.in_transaction


def test_restricted_adapter_does_not_swallow_sql_errors(catalog):
    from home_study_agent.diagnostic_prompt import _BorrowedConnection
    catalog.execute('ALTER TABLE curriculum_supply_package RENAME COLUMN payload_json TO broken')
    with pytest.raises(sqlite3.OperationalError):
        obligations._question_catalog(_BorrowedConnection(catalog))
