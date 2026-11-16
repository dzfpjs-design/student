"""System-verified report scheduling with no network operation in this step."""
from __future__ import annotations


def verify_and_schedule_reports(parent_reports, report_delivery, child_id: str, snapshots=None) -> dict:
    channel = next((item for item in report_delivery.channel_status() if item.get("channel") == "email"), None)
    email_test = next((item for item in report_delivery.channel_test_status(child_id)
                       if item.get('channel') == 'email'), None)
    verified_ids = set(email_test.get('verified_recipient_ids') or []) if email_test else set()
    all_recipients = [item for item in report_delivery.list_recipients(child_id)
                  if item.get("channel") == "email" and item.get("consent_status") == "consented"
                  and item.get("revoked_at") is None]
    recipients = [item for item in all_recipients if item.get('recipient_id') in verified_ids]
    result = {"child_id": child_id, "verified": 0, "scheduled": 0, "blocked": [],
              "network_submission_performed": False}
    if not channel or not channel.get("configured") or not recipients:
        result["blocked"].append("EMAIL_INITIALIZATION_INCOMPLETE")
        return result
    if len(recipients) != len(all_recipients):
        result['blocked'].append('RECIPIENT_EMAIL_TEST_REQUIRED')
    candidates = list(snapshots) if snapshots is not None else parent_reports.list(child_id, 30)
    for candidate in candidates:
        if candidate.get("child_id") != child_id or candidate.get("status") not in {"draft", "approved"}:
            continue
        try:
            snapshot = (parent_reports.approve_system_verified(candidate["snapshot_id"])
                        if candidate["status"] == "draft" else candidate)
            if not parent_reports.validate_current_for_delivery(snapshot["snapshot_id"]):
                raise ValueError("REPORT_SOURCE_NOT_CURRENT")
            result["verified"] += int(candidate["status"] == "draft")
            for recipient in recipients:
                try:
                    delivery = report_delivery.enqueue(snapshot["snapshot_id"], recipient["recipient_id"], automatic=True)
                    result["scheduled"] += int(delivery.get('created') is True)
                except Exception as error:
                    result['blocked'].append(type(error).__name__)
        except Exception as error:
            result["blocked"].append(type(error).__name__)
    return result


def recover_saved_daily_reports(parent_reports, report_delivery, child_id: str, limit: int = 100) -> dict:
    """Revisit persisted daily reports after interrupted generation or channel setup.

    The normal path still verifies an unedited draft and rechecks current source
    before enqueueing. Existing outbox entries, including unknown outcomes, are
    reused by enqueue and never resubmitted by this recovery pass.
    """
    channel = next((item for item in report_delivery.channel_status() if item.get('channel') == 'email'), None)
    email_test = next((item for item in report_delivery.channel_test_status(child_id)
                       if item.get('channel') == 'email'), None)
    verified_ids = set(email_test.get('verified_recipient_ids') or []) if email_test else set()
    result = {'child_id': child_id, 'verified': 0, 'scheduled': 0, 'blocked': [],
              'network_submission_performed': False}
    if not channel or not channel.get('configured') or not verified_ids:
        result['blocked'].append('EMAIL_INITIALIZATION_INCOMPLETE')
        return result
    placeholders = ','.join('?' for _ in verified_ids)
    ordered_ids = sorted(verified_ids)
    with parent_reports.database.connect() as connection:
        needs_work = connection.execute('''SELECT 1 FROM report_delivery_recipient r
          WHERE r.child_id=? AND r.channel='email' AND r.consent_status='consented' AND r.revoked_at IS NULL
          AND r.recipient_id IN (''' + placeholders + ''')
          AND (EXISTS (SELECT 1 FROM daily_report d WHERE d.child_id=r.child_id AND NOT EXISTS
                 (SELECT 1 FROM parent_report_snapshot s WHERE s.report_id=d.report_id))
            OR EXISTS (SELECT 1 FROM parent_report_snapshot s WHERE s.child_id=r.child_id
                 AND s.report_type='daily' AND s.status IN ('draft','approved') AND NOT EXISTS
                 (SELECT 1 FROM report_delivery_outbox o WHERE o.snapshot_id=s.snapshot_id AND o.recipient_id=r.recipient_id
                   AND NOT (o.status='cancelled' AND COALESCE(o.cancel_reason,'')='recipient_email_test_required'
                     AND COALESCE(o.last_error_code,'') LIKE 'AUTO_VERIFY_BLOCKED:%'
                     AND EXISTS (SELECT 1 FROM audit_event a WHERE a.child_id=s.child_id AND a.action='report_delivery_auto_verification_blocked'
                       AND json_extract(a.summary_json,'$.delivery_id')=o.delivery_id AND json_extract(a.summary_json,'$.marker')=o.last_error_code))))) LIMIT 1''', (child_id,*ordered_ids)).fetchone()
    if not needs_work:
        return result
    bounded = max(1, min(limit, 100))
    parent_reports.reconcile_daily_sources(child_id, bounded)
    with parent_reports.database.connect() as connection:
        ids = connection.execute('''SELECT s.snapshot_id FROM parent_report_snapshot s
          WHERE s.child_id=? AND s.report_type='daily' AND s.status IN ('draft','approved')
          AND EXISTS (SELECT 1 FROM report_delivery_recipient r WHERE r.child_id=s.child_id
             AND r.channel='email' AND r.consent_status='consented' AND r.revoked_at IS NULL
             AND r.recipient_id IN (''' + placeholders + ''')
             AND NOT EXISTS (SELECT 1 FROM report_delivery_outbox o WHERE o.snapshot_id=s.snapshot_id
                              AND o.recipient_id=r.recipient_id
                              AND NOT (o.status='cancelled' AND COALESCE(o.cancel_reason,'')='recipient_email_test_required'
                                AND COALESCE(o.last_error_code,'') LIKE 'AUTO_VERIFY_BLOCKED:%'
                                AND EXISTS (SELECT 1 FROM audit_event a WHERE a.child_id=s.child_id AND a.action='report_delivery_auto_verification_blocked'
                                  AND json_extract(a.summary_json,'$.delivery_id')=o.delivery_id AND json_extract(a.summary_json,'$.marker')=o.last_error_code))))
          ORDER BY s.created_at DESC LIMIT ?''', (child_id,*ordered_ids,bounded)).fetchall()
    for row in ids:
        try:
            result_for_snapshot = verify_and_schedule_reports(parent_reports, report_delivery, child_id,
                                                                [parent_reports.get(row['snapshot_id'])])
            result['verified'] += result_for_snapshot['verified']
            result['scheduled'] += result_for_snapshot['scheduled']
            result['blocked'].extend(result_for_snapshot['blocked'])
        except (ValueError, LookupError) as error:
            result['blocked'].append(type(error).__name__)
    return result
