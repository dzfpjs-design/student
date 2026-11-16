"""Connect saved diagnostic receipts to the original-question conversation."""
import json

from . import document_goal_handoff as goal
from .diagnostic_prompt import DiagnosticPromptService, _version
from .document_independent_check import _qualified, original_version
from .learning_state import LearningStateService


def record(c, row):
    ref=json.loads(row['request_json']).get('document_independent')
    if not ref:
        return
    from .document_revisit import retained
    packet=retained(c,ref['binding']['commitment_id'],row['child_id'])
    item,payload,*_=packet
    work=payload.get('practice',{})
    # A late receipt never reopens another question or a cancelled practice.
    if (item['status'] not in {'pending','due'} or work.get('phase')!='checking_new_question'
            or work.get('independent_prompt_id')!=row['prompt_id']):
        return
    goal.require(work.get('independent_prompt_id')==row['prompt_id']
        and work.get('session_id')==row['origin_session_id']
        and work.get('independent_request')==ref, 'DOCUMENT_CHECK_RESULT_BINDING_CHANGED')
    receipt=DiagnosticPromptService._receipt(c,row)
    goal.require(row['status']=='answered' and receipt and row['authorized_turn_id'],
        'DOCUMENT_CHECK_RESULT_UNAVAILABLE')
    saved={key:row[key] for key in ('prompt_id','item_version','attempt_id')}
    saved.update(evidence_id=receipt['evidence_id'],answer_turn_id=row['authorized_turn_id'],request_fingerprint=goal.digest(ref))
    results=work.setdefault('independent_results',[])
    goal.require(not any(r['prompt_id']==row['prompt_id'] for r in results), 'DOCUMENT_CHECK_RESULT_CONFLICT')
    results.append(saved)
    work.update(phase='check_feedback',last_turn_id=row['authorized_turn_id'])
    c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=?',
        (json.dumps(payload,ensure_ascii=False),item['commitment_id']))


def current(c, packet):
    item,payload,root,state,confirmed,teacher=packet
    work=payload.get('practice',{})
    saved=work.get('independent_results',[])[-1] if work.get('independent_results') else None
    goal.require(saved and saved['prompt_id']==work.get('independent_prompt_id'), 'DOCUMENT_CHECK_RESULT_UNAVAILABLE')
    row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=?',
        (saved['prompt_id'],item['child_id'])).fetchone()
    goal.require(row and row['status']=='answered' and row['origin_session_id']==work['session_id']
        and all(saved[k]==row[k] for k in ('prompt_id','item_version','attempt_id'))
        and saved['answer_turn_id']==row['authorized_turn_id'], 'DOCUMENT_CHECK_RESULT_CHANGED')
    receipt=DiagnosticPromptService._receipt(c,row)
    goal.require(receipt and receipt['evidence_id']==saved['evidence_id'], 'DOCUMENT_CHECK_RESULT_CHANGED')
    ref=json.loads(row['request_json'])['document_independent']
    goal.require(goal.digest(ref)==saved['request_fingerprint'] and ref['binding']['commitment_id']==item['commitment_id']
        and ref['binding']['original_version']==original_version(state), 'DOCUMENT_CHECK_RESULT_CHANGED')
    source=_qualified(c,item['child_id'],row['diagnostic_item_id'])
    mapping=_qualified(c,item['child_id'],ref['binding']['mapping_item_id'])
    goal.require(source and _version(source)==row['item_version'] and mapping
        and _version(mapping)==ref['binding']['mapping_version'], 'DOCUMENT_CHECK_RESULT_SOURCE_CHANGED')
    from .document_source_review import reviewed_source
    reviewed,proof=reviewed_source(c,packet)
    hypothesis_matches=False
    if ref['binding'].get('mapping_hypothesis'):
        from .document_target_hypothesis import resolved
        selected,hypothesis=resolved(c,packet)
        hypothesis_matches=(selected is not None and hypothesis==ref['binding']['mapping_hypothesis']
            and selected['diagnostic_item_id']==mapping['diagnostic_item_id'])
    goal.require(proof==ref['binding'].get('mapping_review')
        and (reviewed is not None or hypothesis_matches or ' '.join(mapping['prompt'].split())==' '.join(confirmed['confirmed_text'].split())),
        'DOCUMENT_CHECK_RESULT_MAPPING_CHANGED')
    return row,source,LearningStateService._attempt(c,row['attempt_id'])


