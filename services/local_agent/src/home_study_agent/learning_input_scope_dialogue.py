"""Local remaining-work slots and auditable multi-turn confirmation.

No cloud inference, guessed work identity, or acoustic delivery assertion.
"""
import json
import re

from .learning_input_revision import duration_minutes


def scope_confirmation(text):
    compact = re.sub(r'[\s，,。.!！?？]', '', text)
    return bool(re.fullmatch(r'(?:嗯|对|对的|是|是的|没错|确认|好的|好)(?:就是这些|就是这样|就这样|按这个|按你说的|没错|确认)?', compact))


def scope_slots(text):
    """Extract literal slots in either order; conflicting clauses ask, never choose."""
    if re.search(r'他说|她说|老师说|假如|如果|比如|不要.*(?:记录|确认)|别.*(?:记录|确认)|提示|讲解|播放|怎么|为什么|答案', text):
        return {}
    estimates = list(re.finditer(r'([零〇一二两三四五六七八九十百千0-9]+)(?:个)?分钟', text))
    if len(estimates) > 1 or re.search(r'不是|不对|说错|改一下', text):
        return {'clarify': True}
    values = {}
    if estimates:
        match = estimates[0]
        minutes = duration_minutes(match[1])
        if minutes and 1 <= minutes <= 1440:
            values['estimate'] = {'minutes': minutes, 'quote': match[0]}
    work_text = text[:estimates[0].start()] if estimates else text
    work_text = re.sub(r'(?:[,，\s]*)(?:估计|预计|大概|大约|差不多|可能)?(?:还要|还得|需要|得|要)?\s*$', '', work_text)
    clauses = [c.strip(' ，,。.!！?？') for c in re.split(r'[，,。；;]', work_text)]
    scopes = [c for c in clauses if re.search(r'题|页|段|行|单词|作文|练习|口算|背诵|朗读', c)
              and (re.search(r'还剩|剩下|剩余|还有|还差|没写|没做|没完成|未写|未做|还没|最后|后面|第[零一二三四五六七八九十0-9]', c)
                   or re.fullmatch(r'[零一二两三四五六七八九十0-9]+(?:道|个|页|段|行)?[\u4e00-\u9fff]{0,8}(?:题|单词|口算)', c))
              and not re.search(r'做完了|写完了|已完成|全部完成|不用做|不想做|不做了|不要|取消|明天|后天|喜欢|讨厌', c)]
    # With an estimate first, the later clause can carry the unfinished work.
    if estimates:
        tail = text[estimates[0].end():].strip(' ，,。.!！?？吧左右')
        scopes += [c.strip() for c in re.split(r'[，,。；;]', tail) if re.search(r'题|页|段|行|单词|作文|练习|口算', c)
                   and re.search(r'还剩|剩下|没写|没做|未做|未写|还没', c)]
    if len(scopes) == 1:
        values['scope'] = {'quote': scopes[0]}
    elif len(scopes) > 1:
        return {'clarify': True}
    return values


def advance_scope_dialogue(snapshot, selected, transcript):
    if selected.get('reason') == 'original_execution_unsettled':
        if not scope_slots(transcript) and not scope_confirmation(transcript):
            return None
        response = '旧范围的任务还在暂停或尚未收尾。可以先明确把旧任务延期，保留已做进度，再核对新范围；现在不会补排或记作完成。'
        return {'dialogue': {'candidates': snapshot['candidates'], 'focus': selected, 'response': response}, 'response': response}
    dialogue = snapshot.get('dialogue') or {}
    draft = dialogue.get('scope_draft') or {'binding': selected['binding']}
    if draft.get('binding') != selected['binding']:
        draft = {'binding': selected['binding']}
    else:
        draft = dict(draft)
    if re.search(r'(?:先)?(?:不要|别|不)(?:再)?确认|取消(?:这次)?确认', transcript):
        response = '这次范围确认已放下，尚未写入或安排。原来的实际学习记录保留。'
        return {'dialogue': {'candidates': snapshot['candidates'], 'focus': selected,
                            'scope_draft': {'binding': selected['binding']}, 'scope_offer_ready': False, 'response': response}, 'response': response}
    if scope_confirmation(transcript):
        if draft.get('scope') and draft.get('estimate') and dialogue.get('offer_turn_id'):
            return {'capability': 'learning_inputs.review_reclaimed_scope', 'binding': selected['binding'],
                'scope_offer': {'draft': draft, 'offer_turn_id': dialogue['offer_turn_id']},
                'arguments': {'operation': 'confirm_scope', 'request_quote': transcript,
                    'remaining_scope_quote': draft['scope']['quote'], 'remaining_minutes': draft['estimate']['minutes']},
                'response': '我先保存你确认的剩余内容和新估时，尚未安排学习时间。'}
        slots = {}
    else:
        slots = scope_slots(transcript)
        if not slots:
            if re.search(r'聊|天气|周末|休息|吃饭|喝水|暂停|停止|取消|撤回|播放|听题|听力|提示|讲解|怎么|为什么|答案|换个话题|找家长|妈妈|爸爸|不要了|不做了|明天|后天', transcript):
                return None
            response = ('我还不能确定你说的具体剩余内容。请直接描述没做的部分，也可以先说估计还需多久；不会替你猜。'
                        if not draft.get('scope') else '剩余内容已保留，但这句话还不足以确定新估时。大概还需几分钟？不确定也可以直接说，我不会自动加时。')
            return {'dialogue': {'candidates': snapshot['candidates'], 'focus': selected, 'scope_draft': draft, 'response': response}, 'response': response}
    offer_ready = False
    if slots.get('clarify'):
        draft = {'binding': selected['binding']}
        response = '刚才的信息有修正或不止一个估时。请告诉我现在还没做的内容和新的估时，可以分开说。'
    else:
        for field in ('scope', 'estimate'):
            if field in slots:
                draft[field] = {**slots[field], 'arrival_id': snapshot['arrival_id']}
        if draft.get('scope') and draft.get('estimate'):
            offer_ready = True
            response = f"当前这项{selected['title']}，我记下的剩余内容是：{draft['scope']['quote']}，新估计还需{draft['estimate']['minutes']}分钟。对应作业、内容和时间都对吗？确认后仍要再决定何时安排，已用时间不会返还。"
        elif draft.get('scope'):
            response = f"剩余内容是：{draft['scope']['quote']}。你估计还需要多长时间？"
        elif draft.get('estimate'):
            response = f"新估时记为{draft['estimate']['minutes']}分钟。具体还剩哪些题或内容没做？"
        else:
            response = '还需要具体剩余内容和新估时才能确认，可以先说其中一项。'
    return {'dialogue': {'candidates': snapshot['candidates'], 'focus': selected, 'scope_draft': draft, 'scope_offer_ready': offer_ready, 'response': response},
            'response': response}


