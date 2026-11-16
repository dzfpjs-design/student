"""Atomic stale-candidate rebuild; synthetic data and loopback only."""
import json
from email import policy
from email.parser import BytesParser
import pytest
from home_study_agent.daily_report_combined_rebuild import rebuild
from home_study_agent.daily_report_combined_snapshot import _inspect_in_connection
from home_study_agent.daily_report_combined_snapshot import inspect
from home_study_agent import daily_report_frozen_dependencies, daily_report_combined_delivery as delivery
from test_daily_report_combined_snapshot import candidate
from test_combined_report_real_sources_fixture import synthetic_combined_family
from test_daily_report_combined_delivery import configured
from test_combined_report_loopback_candidate import sink


def _state(f):
    with f['db'].connect() as c:
        tables=('combined_daily_snapshot','daily_observation_report_source','daily_observation_report_outbox',
                'daily_observation_report_snapshot','daily_observation_delivery_ledger','combined_diagnostic_member')
        return {name:sorted([tuple(r) for r in c.execute('SELECT * FROM '+name)],key=repr) for name in tables}


def test_changed_formal_rebuilds_then_sends_once(candidate,monkeypatch):
    f,batch,old,_=candidate
    with sink() as smtp:
        service=configured(f,old,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            c.execute("UPDATE parent_report_snapshot SET parent_edit_json='{\"note\":\"changed\"}' WHERE snapshot_id=?",(f['formal']['snapshot_id'],));c.commit()
        with pytest.raises(ValueError):inspect(f['telemetry'],old,service.encryption_key)
        new=rebuild(service,f['telemetry'],old)['snapshot_id']
        assert new!=old and inspect(f['telemetry'],new,service.encryption_key)['current']
        monkeypatch.setattr(delivery,'_source_withdrawal_guard_proven',lambda:True)
        assert delivery.submit(service,f['telemetry'],new)['status']=='sent'
        assert len(smtp.messages)==1
        with pytest.raises(ValueError):rebuild(service,f['telemetry'],new)
        assert len(smtp.messages)==1


@pytest.mark.parametrize('lose_reply', [False, True])
def test_stale_approved_formal_is_withheld_rebuilt_and_never_retried_after_submission(candidate, monkeypatch, lose_reply):
    f,batch,old,_=candidate
    with f['db'].connect() as c:
        report = c.execute('SELECT report_id,summary_json FROM daily_report WHERE session_id=?', (f['session'],)).fetchone()
        summary = json.loads(report['summary_json'])
        summary['evidence_count'] += 1
        c.execute('UPDATE daily_report SET summary_json=? WHERE report_id=?', (json.dumps(summary,ensure_ascii=False), report['report_id']))
        c.commit()
    with pytest.raises(ValueError,match='COMBINED_FORMAL_CHANGED'):
        inspect(f['telemetry'],old,service_key := f['delivery'].encryption_key)
    with sink(lose_reply=lose_reply) as smtp:
        service=configured(f,old,smtp.server_address[1],monkeypatch)
        first=rebuild(service,f['telemetry'],old)['snapshot_id']
        with f['db'].connect() as c:
            c.execute('BEGIN')
            _,body=_inspect_in_connection(c,f['telemetry'],first,service_key)
            assert body['formal']==[] and body['formal_pending_count']==1
            _,text,html=delivery._render(body,'standard')
            assert '复习判别式第一步' not in text and '复习判别式第一步' not in html
            assert '需要家长核对' in text
        # Even if the approved report remains stale, its source changes must
        # invalidate the candidate rather than silently keeping the old digest.
        with f['db'].connect() as c:
            c.execute("UPDATE daily_reflection SET learned_text='合成变化后的反思' WHERE session_id=?", (f['session'],))
            c.commit()
        with pytest.raises(ValueError,match='COMBINED_FORMAL_CHANGED'):
            inspect(f['telemetry'],first,service_key)
        current=rebuild(service,f['telemetry'],first)['snapshot_id']
        monkeypatch.setattr(delivery,'_source_withdrawal_guard_proven',lambda:True)
        assert delivery.submit(service,f['telemetry'],current)['status']==('unknown' if lose_reply else 'sent')
        assert len(smtp.messages)==1
        email = BytesParser(policy=policy.default).parsebytes(smtp.messages[0])
        text_parts = [part.get_content() for part in email.walk() if part.get_content_type() in {'text/plain','text/html'}]
        assert text_parts and all('复习判别式第一步' not in part for part in text_parts)
        assert any('需要家长核对' in part for part in text_parts)
        with pytest.raises(ValueError):rebuild(service,f['telemetry'],current)
        assert len(smtp.messages)==1


def test_rebuild_keeps_current_approval_and_withholds_only_the_stale_one(candidate, monkeypatch):
    f,_,old,_=candidate
    with f['db'].connect() as c:
        c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            SELECT 'later-session',child_id,status,state,started_at,last_checkpoint_at,'later-action'
            FROM daily_session WHERE session_id=?''',(f['session'],))
        c.execute('''INSERT INTO daily_report(report_id,session_id,child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at)
            SELECT 'later-report','later-session',child_id,report_date,source,evidence_json,summary_json,parent_coaching_json,created_at
            FROM daily_report WHERE session_id=?''',(f['session'],))
        c.execute('''INSERT INTO daily_reflection(reflection_id,session_id,child_id,learned_text,difficult_text,feeling,next_action,skipped,created_at,client_action_id)
            SELECT 'later-reflection','later-session',child_id,learned_text,difficult_text,feeling,next_action,skipped,created_at,'later-reflection-action'
            FROM daily_reflection WHERE session_id=?''',(f['session'],))
        c.commit()
    later=f['reports'].create_from_daily(f['repo'].get_daily_report('later-report'))
    f['reports'].approve(later['snapshot_id'], parent_edit_patch={'parent_note':'合成的过期批准正文不可发送'})
    with f['db'].connect() as c:
        row=c.execute("SELECT summary_json FROM daily_report WHERE report_id='later-report'").fetchone()
        summary=json.loads(row['summary_json']);summary['evidence_count']+=1
        c.execute("UPDATE daily_report SET summary_json=? WHERE report_id='later-report'", (json.dumps(summary,ensure_ascii=False),))
        c.commit()
    with sink() as smtp:
        service=configured(f,old,smtp.server_address[1],monkeypatch)
        new=rebuild(service,f['telemetry'],old)['snapshot_id']
        with f['db'].connect() as c:
            c.execute('BEGIN')
            _,body=_inspect_in_connection(c,f['telemetry'],new,service.encryption_key)
            assert [entry['snapshot_id'] for entry in body['formal']]==[f['formal']['snapshot_id']]
            assert body['formal_pending_count']==1
            _,text,_=delivery._render(body,'standard')
            assert '复习判别式第一步' in text
            assert '合成的过期批准正文不可发送' not in text


def test_mid_rebuild_exception_rolls_back_all_original_rows(candidate,monkeypatch):
    f,batch,old,_=candidate
    with sink() as smtp:
        service=configured(f,old,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            c.execute("UPDATE parent_report_snapshot SET parent_edit_json='{\"note\":\"changed\"}' WHERE snapshot_id=?",(f['formal']['snapshot_id'],));c.commit()
        before=_state(f)
        def fail(*args,**kwargs): raise RuntimeError('synthetic interrupt')
        original_build=daily_report_frozen_dependencies.build
        monkeypatch.setattr(daily_report_frozen_dependencies,'build',fail)
        with pytest.raises(RuntimeError,match='synthetic interrupt'):rebuild(service,f['telemetry'],old)
        assert _state(f)==before
        assert not smtp.messages
        # Reopen the durable database and resume with the exact old identity.
        monkeypatch.setattr(daily_report_frozen_dependencies,'build',original_build)
        from home_study_agent.database import Database
        from home_study_agent.report_delivery import ReportDeliveryService
        from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
        reopened=Database(f['db'].path);reopened.initialize()
        resumed_service=ReportDeliveryService(reopened,service.encryption_key);resumed_service.initialize()
        resumed_telemetry=PilotTestTelemetryService(reopened,f['telemetry'].data_dir);resumed_telemetry.initialize()
        new=rebuild(resumed_service,resumed_telemetry,old)['snapshot_id']
        assert new!=old and inspect(resumed_telemetry,new,service.encryption_key)['current']


def test_unknown_delivery_never_rebuilds(candidate,monkeypatch):
    f,batch,old,_=candidate
    with sink(lose_reply=True) as smtp:
        service=configured(f,old,smtp.server_address[1],monkeypatch)
        monkeypatch.setattr(delivery,'_source_withdrawal_guard_proven',lambda:True)
        assert delivery.submit(service,f['telemetry'],old)['status']=='unknown'
        before=_state(f)
        from home_study_agent.database import Database
        from home_study_agent.report_delivery import ReportDeliveryService
        from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
        reopened=Database(f['db'].path);reopened.initialize()
        resumed_service=ReportDeliveryService(reopened,service.encryption_key);resumed_service.initialize()
        resumed_telemetry=PilotTestTelemetryService(reopened,f['telemetry'].data_dir);resumed_telemetry.initialize()
        with pytest.raises(ValueError):rebuild(resumed_service,resumed_telemetry,old)
        assert _state(f)==before
        assert len(smtp.messages)==1


def test_changed_visual_source_is_rebuilt_from_current_evidence(candidate,monkeypatch):
    f,batch,old,_=candidate
    with sink() as smtp:
        service=configured(f,old,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            c.execute("UPDATE document_page_revision SET ocr_confidence=0.33 WHERE artifact_id='fixture-art'");c.commit()
        with pytest.raises(ValueError,match='COMBINED_VISUAL_STALE'):
            inspect(f['telemetry'],old,service.encryption_key)
        new=rebuild(service,f['telemetry'],old)['snapshot_id']
        assert inspect(f['telemetry'],new,service.encryption_key)['current']
        monkeypatch.setattr(delivery,'_source_withdrawal_guard_proven',lambda:True)
        assert delivery.submit(service,f['telemetry'],new)['status']=='sent'
        assert len(smtp.messages)==1


def test_inconsistent_visual_source_rolls_back_and_never_sends(candidate,monkeypatch):
    f,batch,old,_=candidate
    with sink() as smtp:
        service=configured(f,old,smtp.server_address[1],monkeypatch)
        with f['db'].connect() as c:
            c.execute("UPDATE capture_artifact SET extracted_text='合成不一致页面' WHERE artifact_id='fixture-art'");c.commit()
        before=_state(f)
        with pytest.raises(ValueError,match='FROZEN_REJECTED_SOURCES_UNBOUND'):
            rebuild(service,f['telemetry'],old)
        assert _state(f)==before and not smtp.messages
