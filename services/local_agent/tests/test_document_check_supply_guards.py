"""Local source qualification is bounded, child-owned and transaction-owned."""
import pytest

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent import document_check_supply as supply
from home_study_agent.local_supply import LocalSupplyError, validate_item
from test_diagnostic_prompt_http import environment, CHILD, OTHER, ITEM


@pytest.mark.parametrize('child,seed,other',[(CHILD,ITEM,OTHER),(OTHER,'diag-j3-discriminant-1',CHILD)])
def test_each_child_owns_supply_and_rejection_cannot_be_revived(environment,child,seed,other):
    settings,db=environment
    with TestClient(create_app(settings)):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            first=supply.deliver(c,child,seed,'first-owned-supply')
            assert first
            original=validate_item(c,child,first)
            with pytest.raises(LocalSupplyError):
                validate_item(c,other,first)
            c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE diagnostic_item_id=?",(first,))
            second=supply.deliver(c,child,seed,'second-owned-supply')
            assert second and second!=first
            assert validate_item(c,child,second)['prompt']!=original['prompt']
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
            c.commit()


def test_all_reserved_templates_stop_without_new_rows(environment):
    settings,db=environment
    with TestClient(create_app(settings)):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for ordinal in range(36):
                item=supply.deliver(c,CHILD,ITEM,'reserve-supply-'+str(ordinal))
                assert item
                c.execute("UPDATE specialized_supply_candidate SET status='rejected' WHERE diagnostic_item_id=?",(item,))
            assert supply.deliver(c,CHILD,ITEM,'exhausted-supply') is None
            assert c.execute('SELECT count(*) FROM specialized_supply_job').fetchone()[0]==36
            assert c.execute('SELECT count(*) FROM specialized_supply_candidate').fetchone()[0]==36
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0]==0
            c.rollback()


def test_supply_requires_outer_transaction(environment):
    settings,db=environment
    with TestClient(create_app(settings)):
        with db.connect() as c:
            with pytest.raises(LocalSupplyError,match='SUPPLY_TRANSACTION_REQUIRED'):
                supply.deliver(c,CHILD,ITEM,'no-transaction-supply')
            assert c.execute('SELECT count(*) FROM specialized_supply_job').fetchone()[0]==0
