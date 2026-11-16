import pytest

from home_study_agent.rubric_calibration_transaction import (
    _SESSION, assert_prepared_dependencies, validation_session,
)
from test_calibration_transaction import prepared


def test_final_dependency_check_allows_new_assessment_but_not_original_change(prepared):
    c, ticket = prepared
    ticket.result = {'protocol_id': 'protocol'}
    with validation_session():
        _SESSION.get()['ticket'] = ticket
        c.execute("INSERT INTO assessment VALUES('new',2)")
        assert_prepared_dependencies(c, {'protocol_id': 'protocol'})
        assert c.changed_tables(ticket) == set()
        c.execute("UPDATE source SET text='modified' WHERE id='old'")
        with pytest.raises(ValueError, match='PREPARED_EVIDENCE_CHANGED'):
            assert_prepared_dependencies(c, {'protocol_id': 'protocol'})


def test_final_dependency_check_detects_trigger_and_leaves_rollback_to_caller(prepared):
    c, ticket = prepared
    ticket.result = {'protocol_id': 'protocol'}
    with validation_session():
        _SESSION.get()['ticket'] = ticket
        c.execute("CREATE TRIGGER own_edit AFTER INSERT ON assessment BEGIN UPDATE source SET text='changed'; END")
        c.execute("INSERT INTO assessment VALUES('new',2)")
        with pytest.raises(ValueError, match='PREPARED_EVIDENCE_CHANGED'):
            assert_prepared_dependencies(c, {'protocol_id': 'protocol'})
        assert c.in_transaction
        c.rollback()
        assert c.execute("SELECT text FROM source WHERE id='old'").fetchone()[0] == 'original'


def test_other_epoch_is_not_promoted_to_current_by_final_check(prepared):
    c, ticket = prepared
    ticket.result = {'protocol_id': 'protocol'}
    with validation_session():
        _SESSION.get()['ticket'] = ticket
        c.rollback()
        c.execute('BEGIN IMMEDIATE')
        assert_prepared_dependencies(c, {'protocol_id': 'protocol'})
        assert c.changed_tables(ticket) is None
