"""Standalone public synthetic fixtures: close only their original unused NORMAL app owner.
No environment changes, replacement owners, ACTIVE adoption or singleton resets.
"""
import pytest

def _finish_original_test_money_owner() -> None:
    """End only an unused NORMAL test owner; never take over a live owner.

    Legacy tests intentionally construct TestClient without entering lifespan.
    HTTP client.close() does not run application shutdown. Production singleton
    and ACTIVE financial ownership remain strict; neither is reset here.
    """
    import sys
    module = sys.modules.get("home_study_agent.money_egress")
    owner = getattr(module, "_owner", None) if module is not None else None
    if owner is None:
        return
    if type(owner) is not module.MoneyOwner:
        pytest.fail("TEST_MONEY_OWNER_TYPE_UNCONFIRMED")
    if owner.active is True:
        if (owner.closed is True and owner.budget is not None
                and owner.budget._closed is True and owner.latch is not None
                and owner.latch.closed is True
                and not owner._text_inflight and not owner._tts_inflight):
            return
        pytest.fail("TEST_ACTIVE_MONEY_OWNER_NOT_CLOSED_BY_ORIGINAL_LIFECYCLE")
    if owner.active is not False:
        pytest.fail("TEST_MONEY_OWNER_MODE_UNCONFIRMED")
    if owner.closed is True:
        return
    if (owner.closed is not False or module._slot_mode != "NORMAL"
            or module._process_latch is not None or owner.budget is not None
            or owner.latch is not None or owner.key is not None):
        pytest.fail("TEST_NORMAL_MONEY_OWNER_NOT_EMPTY")
    for name, expected_type in (("_requests", set), ("_pairs", dict), ("_claims", dict),
                                ("_text_inflight", set), ("_tts_inflight", set)):
        value = getattr(owner, name, None)
        if type(value) is not expected_type or value:
            pytest.fail("TEST_NORMAL_MONEY_OWNER_HAS_PENDING_WORK")
    if owner._latch_lock.locked() or owner._bridge_lock.locked():
        pytest.fail("TEST_NORMAL_MONEY_OWNER_BUSY")
    owner.close()  # Real original object's close; no replacement or state reset.
    if owner.closed is not True or module._owner is not owner or module._slot_mode != "NORMAL":
        pytest.fail("TEST_NORMAL_MONEY_OWNER_CLOSE_UNCONFIRMED")


def close_unused_imported_default_app_owner(application):
    from home_study_agent import money_egress
    original = application.state.money_owner
    if money_egress._owner is not original:
        pytest.fail("TEST_IMPORTED_DEFAULT_OWNER_IDENTITY_REQUIRED")
    _finish_original_test_money_owner()
    if money_egress._owner is not original or original.closed is not True:
        pytest.fail("TEST_IMPORTED_DEFAULT_OWNER_CLOSE_UNCONFIRMED")
