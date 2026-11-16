"""Recheck the exact Tutor context before each physical provider submission."""
from .cloud_privacy import build_tutor_cloud_context
from . import realtime_native_curriculum
from .teaching_question_target import resolve_question_target
from .tutor_ocr_confirmation import OcrTutorConfirmationError


class TutorDispatchRevokedError(Exception):
    code = 'TUTOR_SOURCE_AUTHORIZATION_CHANGED'


def text_consent_version(connection):
    row = connection.execute("""SELECT consent_id,allowed,policy_version,source,occurred_at FROM consent_record
        WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1""").fetchone()
    return tuple(row) if row else None


def revalidate_tutor_dispatch(*, database, repository, knowledge_base, confirmations,
        session_id, child_id, action_id, request_hash, subject, question,
        prompt_id, item_version, parent_turn_id, ocr_confirmation_id,
        question_target, expected_user_prompt, consent_version, expected_attempt,
        accepting_result=False,check_response_id=None,check_response_binding=None):
    with database.connect() as c:
        c.execute('BEGIN')
        session = c.execute('''SELECT ds.child_id,ds.status,cp.status AS child_status
            FROM daily_session ds JOIN child_profile cp USING(child_id) WHERE session_id=?''', (session_id,)).fetchone()
        privacy = c.execute('SELECT cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        reservation = c.execute('SELECT * FROM tutor_request_reservation WHERE action_id=?', (action_id,)).fetchone()
        pending = c.execute("""SELECT 1 FROM deletion_job WHERE child_id=? AND target_type='child_scope'
            AND scope IN ('learning','knowledge','media','tutor','all_history') AND status!='completed' LIMIT 1""", (child_id,)).fetchone()
        if (not session or (session['child_id'],session['status'],session['child_status'])!=(child_id,'active','active')
                or not privacy or not privacy[0] or text_consent_version(c)!=consent_version or pending or not reservation
                or (reservation['child_id'],reservation['session_id'],reservation['request_hash'],reservation['attempts'])
                    !=(child_id,session_id,request_hash,expected_attempt)
                or not (reservation['status']=='pending' or (
                    accepting_result and reservation['status']=='interrupted'
                    and reservation['last_error_code']=='INTERRUPTED_UNKNOWN_OUTCOME'))):
            raise TutorDispatchRevokedError()
        current = resolve_question_target(c,session_id,subject=subject,question=question,
            prompt_id=prompt_id,item_version=item_version,parent_turn_id=parent_turn_id,
            ocr_confirmation_id=ocr_confirmation_id)
        if (current['status']=='invalid' or current.get('question_content_cloud_blocked',True)
                or any(current.get(key)!=question_target.get(key) for key in
                       ('status','binding','question_hash','source_fingerprint','fingerprint'))):
            raise TutorDispatchRevokedError()
        from .learning_question_help import validate_dispatch
        try:
            from .foreground_independent_dialogue import tutor_request
            independent = tutor_request(c,session_id=session_id,child_id=child_id,action_id=action_id,
                question=question,subject=subject,prompt_id=prompt_id,item_version=item_version,
                parent_turn_id=parent_turn_id,ocr_confirmation_id=ocr_confirmation_id)
            validate_dispatch(c,action_id,session_id,child_id,request_target=dict(
                question=question,subject=subject,prompt_id=prompt_id,item_version=item_version))
            native_curriculum = realtime_native_curriculum.for_tutor(c, session_id, action_id)
        except (ValueError,KeyError,TypeError) as error:
            raise TutorDispatchRevokedError() from error
    if ocr_confirmation_id:
        try:
            original = confirmations.resolve_for_tutor(session_id,ocr_confirmation_id)
        except OcrTutorConfirmationError as error:
            raise TutorDispatchRevokedError() from error
        if original.confirmed_text!=question:
            raise TutorDispatchRevokedError()
    context = repository.tutoring_context(session_id)
    names = [child['nickname'] for child in repository.list_children()]
    if native_curriculum:
        current = realtime_native_curriculum.cloud_context(native_curriculum, grade=context['child'].get('grade','未知'),
            region=context['child'].get('curriculum_region','未知'), subject=subject, question=question, names=names)
    else:
        references = [] if independent else knowledge_base.grounding_context(child_id,subject,question,2)
        current = build_tutor_cloud_context(grade=context['child'].get('grade','未知'),
            curriculum_region=context['child'].get('curriculum_region','未知'),subject=subject,question=question,
            task=None if independent else context['task'],recalls=[] if independent else context['recalls'],
            known_child_names=names,knowledge_context=references)
    from .reviewed_tutor_projection import is_projected, cloud_context as projected_tutor_context
    if is_projected(question_target):
        current = projected_tutor_context(subject=subject, question=question,
            known_child_names=[child['nickname'] for child in repository.list_children()])
    if check_response_id:
        from .tutor_check_coaching import projection
        try:
            with database.connect() as c:
                c.execute('BEGIN')
                current,_=projection(c,session_id=session_id,child_id=child_id,parent_turn_id=parent_turn_id,
                    feedback_id=check_response_id,expected_binding=check_response_binding)
        except (ValueError,KeyError,TypeError) as error:
            raise TutorDispatchRevokedError() from error
    if current.user_prompt!=expected_user_prompt:
        raise TutorDispatchRevokedError()
