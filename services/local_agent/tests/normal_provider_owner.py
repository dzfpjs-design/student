"""Explicit standalone transport scope, never an App-factory/autouse override.
No money globals are reset. The original NORMAL owner opens and closes itself.
"""
from contextlib import contextmanager
import pytest
from home_study_agent import money_egress as money

@contextmanager
def normal_provider_scope():
    # Inspect only: do not steal a live App owner or adopt an ACTIVE ledger.
    prior=money._owner
    assert prior is None or (type(prior)is money.MoneyOwner and prior.closed is True), 'TEST_PROVIDER_OWNER_ALREADY_LIVE'
    assert money._slot_mode in {'UNSEEN','NORMAL'}, 'TEST_PROVIDER_REQUIRES_NORMAL_MODE'
    assert money.slot_present() is False, 'TEST_PROVIDER_REQUIRES_ABSENT_SLOT'
    owner=money.open_process_owner(None)
    try:
        assert owner.active is False and owner.check() is False
        assert owner.budget is owner.latch is owner.key is None
        yield owner
    finally:
        if owner.closed is False:owner.close()
        assert owner.closed is True and money._owner is owner and money._slot_mode=='NORMAL'
        assert not owner._requests and not owner._pairs and not owner._claims
        assert not owner._text_inflight and not owner._tts_inflight

@pytest.fixture
def normal_provider_owner():
    with normal_provider_scope()as owner:yield owner
