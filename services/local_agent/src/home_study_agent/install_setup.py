"""Durable, privacy-minimised status for the post-install parent setup gate."""
from __future__ import annotations

from typing import Protocol


class ParentAuthStatus(Protocol):
    def status(self) -> dict: ...


class ChildRepository(Protocol):
    def list_children(self) -> list[dict]: ...
    def has_existing_learning_history(self) -> bool: ...


class ReportDeliveryStatus(Protocol):
    def channel_status(self) -> list[dict]: ...
    def list_recipients(self, child_id: str) -> list[dict]: ...
    def channel_test_status(self, child_id: str) -> list[dict]: ...


def installation_setup_status(
    parent_auth: ParentAuthStatus,
    repository: ChildRepository,
    report_delivery: ReportDeliveryStatus,
) -> dict:
    """Return booleans and counts only; never expose addresses or credentials."""
    pin_configured = bool(parent_auth.status().get("configured"))
    email_status = next(
        (item for item in report_delivery.channel_status() if item.get("channel") == "email"),
        None,
    )
    smtp_configured = bool(email_status and email_status.get("configured"))
    children = [item for item in repository.list_children() if item.get("status", "active") == "active"]
    missing_recipient = 0
    missing_test = 0
    for child in children:
        child_id = str(child["child_id"])
        recipients = report_delivery.list_recipients(child_id)
        if not any(
            item.get("channel") == "email"
            and item.get("consent_status") == "consented"
            and item.get("revoked_at") is None
            for item in recipients
        ):
            missing_recipient += 1
        tests = report_delivery.channel_test_status(child_id)
        if not any(item.get("channel") == "email" and item.get("status") == "passed" for item in tests):
            missing_test += 1
    delivery_ready = smtp_configured and (
        not children or (missing_recipient == 0 and missing_test == 0)
    )
    return {
        "schema": "hsc.installation-setup.v1",
        "existing_family": bool(getattr(repository, 'has_existing_learning_history', lambda: False)()),
        "complete": pin_configured and delivery_ready,
        "parent_pin_configured": pin_configured,
        "smtp_configured": smtp_configured,
        "smtp_secret_check_required": smtp_configured,
        "active_child_count": len(children),
        "children_missing_email_recipient": missing_recipient,
        "children_missing_email_test": missing_test,
        "next_step": (
            "parent_pin" if not pin_configured else
            "smtp" if not smtp_configured else
            "email_recipients" if missing_recipient else
            "email_test" if missing_test else
            "complete"
        ),
    }
