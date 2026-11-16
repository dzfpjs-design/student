"""Local ownership and durable evidence for a Tutor's immediate check.

A prompted response is not a diagnostic grade, independent evidence, or mastery.
Use the existing feedback store so privacy export/deletion and backup retain it.
"""
import json
from uuid import uuid4

from .repository import StudyRepository, utc_now
from .teaching_question_target import resolve_question_target
from .tutor_playback import TutorPlaybackService, TutorPlaybackError


class TutorCheckError(ValueError):
    pass


def _scope(c, session_id):
    session=TutorPlaybackService._session(c,session_id)
    if c.execute("""SELECT 1 FROM deletion_job WHERE child_id=? AND target_type='child_scope'
            AND scope IN ('learning','knowledge','media','tutor','all_history') AND status!='completed'""",(session['child_id'],)).fetchone():
        raise TutorCheckError('TUTOR_CHECK_SCOPE_UNAVAILABLE')
    return session


def _current(c, session_id):
    session=_scope(c,session_id)
    row=c.execute('SELECT * FROM tutor_turn WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                  (session_id,session['child_id'])).fetchone()
    if not row or row['hint_level']!=1:
        raise TutorCheckError('TUTOR_CHECK_NOT_AVAILABLE')
    # Playback owns this lazy schema. A persisted lesson can predate its first
    # presentation; absence is no presentation evidence, including after restart.
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tutor_playback_checkpoint'").fetchone():
        raise TutorCheckError('TUTOR_CHECK_NOT_PRESENTED')
    source,board=TutorPlaybackService._source(c,session,row['turn_id'])
    playback=c.execute('SELECT * FROM tutor_playback_checkpoint WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                       (session_id,session['child_id'])).fetchone()
    if (not playback or playback['turn_id']!=row['turn_id'] or playback['source_version']!=source
            or playback['status']!='playback_finished'
            or json.loads(playback['presentation_json'])!=board):
        raise TutorCheckError('TUTOR_CHECK_NOT_PRESENTED')
    target=json.loads(row['output_envelope_json']).get('local_teaching_control',{}).get('question_target')
    current=resolve_question_target(c,session_id,subject=row['subject'],question=row['question'],parent_turn_id=row['turn_id'])
    if (not isinstance(target,dict) or current['status']!='resolved'
            or target.get('fingerprint')!=current['fingerprint']):
        raise TutorCheckError('TUTOR_CHECK_SOURCE_CHANGED')
    question=board['segments'][-1]['narration'].strip()
    if not question.endswith(('？','?')):
        raise TutorCheckError('TUTOR_CHECK_NOT_AVAILABLE')
    return row,{'turn_id':row['turn_id'],'source_version':source},question


class TutorCheckResponseService:
    def __init__(self,database):
        self.database=database

    @staticmethod
    def _saved(c,feedback_id):
        return StudyRepository._get_tutor_feedback(c,feedback_id)

    @staticmethod
    def _view(row,binding,question,response=None):
        return {'status':'response_saved' if response else 'waiting_response',
                'session_id':row['session_id'],'child_id':row['child_id'],'binding':binding,
                'question':question,'subject':row['subject'],'response':response,
                'local_only':True,'cloud_transmission_allowed':False,
                'mastery_update_allowed':False,'independent_evidence':False}

    def read(self,session_id):
        with self.database.connect() as c:
            c.execute('BEGIN')
            try:row,binding,question=_current(c,session_id)
            except (TutorCheckError,TutorPlaybackError,ValueError,KeyError,TypeError):
                return {'status':'unavailable','local_only':True,'cloud_transmission_allowed':False,
                        'mastery_update_allowed':False,'independent_evidence':False}
            response=c.execute("SELECT feedback_id FROM tutor_feedback WHERE turn_id=? AND evidence_basis='tutor_check_response' ORDER BY rowid DESC LIMIT 1",(row['turn_id'],)).fetchone()
            return self._view(row,binding,question,self._saved(c,response[0]) if response else None)

    def save(self,session_id,*,binding,response_text,client_action_id,pre_persist=None):
        if not isinstance(response_text,str) or not response_text.strip():
            raise TutorCheckError('TUTOR_CHECK_EMPTY_RESPONSE')
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT * FROM tutor_feedback WHERE client_action_id=?',(client_action_id,)).fetchone()
            if existing:
                session=_scope(c,session_id)
                parent=c.execute('SELECT * FROM tutor_turn WHERE turn_id=?',(existing['turn_id'],)).fetchone()
                from .tutor_playback import _hash
                if (not parent or existing['session_id']!=session_id or existing['child_id']!=session['child_id']
                        or existing['response_text']!=response_text or existing['outcome']!='answer_attempt'
                        or existing['evidence_basis']!='tutor_check_response'
                        or binding!={'turn_id':parent['turn_id'],'source_version':_hash(dict(parent),'tutor_source_v1')}):
                    raise TutorCheckError('TUTOR_CHECK_RESPONSE_CONFLICT')
                if pre_persist:pre_persist(c)
                question=json.loads(parent['output_envelope_json'])['result']['blackboard']['segments'][-1]['narration']
                return self._view(parent,binding,question,self._saved(c,existing['feedback_id']))
            try:row,current,question=_current(c,session_id)
            except (TutorPlaybackError,ValueError,KeyError,TypeError) as error:
                raise TutorCheckError('TUTOR_CHECK_SOURCE_CHANGED') from error
            if binding!=current:raise TutorCheckError('TUTOR_CHECK_BINDING_CHANGED')
            if pre_persist:pre_persist(c)
            existing=c.execute('SELECT * FROM tutor_feedback WHERE client_action_id=?',(client_action_id,)).fetchone()
            if existing:
                if (existing['turn_id']!=row['turn_id'] or existing['session_id']!=session_id
                        or existing['child_id']!=row['child_id'] or existing['response_text']!=response_text
                        or existing['evidence_basis']!='tutor_check_response' or existing['outcome']!='answer_attempt'):
                    raise TutorCheckError('TUTOR_CHECK_RESPONSE_CONFLICT')
                return self._view(row,current,question,self._saved(c,existing['feedback_id']))
            if c.execute("SELECT 1 FROM tutor_feedback WHERE turn_id=? AND evidence_basis='tutor_check_response'",(row['turn_id'],)).fetchone():
                raise TutorCheckError('TUTOR_CHECK_ALREADY_ANSWERED')
            feedback_id=str(uuid4())
            c.execute('''INSERT INTO tutor_feedback(feedback_id,turn_id,child_id,session_id,response_text,outcome,
                error_hypothesis,confidence,evidence_basis,created_at,client_action_id)
                VALUES (?,?,?,?,?,'answer_attempt','提示后作答，尚未独立核验',0,'tutor_check_response',?,?)''',
                (feedback_id,row['turn_id'],row['child_id'],session_id,response_text,utc_now(),client_action_id))
            response=self._saved(c,feedback_id);c.commit()
            return self._view(row,current,question,response)


def capture_check_binding(c,session_id,child_id,capture):
    """Freeze the displayed check before the content-free role call.

    The original diagnostic reference identifies the source, not a fresh grade.
    Audio admitted before the check ended cannot answer that later question.
    """
    from datetime import datetime
    try:
        row,binding,_=_current(c,session_id)
        if row['child_id']!=child_id or not capture or capture.get('question_validation')!='valid':return None
        if any(capture.get(k) for k in ('activity_reference','pending_evaluation_turn_id','local_recovery_only')):return None
        target=json.loads(row['output_envelope_json'])['local_teaching_control']['question_target']['binding']
        expected={'kind':'diagnostic_help','binding':{k:target[k] for k in ('prompt_id','item_version')}}
        if capture.get('question_reference')!=expected:return None
        voice=c.execute('SELECT created_at,speech_started_at FROM voice_capture_binding WHERE voice_input_id=? AND child_id=? AND session_id=?',
                        (capture.get('voice_input_id'),child_id,session_id)).fetchone()
        checkpoint=c.execute('SELECT updated_at FROM tutor_playback_checkpoint WHERE turn_id=? ORDER BY rowid DESC LIMIT 1',(row['turn_id'],)).fetchone()
        if not voice or not checkpoint:return None
        onset=datetime.fromisoformat((voice['speech_started_at'] or voice['created_at']).replace('Z','+00:00'))
        ended=datetime.fromisoformat(checkpoint['updated_at'].replace('Z','+00:00'))
        if onset.tzinfo is None or ended.tzinfo is None or onset<ended:return None
        if c.execute("SELECT 1 FROM tutor_feedback WHERE turn_id=? AND evidence_basis='tutor_check_response'",(row['turn_id'],)).fetchone():return None
        return {**binding,'voice_input_id':capture['voice_input_id']}
    except (TutorCheckError,TutorPlaybackError,ValueError,KeyError,TypeError):return None


def _authorized_runtime_response(c,turn_id):
    """Check the saved ASR, classified role and frozen owner at the write gate."""
    from .realtime_native_sources import tutor_check_source
    native=tutor_check_source(c,turn_id)
    if native is not None:return native
    import hashlib
    from .agent_runtime import AgentRuntimeStore
    row=c.execute('''SELECT t.*,s.capability_name,s.arguments_json,p.context_manifest_json
        FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
        JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0 WHERE t.agent_turn_id=?''',(turn_id,)).fetchone()
    if not row or row['capability_name']!='tutor.capture_check_response':raise TutorCheckError('TUTOR_CHECK_ROLE_UNAVAILABLE')
    args=json.loads(row['arguments_json']);proof=json.loads(row['context_manifest_json']).get('tutor_check_response_binding') or {}
    journal=c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=? AND session_id=? AND child_id=?',
                      (proof.get('request_key'),row['session_id'],row['child_id'])).fetchone()
    expected=hashlib.sha256(json.dumps([row['child_id'],row['session_id'],row['transcript']],ensure_ascii=False).encode()).hexdigest()
    decision=json.loads(journal['decision_json'] or 'null') if journal else None
    frozen=(json.loads(journal['binding_json']).get('tutor_check') if journal else None)
    if (not journal or journal['fingerprint']!=expected or not isinstance(decision,dict)
            or decision.get('status')!='non_control' or decision.get('role')!='answer'
            or decision.get('role_version')!='spoken_answer_role_v1' or frozen!=proof.get('target')
            or args.get('response')!=row['transcript'] or not isinstance(frozen,dict)
            or {k:args.get(k) for k in ('turn_id','source_version')}!={k:frozen.get(k) for k in ('turn_id','source_version')}):
        raise TutorCheckError('TUTOR_CHECK_ROLE_UNAVAILABLE')
    capture=AgentRuntimeStore.resolve_captured_voice(c,session_id=row['session_id'],child_id=row['child_id'],
        voice_input_id=frozen['voice_input_id'],transcript=row['transcript'])
    # An exact replay may find the response already saved; ownership still has
    # to be valid, but it need not open a new pending slot.
    existing=c.execute("SELECT 1 FROM tutor_feedback WHERE client_action_id=? AND turn_id=? AND evidence_basis='tutor_check_response'",
                       ('tutor-check:'+turn_id,frozen['turn_id'])).fetchone()
    current=capture_check_binding(c,row['session_id'],row['child_id'],capture)
    if not existing and current!=frozen:raise TutorCheckError('TUTOR_CHECK_SOURCE_CHANGED')
    return row,args


def authorized_runtime_response(c,turn_id):
    try:return _authorized_runtime_response(c,turn_id)
    except (ValueError,KeyError,TypeError) as error:
        if isinstance(error,TutorCheckError):raise
        raise TutorCheckError('TUTOR_CHECK_SOURCE_CHANGED') from error