def sources(c, packet):
    work=packet[1].get('practice',{})
    if work.get('phase')!='check_feedback' and work.get('search_return_phase')!='check_feedback':
        return []
    try:
        # A withdrawn source is still private; redaction does not grant teaching authority.
        work=packet[1]['practice']
        row=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
            (work.get('independent_prompt_id'),packet[0]['child_id'],work.get('session_id'))).fetchone()
        if not row:
            return []
        texts=[json.loads(row['item_json']).get('prompt','')]
        source=c.execute('SELECT prompt,explanation FROM diagnostic_item WHERE diagnostic_item_id=?',(row['diagnostic_item_id'],)).fetchone()
        if source:
            texts.extend(source)
        receipt=DiagnosticPromptService._receipt(c,row)
        if receipt:
            texts.extend([receipt['submitted_answer'],receipt['feedback']])
        from .document_open_feedback import private_feedback_texts
        texts.extend(private_feedback_texts(c,row))
        # Combined reviews are private even after a later revision invalidates
        # their teaching authority. They are never implicit classifier context.
        for review in c.execute("""SELECT answer FROM tutor_turn WHERE child_id=? AND session_id=?
            AND client_action_id LIKE 'document_spoken_review_v1:%'""",(packet[0]['child_id'],work.get('session_id'))):
            texts.append(review[0])
        # Readback of a multi-utterance explanation does not authorize sending
        # earlier answers or withdrawn assessments with a later intent request.
        for saved in work.get('independent_results', []):
            prior=c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND child_id=? AND origin_session_id=?',
                (saved.get('prompt_id'),packet[0]['child_id'],work.get('session_id'))).fetchone()
            if prior and prior['prompt_id']!=row['prompt_id']:
                original=c.execute('SELECT submitted_answer,feedback FROM diagnostic_attempt WHERE attempt_id=? AND child_id=?',
                    (prior['attempt_id'],prior['child_id'])).fetchone()
                if original:
                    texts.extend(original)
                texts.extend(private_feedback_texts(c,prior))
        return texts
    except (ValueError,TypeError,KeyError):
        return []


def feedback(c, packet, *, help_requested=False):
    try:
        _,source,attempt=current(c,packet)
        from .document_open_feedback import spoken_prompt, feedback as open_feedback
        if spoken_prompt(c,packet) is not None:
            from .document_spoken_review import latest, speech, latest_clarification
            whole=latest(c,packet)
            if whole:
                return speech(whole['review'])
            clarification=latest_clarification(c,packet)
            if clarification:
                return clarification['response_text']
            return open_feedback(c,packet,help_requested=help_requested)
    except (ValueError,TypeError,KeyError):
        return '刚才的作答记录保留着，但题目来源或评分依据发生变化，暂不据此判断对错或继续讲解。'
    if attempt['score_status']!='verified_local':
        return '刚才的作答已经保存，目前还不能可靠确认对错，不能据此判断掌握。'
    verdict='这道新题回答正确。' if attempt['correctness']==1 else '这道新题还没有完全答对。'
    if help_requested:
        return verdict+'刚才的新题是：'+source['prompt']+'。参考思路：'+source['explanation']+'。你可以说说哪一步还不清楚。'
    return verdict+'一次作答不代表长期掌握。可以继续核对思路、再做一道新题，或先结束这次回顾；后续复习仍按记录安排。'
