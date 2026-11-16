"""Unapproved report text never leaves in the synthetic loopback message."""
from email import policy
from email.parser import BytesParser
import pytest
from home_study_agent import daily_report_combined_delivery as delivery
from home_study_agent.daily_report_combined_snapshot import freeze,inspect
from test_combined_report_real_sources_fixture import synthetic_combined_family,SYNTHETIC_ADDRESS,SYNTHETIC_KEY
from test_daily_report_combined_delivery import configured
from test_combined_report_loopback_candidate import sink

CANARY='PRIVATE_UNAPPROVED_SYNTHETIC_REPORT'


def prepare(f):
    with f['db'].connect() as c:
        c.execute("UPDATE parent_report_snapshot SET status='draft',content_json=? WHERE snapshot_id=?",('{\"private\":\"'+CANARY+'\"}',f['formal']['snapshot_id']));c.commit()
    batch=f['telemetry'].create_combined_batch(f['day'],SYNTHETIC_ADDRESS,[f['child']],SYNTHETIC_KEY)['batch_id']
    return freeze(f['telemetry'],batch,f['child'],f['day'],f['recipient'],f['visual'],SYNTHETIC_KEY)['snapshot_id']


def test_draft_is_mentioned_but_its_text_is_not_sent(synthetic_combined_family,monkeypatch):
    f=synthetic_combined_family;snapshot=prepare(f)
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        monkeypatch.setattr(delivery,'_source_withdrawal_guard_proven',lambda:True)
        assert delivery.submit(service,f['telemetry'],snapshot)['status']=='sent'
        message=BytesParser(policy=policy.default).parsebytes(smtp.messages[0])
        content=message.get_body(preferencelist=('plain',)).get_content()
        assert '需要家长核对，未纳入邮件' in content
        assert CANARY not in content and CANARY.encode() not in smtp.messages[0]


def test_changing_log_recipient_prevents_existing_combined_snapshot_from_sending(synthetic_combined_family,monkeypatch):
    f=synthetic_combined_family
    snapshot=prepare(f)
    f['telemetry'].configure_diagnostic_recipient('new@example.test','new@example.test')
    with sink() as smtp:
        service=configured(f,snapshot,smtp.server_address[1],monkeypatch)
        monkeypatch.setattr(delivery,'_source_withdrawal_guard_proven',lambda:True)
        with pytest.raises(ValueError,match='PILOT_RECIPIENT_SELECTION_CHANGED'):
            delivery.submit(service,f['telemetry'],snapshot)
        assert smtp.messages == []


@pytest.mark.parametrize('change',['edit','approve','delete_report_snapshot'])
def test_draft_state_change_invalidates_frozen_candidate(synthetic_combined_family,change):
    f=synthetic_combined_family;snapshot=prepare(f)
    with f['db'].connect() as c:
        if change=='edit':c.execute("UPDATE parent_report_snapshot SET content_json='{}' WHERE snapshot_id=?",(f['formal']['snapshot_id'],))
        if change=='approve':c.execute("UPDATE parent_report_snapshot SET status='approved' WHERE snapshot_id=?",(f['formal']['snapshot_id'],))
        if change=='delete_report_snapshot':c.execute('DELETE FROM parent_report_snapshot WHERE snapshot_id=?',(f['formal']['snapshot_id'],))
        c.commit()
    with pytest.raises(ValueError,match='COMBINED_FORMAL_CHANGED'):
        inspect(f['telemetry'],snapshot,SYNTHETIC_KEY)
