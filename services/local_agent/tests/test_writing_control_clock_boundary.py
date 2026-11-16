"""SQLite protocol counterexample; no app/media/model or household database."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import sqlite3
import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore

from home_study_agent import paper_writing_control_projection as projection


@pytest.mark.parametrize('skew_seconds',[0,-10,10])
@pytest.mark.parametrize('historical_lease',['valid','missing','later','expired','released_before'])
def test_live_accepted_camera_receipt_remains_historical_after_release(skew_seconds,historical_lease):
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
    try:
        tables={
            'daily_session':'session_id TEXT,child_id TEXT',
            'agent_turn':'agent_turn_id TEXT,child_id TEXT,session_id TEXT,transcript TEXT,action_json TEXT,tool_status TEXT,created_at TEXT',
            'agent_plan':'plan_id TEXT,agent_turn_id TEXT,child_id TEXT,session_id TEXT,status TEXT,context_manifest_json TEXT',
            'agent_step':'step_id TEXT,plan_id TEXT,step_index INTEGER,capability_name TEXT,execution_location TEXT,status TEXT,result_json TEXT,error_code TEXT,updated_at TEXT,arguments_json TEXT',
            'voice_input':'voice_input_id TEXT,transcript TEXT,local_path TEXT,status TEXT,child_id TEXT,session_id TEXT',
            'voice_transcription_action':'client_action_id TEXT,status TEXT,child_id TEXT,session_id TEXT',
            'voice_capture_binding':'voice_input_id TEXT,child_id TEXT,session_id TEXT,prompt_id TEXT,item_version TEXT,status TEXT,client_action_id TEXT',
            'voice_control_intent_request':'request_key TEXT,child_id TEXT,session_id TEXT,fingerprint TEXT,status TEXT,binding_json TEXT,decision_json TEXT,created_at TEXT,completed_at TEXT',
            'sensor_lease':'session_id TEXT,device_type TEXT,status TEXT,opened_at TEXT,expires_at TEXT,released_at TEXT',
            'document_monitor_session':'session_id TEXT,child_id TEXT,status TEXT',
        }
        for name,columns in tables.items():c.execute('CREATE TABLE '+name+'('+columns+')')
        def put(table,*values):c.execute('INSERT INTO '+table+' VALUES('+','.join('?' for _ in values)+')',values)
        now=datetime.now(timezone.utc)
        stamp=lambda delta:(now+timedelta(seconds=delta)).isoformat()
        child,sid,prompt,version='synthetic-child','synthetic-session','prompt-2','version-1'
        quote='恢复高拍仪';voice='voice';turn='turn'
        put('daily_session',sid,child)
        put('voice_input',voice,quote,None,'completed',child,sid)
        put('voice_transcription_action','asr','completed',child,sid)
        put('voice_capture_binding',voice,child,sid,prompt,version,'completed','asr')
        capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=sid,child_id=child,voice_input_id=voice,transcript=quote)
        binding={'document_monitor':{'status':'paused'}}
        action=dict(type='control_document_monitor',command='resume',source='content_free_voice_control',
            local_only=True,cloud_transmission_allowed=False,voice_input_id=voice,
            voice_input_binding=AgentRuntimeStore.voice_capture_snapshot(capture),agent_runtime={})
        args=dict(command='resume',local_only=True,cloud_transmission_allowed=False)
        manifest={'voice_control_binding':{'request_key':'request','control':'document.resume','targets':binding}}
        receipt=dict(capability='document.monitor_control',execution_location='client_device_bridge',
            action_type='control_document_monitor',status='completed',completed_at=stamp(skew_seconds),
            output=dict(device_type='document_camera',device_status='active',frame_confirmed=True,
                lease_confirmed=True,persisted_status='active'))
        envelope={'client_bridge_receipt':receipt}
        put('agent_turn',turn,child,sid,quote,json.dumps(action),'completed',stamp(-2))
        put('agent_plan','plan',turn,child,sid,'completed',json.dumps(manifest))
        put('agent_step','step','plan',0,'document.monitor_control','client_device_bridge','completed',
            json.dumps(envelope),None,stamp(0),json.dumps(args))
        fingerprint=hashlib.sha256(json.dumps([child,sid,quote],ensure_ascii=False).encode()).hexdigest()
        put('voice_control_intent_request','request',child,sid,fingerprint,'completed',json.dumps(binding),
            json.dumps({'status':'control','control':'document.resume'}),stamp(-4),stamp(-3))
        put('sensor_lease',sid,'document_camera','active',stamp(-1),stamp(30),None)
        put('document_monitor_session',sid,child,'active')
        # Actual Runtime admission accepts this documented +/-30-second envelope.
        AgentRuntimeStore.validate_device_receipt(c,turn,envelope)
        c.execute("UPDATE sensor_lease SET status='released',released_at=?",(stamp(1),))
        c.execute("UPDATE document_monitor_session SET status='paused'")
        if historical_lease=='missing':
            c.execute('DELETE FROM sensor_lease')
        elif historical_lease=='later':
            c.execute('UPDATE sensor_lease SET opened_at=?',(stamp(2),))
        elif historical_lease=='expired':
            c.execute('UPDATE sensor_lease SET expires_at=?',(stamp(-.1),))
        elif historical_lease=='released_before':
            c.execute('UPDATE sensor_lease SET released_at=?',(stamp(-.1),))
        row=c.execute('SELECT * FROM agent_step WHERE step_id=?',('step',)).fetchone()
        source=dict(child_id=child,origin_session_id=sid,prompt_id=prompt,item_version=version)
        before=c.total_changes
        if historical_lease=='valid':
            projection.validate_document_control(c,row,source)
        else:
            with pytest.raises(ValueError,match='PAPER_DOCUMENT_CONTROL_CHANGED'):
                projection.validate_document_control(c,row,source)
        assert c.total_changes==before
    finally:
        c.close()
