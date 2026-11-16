import pytest

from home_study_agent.local_supply import LocalSupplyError
from home_study_agent.open_response_materials import resolve_source_materials, _snapshot
from home_study_agent.diagnostic_prompt import _BorrowedConnection
from test_open_response_materials import material_case, CHILD


@pytest.mark.parametrize('invalid',[False,True])
def test_material_read_does_not_invalidate_an_existing_caller_cursor(tmp_path,invalid):
    db,_,_,source,_,_=material_case(tmp_path)
    if invalid:
        source['material_bindings'][0]['source_version']='0'*64
    with db.connect() as c:
        cursor=c.execute('SELECT 1 UNION ALL SELECT 2')
        assert cursor.fetchone()[0]==1
        before=c.total_changes
        if invalid:
            with pytest.raises(LocalSupplyError): resolve_source_materials(c,child_id=CHILD,source=source)
        else:
            assert resolve_source_materials(c,child_id=CHILD,source=source)['materials']
        assert cursor.fetchone()[0]==2
        assert c.total_changes==before and not c.in_transaction


@pytest.mark.parametrize('diagnostic_facade',[False,True])
def test_material_reader_never_commits_or_rolls_back_borrowed_transaction(tmp_path,diagnostic_facade):
    db,_,_,source,_,_=material_case(tmp_path)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before=c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]
        c.execute("UPDATE child_profile SET nickname='Uncommitted' WHERE child_id=?",(CHILD,))
        resolve_source_materials(_BorrowedConnection(c) if diagnostic_facade else c,child_id=CHILD,source=source)
        assert c.in_transaction
        c.rollback()
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]==before


def test_owned_read_snapshot_rejects_and_rolls_back_accidental_write(tmp_path):
    db,_,_,_,_,_=material_case(tmp_path)
    with db.connect() as c:
        before=c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]
        with pytest.raises(LocalSupplyError,match='READ_SNAPSHOT_MUTATED'):
            with _snapshot(c):
                c.execute("UPDATE child_profile SET nickname='Unexpected write' WHERE child_id=?",(CHILD,))
        assert not c.in_transaction
        assert c.execute('SELECT nickname FROM child_profile WHERE child_id=?',(CHILD,)).fetchone()[0]==before
