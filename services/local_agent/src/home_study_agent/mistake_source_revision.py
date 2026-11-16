"""Child-confirmed question revisions keep the original case and local provenance."""
import json
from datetime import datetime
from uuid import uuid4

from .local_supply import digest
from .repository import utc_now


def migrate(c):
    c.execute('''CREATE TABLE IF NOT EXISTS mistake_source_revision (
        revision_id TEXT PRIMARY KEY,
        mistake_id TEXT NOT NULL REFERENCES mistake_case(mistake_id) ON DELETE CASCADE,
        child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK(version > 0),
        question TEXT NOT NULL, artifact_id TEXT NOT NULL,
        before_json TEXT NOT NULL, after_hash TEXT NOT NULL,
        offer_json TEXT NOT NULL, confirmation_turn_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL, UNIQUE(mistake_id,version))''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_mistake_source_revision_child ON mistake_source_revision(child_id,mistake_id,version)')
    c.execute('''CREATE TRIGGER IF NOT EXISTS mistake_source_revision_no_update
        BEFORE UPDATE ON mistake_source_revision BEGIN SELECT RAISE(ABORT,'question revisions are append-only'); END''')
    c.execute('''CREATE TRIGGER IF NOT EXISTS mistake_source_revision_owner
        BEFORE INSERT ON mistake_source_revision BEGIN
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM mistake_case m JOIN daily_session s ON s.child_id=m.child_id
            WHERE m.mistake_id=NEW.mistake_id AND m.child_id=NEW.child_id AND s.session_id=NEW.session_id)
            THEN RAISE(ABORT,'question revision owner mismatch') END; END''')


def latest(c, mistake_id, child_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='mistake_source_revision'").fetchone():
        return None
    return c.execute('SELECT * FROM mistake_source_revision WHERE mistake_id=? AND child_id=? ORDER BY version DESC LIMIT 1',
                     (mistake_id, child_id)).fetchone()


def snapshot(c, mistake_id, child_id):
    row = c.execute('''SELECT knowledge_node_id,diagnostic_item_id,source_evidence_id,prompt_summary,
        submitted_answer,source_type,source_ref FROM mistake_case WHERE mistake_id=? AND child_id=?''',
        (mistake_id, child_id)).fetchone()
    if not row or row['source_type'] != 'document_camera':
        return None
    # Standalone scoring can operate without the optional OCR review store.
    # Its absence cannot grant a complete source snapshot or break scoring.
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ocr_review_item'").fetchone():
        return None
    artifact_id, separator, candidate_id = (row['source_ref'] or '').partition('#')
    if not separator:
        return None
    artifact = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=?', (artifact_id, child_id)).fetchone()
    candidate = c.execute('SELECT * FROM mistake_capture_candidate WHERE candidate_id=? AND child_id=? AND mistake_id=?',
                          (candidate_id, child_id, mistake_id)).fetchone()
    case_status = c.execute('SELECT status FROM mistake_case WHERE mistake_id=? AND child_id=?',
                           (mistake_id, child_id)).fetchone()
    if (not artifact or not candidate or candidate['artifact_id'] != artifact_id
            or not case_status or case_status[0] == 'excluded'
            or candidate['status'] != 'saved'):
        return None
    source = dict(artifact)
    try:
        details = json.loads(source['details_json'])
    except (ValueError, TypeError):
        return None
    if not isinstance(details, dict):
        return None
    details.pop('visual_capture', None)
    source['details_json'] = json.dumps(details, sort_keys=True, ensure_ascii=False)
    reviews = [dict(r) for r in c.execute('SELECT * FROM ocr_review_item WHERE artifact_id=? AND child_id=? ORDER BY review_id',
                                        (artifact_id, child_id))]
    return {'case': dict(row), 'artifact': source, 'candidate': dict(candidate), 'reviews': reviews}


def current(c, mistake_id, child_id):
    row = latest(c, mistake_id, child_id)
    if not row:
        return None
    source = snapshot(c, mistake_id, child_id)
    before = json.loads(row['before_json'])
    offer_value = json.loads(row['offer_json'])
    recaptured = bool(offer_value.get('recapture'))
    status = 'confirmed' if source and digest(source) == row['after_hash'] else 'invalidated'
    question = row['question']
    # A later independently reviewed formula may refine the recaptured question,
    # but cannot excuse a changed case, answer, candidate or original OCR payload.
    if (recaptured and status == 'invalidated' and source
            and offer_value.get('accepted_core_hash') == digest({k: v for k, v in source.items() if k != 'reviews'})):
        from .paper_formula_review import resolve
        try:
            review = resolve(c, source['artifact'], source['candidate'])
        except (ValueError, TypeError, KeyError):
            review = None
        if review:
            question, status = review['question'], 'confirmed'
    return {'status': status,
            'question': question, 'original_question': before['case']['prompt_summary'],
            'review_id': row['revision_id'], 'version': digest([row['revision_id'], row['after_hash'], source]),
            'authority': 'child_confirmed_recapture' if recaptured else 'child_spoken_correction', 'score_authority': False,
            'revision_session_id': row['session_id'], 'artifact_id': row['artifact_id']}


def artifact_binding(c, artifact_id, child_id, text, session_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='mistake_source_revision'").fetchone():
        return None
    from .tutor_ocr_confirmation import normalize_confirmation_text
    rows = c.execute('''SELECT DISTINCT mistake_id FROM mistake_source_revision
        WHERE artifact_id=? AND child_id=?''', (artifact_id, child_id)).fetchall()
    matches = []
    for row in rows:
        value = current(c, row['mistake_id'], child_id)
        if value and value['artifact_id'] == artifact_id and normalize_confirmation_text(value['question']) == normalize_confirmation_text(text):
            owner = c.execute('SELECT 1 FROM daily_session WHERE session_id=? AND child_id=?',
                              (session_id, child_id)).fetchone()
            # A new same-child confirmation may reuse a historical revision.
            # Callers still require its exact binding and their own session row.
            if value['status'] != 'confirmed' or not owner:
                raise ValueError('question revision changed')
            matches.append({'review_id': value['review_id'], 'version': value['version']})
    if len(matches) > 1:
        raise ValueError('ambiguous question revision')
    return matches[0] if matches else None


def target(c, focus):
    if (not focus or not focus.get('mistake_id') or focus.get('phase') in {'paused', 'closed'}
            or focus['state'].get('pending_operation') or focus['state'].get('foundation_hold')):
        return None
    source = snapshot(c, focus['mistake_id'], focus['child_id'])
    if not source:
        return None
    return {'mistake_id': focus['mistake_id'], 'source_version': digest(source),
            'pending_version': digest(focus['state'].get('question_confirmation'))}


def readback(pending):
    if 'replacement_answer' in pending:
        return (f"我把待核对的两项一起读回。题干：{pending['question']}。你原来的作答记录：{pending['replacement_answer']}。"
                '这两项都对吗？只确认题干不会修改原答案。这不是一次新的作答，也不会记为答对。')
    return (f"我读回你的更正：{pending['question']}。原来记录的答案是“{pending['original_answer']}”。"
            '这是刚才同一道题的正确题干，而且原答案没变，对吗？确认前不会修改原记录。')


def accepts_joint(text):
    import re
    value = re.sub(r'[，,。！!\s]', '', text)
    return bool(re.fullmatch(r'(?:(?:题干和原答案|题干和原作答)(?:两项|两处)?|(?:这)?(?:两项|两处|两个))(?:都对|都正确|都准确)'
                            r'(?:(?:就|请)?(?:讲|讲解)(?:吧)?)?', value))


def offer(dialogue, focus, question, action_id, utterance, *, replacement_answer=None):
    from .mistake_capture import InvalidMistakeCaptureCandidateError
    if (not focus.get('mistake_id') or focus['state'].get('pending_operation')
            or focus.get('phase') in {'paused', 'closed'} or focus['state'].get('foundation_hold')
            or (question is None and replacement_answer is None)
            or (question is not None and (not isinstance(question, str)
                or not 2 <= len(question.strip()) <= 2800 or question not in utterance))):
        raise InvalidMistakeCaptureCandidateError('先保留当前作答。更正时请读出完整题干，不能替换尚在保存的回答。')
    if replacement_answer is not None and (not isinstance(replacement_answer, str)
            or not 1 <= len(replacement_answer.strip()) <= 1000 or replacement_answer not in utterance):
        raise InvalidMistakeCaptureCandidateError('原作答更正必须来自你这次亲口说的内容，我不会猜答案。')
    with dialogue.database.connect() as c:
        source = snapshot(c, focus['mistake_id'], focus['child_id'])
        previous = latest(c, focus['mistake_id'], focus['child_id'])
        from .paper_formula_review import for_mistake
        reviewed = for_mistake(c, focus['mistake_id'], focus['child_id']) if question is None else None
    if not source:
        raise InvalidMistakeCaptureCandidateError('这道题的原拍摄记录暂时无法核对，请保留原题并重新拍摄。')
    previous_offer = focus['state'].get('question_confirmation') or {}
    amend = previous_offer.get('mode') == 'source_repair' and not previous_offer.get('recapture')
    if amend and (previous_offer.get('version') != digest(source)
            or previous_offer.get('base_revision') != (previous['revision_id'] if previous else None)):
        raise InvalidMistakeCaptureCandidateError('待确认的提案依据已经变化，请重新核对原题和原作答。')
    retained_question_record = None
    if question is None:
        if previous_offer.get('recapture'):
            raise InvalidMistakeCaptureCandidateError('重拍的题干还没确认，请先核对这道题，再更正原作答。')
        if amend:
            question = previous_offer['question']
            retained_question_record = previous_offer.get('retained_question_record')
        else:
            question = reviewed['question'] if reviewed and reviewed['status'] == 'confirmed' else source['case']['prompt_summary']
            retained_question_record = source['case']['prompt_summary']
    if replacement_answer is None and amend:
        replacement_answer = previous_offer.get('replacement_answer')
    if not isinstance(question, str) or not 2 <= len(question.strip()) <= 2800:
        raise InvalidMistakeCaptureCandidateError('当前原题不能完整读回，请先重新拍摄或口述完整题干。')
    pending = {'mode': 'source_repair', 'mistake_id': focus['mistake_id'], 'question': question.strip(),
        'review_id': str(uuid4()), 'version': digest(source), 'base_revision': previous['revision_id'] if previous else None,
        'offer_id': action_id, 'created_at': utc_now(), 'original_answer': source['case']['submitted_answer']}
    if replacement_answer is not None:
        pending['replacement_answer'] = replacement_answer.strip()
    if amend:
        pending['amends_offer_id'] = previous_offer['offer_id']
    if retained_question_record is not None:
        pending['retained_question_record'] = retained_question_record
    superseded_capture = ((previous_offer.get('recapture') or {}).get('turn_id')
                          or previous_offer.get('superseded_recapture_turn_id'))
    if superseded_capture:
        pending['superseded_recapture_turn_id'] = superseded_capture
    # A spoken replacement keeps the original source. An unaccepted rephoto
    # must not leave the focus pointing at a different OCR artifact/candidate.
    focus['candidate_id'] = source['candidate']['candidate_id']
    focus['state'].update(artifact_id=source['artifact']['artifact_id'],
                          question_text=source['case']['prompt_summary'],
                          submitted_answer=source['case']['submitted_answer'])
    focus['state']['question_confirmation'] = pending
    focus['state'].pop('teaching_requested', None)
    focus['state'].pop('ocr_confirmation_id', None)
    focus['phase'] = 'remediation'
    return dialogue._save(focus, readback(pending), action_id, utterance)


def approve(database, focus, text, action_id, authorized_turn_id):
    from .mistake_question_confirmation import accepts
    from .diagnostic_prompt import _TransactionDatabase
    from .tutor_ocr_confirmation import OcrTutorConfirmationService
    from .mistake_dialogue import MistakeDialogueService
    pending = focus['state'].get('question_confirmation')
    if not authorized_turn_id or not pending or pending.get('mode') != 'source_repair':
        return False
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND session_id=? AND child_id=?',
            (authorized_turn_id, focus['session_id'], focus['child_id'])).fetchone()
        action = json.loads(turn['action_json']) if turn else {}
        from .mistake_revision_semantics import verifies
        if not ((accepts_joint(text) or verifies(c, action.get('revision_semantics'), focus, pending, text))
                if 'replacement_answer' in pending else accepts(text)):
            return False
        live = c.execute('SELECT mistake_id,phase,state_json FROM mistake_dialogue WHERE session_id=? AND child_id=?',
                         (focus['session_id'], focus['child_id'])).fetchone()
        steps = c.execute('''SELECT s.arguments_json FROM agent_step s JOIN agent_plan p USING(plan_id)
            JOIN daily_session d ON d.session_id=p.session_id AND d.child_id=p.child_id
            WHERE p.agent_turn_id=? AND p.status='running' AND s.status='running'
            AND s.capability_name='mistake.dialogue' AND d.status='active' AND d.state='study' ''',
            (authorized_turn_id,)).fetchall()
        if (not turn or turn['transcript'] != text or action.get('source') != 'mistake_question_voice_confirmation'
                or action.get('question_confirmation') != pending or not live or live['mistake_id'] != focus['mistake_id']
                or live['phase'] != 'remediation'
                or json.loads(live['state_json']).get('question_confirmation') != pending
                or len(steps) != 1 or json.loads(steps[0][0]).get('utterance') != text):
            return False
        if action.get('voice_input_id'):
            from .agent_runtime import AgentRuntimeStore
            captured = AgentRuntimeStore.resolve_captured_voice(c, session_id=focus['session_id'], child_id=focus['child_id'],
                voice_input_id=action['voice_input_id'], transcript=text)
            clock = c.execute('SELECT created_at FROM voice_capture_binding WHERE voice_input_id=?', (action['voice_input_id'],)).fetchone()
            if (AgentRuntimeStore.voice_capture_snapshot(captured) != action.get('voice_input_binding') or not clock
                    or datetime.fromisoformat(clock[0]) < datetime.fromisoformat(pending['created_at'])):
                return False
        if pending.get('recapture'):
            from .mistake_recapture import original
            source = original(c, focus['mistake_id'], focus['child_id'])
        else:
            source = snapshot(c, focus['mistake_id'], focus['child_id'])
        previous = latest(c, focus['mistake_id'], focus['child_id'])
        case = c.execute('SELECT status FROM mistake_case WHERE mistake_id=? AND child_id=?',
                         (focus['mistake_id'], focus['child_id'])).fetchone()
        if (not source or digest(source) != pending['version']
                or (previous['revision_id'] if previous else None) != pending['base_revision']
                or not case or case['status'] == 'excluded'):
            focus['state'].pop('question_confirmation', None)
            focus['state']['question_repair_error'] = 'source_changed'
            return False
        now = utc_now()
        before = {'case': source['case'], 'source_version': digest(source),
                  'status': case['status'], 'mastery_gate': dict(c.execute('SELECT * FROM mistake_mastery_gate WHERE mistake_id=?',
                    (focus['mistake_id'],)).fetchone() or {})}
        if pending.get('recapture'):
            from .mistake_recapture import verify_pending
            captured = verify_pending(c, focus, pending)
            artifact_id = captured['artifact']['artifact_id']
            candidate_id = captured['candidate']['candidate_id']
            c.execute('''UPDATE mistake_capture_candidate SET status='saved',mistake_id=?,updated_at=?
                WHERE candidate_id=? AND child_id=?''', (focus['mistake_id'], now, candidate_id, focus['child_id']))
            c.execute('UPDATE mistake_case SET source_ref=? WHERE mistake_id=? AND child_id=?',
                      (artifact_id + '#' + candidate_id, focus['mistake_id'], focus['child_id']))
            from .mistake_capture import MistakeCaptureService
            MistakeCaptureService._event(c, candidate_id, focus['child_id'], 'captured',
                {'mistake_id': focus['mistake_id'], 'source_revision': pending['review_id']},
                'recapture:' + authorized_turn_id, now)
            focus['candidate_id'] = candidate_id
            focus['state']['artifact_id'] = artifact_id
        else:
            artifact_id = source['artifact']['artifact_id']
        answer = pending.get('replacement_answer', source['case']['submitted_answer'])
        c.execute('UPDATE mistake_case SET prompt_summary=?,submitted_answer=? WHERE mistake_id=? AND child_id=?',
                  (pending.get('retained_question_record', pending['question']), answer, focus['mistake_id'], focus['child_id']))
        from .mistake_book import MistakeBookService
        MistakeBookService._transition(c, focus['mistake_id'], focus['child_id'], case['status'],
            'diagnosing', 'child_confirmed_question_revision', 'question-revision:' + authorized_turn_id, now)
        c.execute('''UPDATE mistake_mastery_gate SET updated_at=?,delayed_48h_due_at=NULL,delayed_7d_due_at=NULL
            WHERE mistake_id=?''', (now, focus['mistake_id']))
        offer_value = dict(pending)
        if pending.get('recapture'):
            accepted_source = snapshot(c, focus['mistake_id'], focus['child_id'])
            offer_value['accepted_core_hash'] = digest({k: v for k, v in accepted_source.items() if k != 'reviews'})
        c.execute('''INSERT INTO mistake_source_revision
            (revision_id,mistake_id,child_id,session_id,version,question,artifact_id,before_json,
             after_hash,offer_json,confirmation_turn_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (pending['review_id'], focus['mistake_id'], focus['child_id'], focus['session_id'],
             previous['version'] + 1 if previous else 1, pending['question'], artifact_id,
             json.dumps(before, ensure_ascii=False), digest(snapshot(c, focus['mistake_id'], focus['child_id'])),
             json.dumps(offer_value, ensure_ascii=False), authorized_turn_id, now))
        revision = current(c, focus['mistake_id'], focus['child_id'])
        confirmation = OcrTutorConfirmationService(_TransactionDatabase(c)).confirm(focus['session_id'],
            artifact_id, pending['question'], 'manual_correction',
            'mistake-repair-confirm:' + authorized_turn_id,
            expected_formula_binding={k: revision[k] for k in ('review_id', 'version')})
        focus['state'].pop('question_confirmation', None)
        for key in ('diagnosis_probe_id', 'next_activity', 'teaching_turn_id'):
            focus['state'].pop(key, None)
        focus['state'].update(question_text=pending['question'], submitted_answer=answer, question_review_status='confirmed',
                              ocr_confirmation_id=confirmation['confirmation_id'], teaching_requested=True)
        focus['phase'] = 'remediation'
        # The revision and replay receipt must survive (or roll back) together.
        MistakeDialogueService(_TransactionDatabase(c))._save(focus,
            '好，我们按刚才核对的这道题继续讲解。', action_id, text)
        c.commit()
    return True
