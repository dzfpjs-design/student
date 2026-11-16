"""Metadata-only delivery receipts for real Tutor answers without a board.

These are renderer natural callback acknowledgements, never acoustic or learning
proof. New reserve generations fence every old playback, including terminal rows.
"""
from hashlib import sha256
import json
from uuid import uuid4
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, Optional
from .repository import utc_now
from .voice_capture_binding import VoiceCaptureBindingError

TABLE_SQL = '''CREATE TABLE IF NOT EXISTS tutor_voice_playback (
 playback_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES tutor_turn(turn_id) ON DELETE CASCADE,
 session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
 child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
 source_version TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=1),
 lease_id TEXT, lease_generation INTEGER, lease_kind TEXT,
 status TEXT NOT NULL CHECK(status IN ('reserved','started','finished','cancelled')),
 client_action_id TEXT NOT NULL UNIQUE, event_source TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(session_id,child_id,generation))'''


def source(c, session_id, child_id, turn_id):
    from .tutor_playback import TutorPlaybackService
    from .teaching_presentation_scope import replaced_by_question
    row=c.execute('SELECT * FROM tutor_turn WHERE turn_id=? AND session_id=? AND child_id=?',
                  (turn_id,session_id,child_id)).fetchone()
    latest=c.execute('SELECT turn_id FROM tutor_turn WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                     (session_id,child_id)).fetchone()
    try:session=TutorPlaybackService._session(c,session_id)
    except ValueError as exc:raise VoiceCaptureBindingError('VOICE_RESPONSE_TUTOR_SOURCE_CHANGED') from exc
    if (session['child_id']!=child_id or not row or not latest or latest[0]!=turn_id
            or row['status']!='completed' or row['safety_status']!='passed'
            or not isinstance(row['answer'],str) or not row['answer'].strip()
            or replaced_by_question(c,session_id,child_id,row['created_at'])):
        raise VoiceCaptureBindingError('VOICE_RESPONSE_TUTOR_SOURCE_CHANGED')
    try:
        envelope=json.loads(row['output_envelope_json'] or 'null')
        if not isinstance(envelope,dict) or not isinstance(envelope.get('result'),dict):raise ValueError('missing envelope')
        from .mistake_teaching_authority import validate_saved_teaching
        validate_saved_teaching(c,row,envelope)
        from .tutor_check_coaching import validate_saved
        validate_saved(c,row,envelope)
        from .reviewed_tutor_projection import is_projected
        target=(envelope.get('local_teaching_control') or {}).get('question_target') or {}
        if is_projected(target):
            from .teaching_question_target import resolve_question_target
            current=resolve_question_target(c,session_id,subject=row['subject'],question=row['question'],parent_turn_id=turn_id)
            if not is_projected(current) or current['fingerprint']!=target.get('fingerprint'):raise ValueError('changed target')
        if envelope['result'].get('local_visual'):
            from .visual_teaching import read_turn
            read_turn(c,session_id,turn_id)
        if envelope.get('document_revisit'):
            from .document_revisit_feedback import read_saved
            read_saved(c,row)
        if row['plan_item_id']:
            owner=c.execute('''SELECT dp.child_id,dp.session_id FROM plan_item pi
                  JOIN daily_plan dp USING(plan_id) WHERE pi.plan_item_id=?''',(row['plan_item_id'],)).fetchone()
            if not owner or tuple(owner)!=(child_id,session_id):raise ValueError('changed plan')
    except (ValueError,TypeError,KeyError,OSError,AttributeError) as exc:
        raise VoiceCaptureBindingError('VOICE_RESPONSE_TUTOR_SOURCE_CHANGED') from exc
    version='tutor_voice_source_v1:'+sha256(json.dumps(dict(row),sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
    return version,envelope['result'].get('blackboard') is not None


def validate_finished(c, session_id, child_id, turn_id, playback_id):
    version,has_board=source(c,session_id,child_id,turn_id)
    if has_board:raise VoiceCaptureBindingError('VOICE_RESPONSE_BOARD_RECEIPT_REQUIRED')
    row=c.execute('SELECT * FROM tutor_voice_playback WHERE playback_id=?',(playback_id,)).fetchone()
    latest=c.execute('SELECT MAX(generation) FROM tutor_voice_playback WHERE session_id=? AND child_id=?',
                     (session_id,child_id)).fetchone()[0]
    if (not row or (row['session_id'],row['child_id'],row['turn_id'],row['source_version'])!=(session_id,child_id,turn_id,version)
            or row['generation']!=latest or row['status']!='finished'
            or row['event_source']!='renderer_natural_end_callback_v1' or not row['lease_id']
            or row['lease_generation'] is None or row['lease_kind'] not in {'local_sapi','managed_audio','local_pcm'}):
        raise VoiceCaptureBindingError('VOICE_RESPONSE_TUTOR_DELIVERY_REQUIRED')
    return dict(row)


class Reserve(BaseModel):
    model_config=ConfigDict(extra='forbid')
    turn_id:str=Field(min_length=1,max_length=128,strict=True,pattern=r'^[A-Za-z0-9_.:-]+$')
    client_action_id:str=Field(min_length=8,max_length=128,strict=True,pattern=r'^[A-Za-z0-9_.:-]+$')

class Token(BaseModel):
    model_config=ConfigDict(extra='forbid')
    playback_id:str=Field(min_length=1,max_length=128,strict=True,pattern=r'^[A-Za-z0-9_.:-]+$')
    source_version:str=Field(min_length=1,max_length=128,strict=True,pattern=r'^[A-Za-z0-9_.:-]+$')
    generation:int=Field(ge=1,le=2147483647,strict=True)
    lease_id:Optional[str]=Field(default=None,min_length=1,max_length=128,strict=True,pattern=r'^[A-Za-z0-9_.:-]+$')
    lease_generation:Optional[int]=Field(default=None,ge=1,le=2147483647,strict=True)
    lease_kind:Optional[Literal['local_sapi','managed_audio','local_pcm']]=None
    client_action_id:str=Field(min_length=8,max_length=128,strict=True,pattern=r'^[A-Za-z0-9_.:-]+$')


def view(row):
    return {k:row[k] for k in ('playback_id','turn_id','child_id','session_id','source_version','generation',
                              'status','lease_id','lease_generation','lease_kind')}


def create_router(database):
    router=APIRouter(prefix='/v1/sessions/{session_id}/tutor-voice-playback')
    @router.post('/reserve')
    def reserve(session_id:str,payload:Reserve):
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from .tutor_playback import TutorPlaybackService
            try:session=TutorPlaybackService._session(c,session_id)
            except ValueError as exc:raise VoiceCaptureBindingError('VOICE_RESPONSE_TUTOR_SOURCE_CHANGED') from exc
            child=session['child_id']
            old=c.execute('SELECT * FROM tutor_voice_playback WHERE client_action_id=?',(payload.client_action_id,)).fetchone()
            if old:
                if (old['session_id'],old['child_id'],old['turn_id'])!=(session_id,child,payload.turn_id):
                    raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_CONFLICT')
                return view(old)  # Never revive an old/terminal generation.
            version,board=source(c,session_id,child,payload.turn_id)
            if board:raise VoiceCaptureBindingError('VOICE_RESPONSE_BOARD_RECEIPT_REQUIRED')
            generation=1+c.execute('SELECT COALESCE(MAX(generation),0) FROM tutor_voice_playback WHERE session_id=? AND child_id=?',
                                   (session_id,child)).fetchone()[0]
            now=utc_now();pid=str(uuid4())
            c.execute("UPDATE tutor_voice_playback SET status='cancelled',updated_at=? WHERE session_id=? AND child_id=? AND status IN ('reserved','started')",(now,session_id,child))
            c.execute('''INSERT INTO tutor_voice_playback(playback_id,turn_id,session_id,child_id,source_version,generation,status,client_action_id,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,'reserved',?,?,?)''',(pid,payload.turn_id,session_id,child,version,generation,payload.client_action_id,now,now))
            row=c.execute('SELECT * FROM tutor_voice_playback WHERE playback_id=?',(pid,)).fetchone();c.commit();return view(row)
    def event(session_id,payload,operation):
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            session=c.execute('SELECT child_id FROM daily_session WHERE session_id=?',(session_id,)).fetchone()
            row=c.execute('SELECT * FROM tutor_voice_playback WHERE playback_id=?',(payload.playback_id,)).fetchone()
            if not session or not row or (row['session_id'],row['child_id'])!=(session_id,session['child_id']):
                raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_OWNER_CHANGED')
            if (row['source_version'],row['generation'])!=(payload.source_version,payload.generation):
                raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_CONFLICT')
            lease=(payload.lease_id,payload.lease_generation,payload.lease_kind)
            stored=(row['lease_id'],row['lease_generation'],row['lease_kind'])
            if operation=='cancel':
                if (any(x is not None for x in lease) or row['status']!='reserved') and lease!=stored:raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_CONFLICT')
                c.execute("UPDATE tutor_voice_playback SET status='cancelled',updated_at=? WHERE playback_id=?",(utc_now(),row['playback_id']))
            else:
                version,board=source(c,session_id,row['child_id'],row['turn_id'])
                latest=c.execute('SELECT MAX(generation) FROM tutor_voice_playback WHERE session_id=? AND child_id=?',(session_id,row['child_id'])).fetchone()[0]
                if board or version!=row['source_version'] or latest!=row['generation'] or row['status']=='cancelled':
                    raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_SOURCE_CHANGED')
                if any(x is None for x in lease):raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_LEASE_REQUIRED')
                if operation=='start':
                    if row['status']=='reserved':
                        c.execute("UPDATE tutor_voice_playback SET status='started',lease_id=?,lease_generation=?,lease_kind=?,updated_at=? WHERE playback_id=?",(*lease,utc_now(),row['playback_id']))
                    elif row['status']!='started' or lease!=stored:raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_CONFLICT')
                elif row['status'] not in {'started','finished'} or lease!=stored:
                    raise VoiceCaptureBindingError('VOICE_RESPONSE_PLAYBACK_CONFLICT')
                elif row['status']=='started':c.execute("UPDATE tutor_voice_playback SET status='finished',event_source='renderer_natural_end_callback_v1',updated_at=? WHERE playback_id=?",(utc_now(),row['playback_id']))
            result=c.execute('SELECT * FROM tutor_voice_playback WHERE playback_id=?',(row['playback_id'],)).fetchone();c.commit();return view(result)
    @router.post('/start')
    def start(session_id:str,payload:Token):return event(session_id,payload,'start')
    @router.post('/natural-end')
    def natural_end(session_id:str,payload:Token):return event(session_id,payload,'end')
    @router.post('/cancel')
    def cancel(session_id:str,payload:Token):return event(session_id,payload,'cancel')
    return router
