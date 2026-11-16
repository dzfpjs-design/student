"""Explicit retry may bypass only its authenticated unknown ancestry."""
import pytest

from test_report_auto_recipient_verification import setup


def original_and_reissue(tmp_path):
    db,child,reports,snapshot,delivery,fake,a,b=setup(tmp_path)
    reports.approve(snapshot['snapshot_id'])
    original=delivery.enqueue(snapshot['snapshot_id'],a)
    with db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='unknown' WHERE delivery_id=?", (original['delivery_id'],)); c.commit()
    replacement=delivery.reissue_unknown(original['delivery_id'],'explicit-action-1')
    return db,child,reports,snapshot,delivery,fake,a,b,original,replacement


@pytest.mark.parametrize('damage',['missing_audit','wrong_action','missing_link','wrong_recipient'])
def test_forged_or_incomplete_reissue_never_sends(tmp_path,monkeypatch,damage):
    db,child,reports,snapshot,delivery,fake,a,b,original,replacement=original_and_reissue(tmp_path)
    with db.connect() as c:
        if damage=='missing_audit':
            c.execute("DELETE FROM audit_event WHERE action='report_delivery_unknown_explicit_reissue'")
        elif damage=='wrong_action':
            c.execute("UPDATE report_delivery_outbox SET reissue_action_id='different-action' WHERE delivery_id=?", (replacement['delivery_id'],))
        elif damage=='missing_link':
            c.execute('UPDATE report_delivery_outbox SET replacement_delivery_id=NULL WHERE delivery_id=?',(original['delivery_id'],))
        else:
            c.execute('UPDATE report_delivery_outbox SET recipient_id=? WHERE delivery_id=?',(b,replacement['delivery_id']))
        c.commit()
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _child:False)
    result=delivery._process_one(replacement['delivery_id'])
    assert result['status']=='cancelled' and result['cancel_reason']=='invalid_reissue_provenance'
    assert fake.messages==[]


def test_unrelated_unknown_still_blocks_explicit_reissue(tmp_path,monkeypatch):
    db,child,reports,snapshot,delivery,fake,a,b,original,replacement=original_and_reissue(tmp_path)
    with db.connect() as c:
        now=original['created_at']
        c.execute("INSERT INTO report_delivery_outbox(delivery_id,snapshot_id,recipient_id,channel,idempotency_key,status,scheduled_at,created_at,updated_at) VALUES('unrelated',?,?,'email','legacy-unknown','unknown',?,?,?)",(snapshot['snapshot_id'],a,now,now,now)); c.commit()
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _child:False)
    result=delivery._process_one(replacement['delivery_id'])
    assert result['status']=='cancelled' and result['cancel_reason']=='prior_delivery_unresolved'
    assert fake.messages==[]


def test_new_explicit_action_can_reissue_a_second_unknown_in_same_chain(tmp_path,monkeypatch):
    db,child,reports,snapshot,delivery,fake,a,b,original,replacement=original_and_reissue(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE report_delivery_outbox SET status='unknown' WHERE delivery_id=?",(replacement['delivery_id'],)); c.commit()
    second=delivery.reissue_unknown(replacement['delivery_id'],'explicit-action-2')
    monkeypatch.setattr(delivery,'_in_quiet_hours',lambda _child:False)
    result=delivery._process_one(second['delivery_id'])
    assert result['status']=='sent' and len(fake.messages)==1
    assert delivery.reissue_unknown(replacement['delivery_id'],'explicit-action-2')['delivery_id']==second['delivery_id']
    assert len(fake.messages)==1