def validate_scope_offer(c, turn_id, binding, request):
    """Verify a generated readback and each literal child slot, not concatenated speech."""
    turn = c.execute('SELECT rowid AS turn_order,* FROM agent_turn WHERE agent_turn_id=?', (turn_id,)).fetchone()
    plan = c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?', (turn_id,)).fetchone()
    if not turn or not plan or not scope_confirmation(turn['transcript']):
        return None
    manifest = json.loads(plan[0])
    offer = manifest.get('learning_input_scope_offer') or {}
    draft = offer.get('draft') or {}
    if (draft.get('binding') != binding or (draft.get('scope') or {}).get('quote') != request['remaining_scope_quote']
            or (draft.get('estimate') or {}).get('minutes') != request['remaining_minutes']):
        return None
    prior = c.execute('''SELECT t.*,p.context_manifest_json,p.status AS plan_status FROM agent_turn t
        JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id WHERE t.session_id=? AND t.child_id=? AND t.rowid<?
        ORDER BY t.rowid DESC LIMIT 1''', (turn['session_id'], turn['child_id'], turn['turn_order'])).fetchone()
    if not prior or prior['agent_turn_id'] != offer.get('offer_turn_id') or prior['intent'] != 'unplaced_voice_dialogue' or prior['plan_status'] == 'cancelled':
        return None
    saved = json.loads(prior['context_manifest_json']).get('learning_input_unplaced_dialogue') or {}
    if not saved.get('scope_offer_ready') or saved.get('scope_draft') != draft or saved.get('response') != prior['response_text']:
        return None
    for field in ('scope', 'estimate'):
        slot = draft.get(field) or {}
        arrival = c.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=?',
                            (slot.get('arrival_id'), turn['child_id'], turn['session_id'])).fetchone()
        if not arrival or slot.get('quote') not in arrival['utterance']:
            return None
        extracted = scope_slots(arrival['utterance']).get(field)
        if extracted != {k: v for k, v in slot.items() if k != 'arrival_id'}:
            return None
        snapshot = next((v['record'] for v in json.loads(arrival['snapshot_json']) if v.get('kind') == 'unplaced_voice_arrival'), {})
        if ((snapshot.get('dialogue') or {}).get('focus') or {}).get('binding') != binding:
            return None
        source = c.execute('SELECT t.rowid AS turn_order,t.*,p.context_manifest_json,p.status AS plan_status FROM agent_turn t JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id WHERE t.client_action_id=? AND t.child_id=? AND t.session_id=?',
            (arrival['client_action_id'], turn['child_id'], turn['session_id'])).fetchone()
        if (not source or source['turn_order'] >= turn['turn_order'] or source['intent'] != 'unplaced_voice_dialogue'
                or source['plan_status'] == 'cancelled' or source['transcript'] != arrival['utterance']):
            return None
        source_draft = (json.loads(source['context_manifest_json']).get('learning_input_unplaced_dialogue') or {}).get('scope_draft') or {}
        if source_draft.get('binding') != binding or source_draft.get(field) != slot:
            return None
    return offer
