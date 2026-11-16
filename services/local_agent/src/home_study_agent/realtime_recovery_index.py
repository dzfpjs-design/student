"""Bounded local identity discovery, never speech export or replay permission."""
from .voice_capture_binding import VoiceCaptureBindingError, read_realtime_admission_receipt


def read_realtime_recovery_index(database, session_id, child_id):
    with database.connect() as c:
        c.execute('BEGIN')
        owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
        if owner is None or owner['child_id'] != child_id:
            raise VoiceCaptureBindingError('VOICE_CAPTURE_OWNER_CHANGED')
        rows = c.execute('''SELECT b.client_action_id,b.created_at FROM voice_capture_binding b
            WHERE b.session_id=? AND b.child_id=? AND b.source_kind='realtime_transcript'
              AND b.status IN ('prepared','completed') AND b.local_recovery_only=0
              AND NOT EXISTS (SELECT 1 FROM voice_transcription_action a WHERE a.client_action_id=b.client_action_id
                AND a.session_id=b.session_id AND a.child_id=b.child_id AND a.status IN ('cancelled','ignored'))
              AND NOT EXISTS (SELECT 1 FROM agent_turn t WHERE t.session_id=b.session_id AND t.child_id=b.child_id
                AND t.tool_status IN ('completed','failed') AND
                (t.client_action_id='realtime-'||b.voice_input_id OR
                 json_extract(CASE WHEN json_valid(t.action_json) THEN t.action_json ELSE '{}' END,'$.voice_input_id')=b.voice_input_id))
            ORDER BY b.created_at,b.rowid LIMIT 129''', (session_id, child_id)).fetchall()
        entries = []
        for row in rows[:128]:
            receipt = read_realtime_admission_receipt(database, session_id, row['client_action_id'], connection=c)
            if receipt['status'] not in ('prepared', 'completed'):
                raise VoiceCaptureBindingError('VOICE_CAPTURE_ACTION_CONFLICT')
            entries.append({key: receipt[key] for key in ('client_action_id', 'voice_input_id', 'status')} |
                           {'created_at': row['created_at']})
    return {'session_id': session_id, 'child_id': child_id, 'entries': entries, 'has_more': len(rows) > 128,
            'local_only': True, 'cloud_transmission_allowed': False, 'replay_allowed': False, 'mastery_update_allowed': False}
