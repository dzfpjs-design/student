"""TEST ONLY: durable candidate over a hard-coded loopback SMTP sink.

This harness is not a production sender. It cannot be configured for a real
host, recipient domain, credential, scheduler or API.
"""
from html import escape
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from home_study_agent.database import Database
from home_study_agent.daily_report_combined_snapshot import _inspect_in_connection
from home_study_agent.daily_report_visual_submission import _render as render_visual
from home_study_agent.notification_channels import SmtpChannel, submission_guard
from home_study_agent.pilot_test_telemetry import PilotTestTelemetryService
from home_study_agent.report_delivery import render_message

KEY=b's'*32
TABLE="""CREATE TABLE combined_loopback_attempt (
 snapshot_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, child_id TEXT NOT NULL,
 identity TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('processing','unknown','sent')),
 token TEXT NOT NULL, UNIQUE(batch_id,child_id), UNIQUE(identity))"""


def recover(telemetry):
    with telemetry.database.external_effect_guard(timeout=10),telemetry.database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        count=c.execute("UPDATE combined_loopback_attempt SET status='unknown' WHERE status='processing'").rowcount
        c.commit(); return count


def send(telemetry,snapshot_id,port,*,crash_before_result=False):
    if type(port) is not int or not 1<=port<=65535: raise ValueError('INVALID_LOOPBACK_PORT')
    with telemetry.database.external_effect_guard(timeout=10):
        with telemetry.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row,body=_inspect_in_connection(c,telemetry,snapshot_id,KEY)
            pilot=c.execute('SELECT * FROM pilot_test_digest_outbox WHERE combined_batch_id=?',(row['batch_id'],)).fetchone()
            claim=telemetry._frozen_claim(pilot)
            address=pilot['recipient'].strip().casefold()
            if not address.endswith('@example.test') or address.count('@')!=1: raise ValueError('TEST_RECIPIENT_ONLY')
            if c.execute('SELECT 1 FROM combined_loopback_attempt WHERE identity=? OR (batch_id=? AND child_id=?)',(row['recipient_identity']+':'+row['child_id'],row['batch_id'],row['child_id'])).fetchone():
                raise ValueError('PRIOR_ATTEMPT')
            token=uuid4().hex
            c.execute("INSERT INTO combined_loopback_attempt VALUES(?,?,?,?,'processing',?)",(snapshot_id,row['batch_id'],row['child_id'],row['recipient_identity']+':'+row['child_id'],token))
            c.commit()
        chunks=[]
        for raw in body['formal']:
            formal=dict(raw)
            for key in ('facts','inferences','content','confidence','parent_edit'):
                formal[key]=json.loads(formal.pop(key+'_json'))
            _,text,_=render_message(formal,'standard'); chunks.append(text)
        _,visual,_=render_visual(body['day'],body['visual'],'standard'); chunks.append(visual)
        text='\n\n'.join(chunks)
        html='<html><body><pre>'+escape(text)+'</pre></body></html>'
        def check():
            try:
                with telemetry.database.connect() as c:
                    c.execute('BEGIN'); current,_=_inspect_in_connection(c,telemetry,snapshot_id,KEY)
                    active=c.execute("SELECT 1 FROM combined_loopback_attempt WHERE snapshot_id=? AND token=? AND status='processing'",(snapshot_id,token)).fetchone()
                    if not active or current['batch_id']!=row['batch_id']: return 'ownership_changed'
                    c.rollback()
            except Exception: return 'source_changed'
            return None
        result='unknown'
        try:
            with submission_guard(check):
                SmtpChannel().send(address,'合成综合日报',text,html,{'host':'127.0.0.1','port':port,
                    'from_address':'sender@example.test','starttls':False,'allow_external_delivery':True},attachments=claim.attachments)
            result='sent'
        except Exception:
            result='unknown'
        if crash_before_result: os._exit(91)
        with telemetry.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            count=c.execute("UPDATE combined_loopback_attempt SET status=? WHERE snapshot_id=? AND token=? AND status='processing'",(result,snapshot_id,token)).rowcount
            if count!=1: raise ValueError('CLAIM_LOST')
            c.commit()
        return result


if __name__=='__main__':
    # The process receives only synthetic paths and sink port, never credentials.
    from home_study_agent import notification_channels
    notification_channels._admit_external_delivery_budget=lambda:None
    database=Database(Path(sys.argv[1])); service=PilotTestTelemetryService(database,Path(sys.argv[2]))
    send(service,sys.argv[3],int(sys.argv[4]),crash_before_result=True)
