"""Explicit spoken corrections produce local derived text, never edit recordings."""
import re

from . import document_goal_handoff as goal


def parse(value, transcript):
    goal.require(isinstance(value, dict) and set(value) == {'target','target_quote','replacement_quote'},
        'DOCUMENT_SPOKEN_REVISION_SCHEMA')
    target, quote, replacement = (value[k] for k in ('target','target_quote','replacement_quote'))
    goal.require(target is None or target == 'last' or type(target) is int and 1 <= target <= 1000,
        'DOCUMENT_SPOKEN_REVISION_TARGET')
    goal.require(isinstance(quote,str) and (not quote or quote in transcript)
        and (target is None or quote.strip()), 'DOCUMENT_SPOKEN_REVISION_QUOTE')
    goal.require(isinstance(replacement,str) and replacement.strip() and replacement in transcript
        and len(replacement) <= 2000, 'DOCUMENT_SPOKEN_REVISION_REPLACEMENT')
    return dict(value)


def resolve(chain, request):
    """Ordinal validation checks the model's literal reference, not intent keywords."""
    from .learning_input_revision import duration_minutes
    target, quote = request['target'], request['target_quote']
    quote = re.sub(r'^(?:我(?:刚才)?的|我)', '', quote)
    slot = None
    effective = chain['effective_segments']
    if target == 'last' and re.fullmatch(r'(?:上一段|前一段|最后一段)(?:回答|补充|解释)?', quote):
        slot = len(effective)-1
    elif type(target) is int:
        literal = re.fullmatch(r'第([一二两三四五六七八九十百零\d]+)(?:段|次)(?:回答|补充|解释)?', quote)
        if literal and duration_minutes(literal[1]) == target:
            slot = target-1
    base = dict(request=request, base_fingerprint=chain['fingerprint'])
    if slot is None or not 0 <= slot < len(effective):
        return dict(base, status='needs_clarification')
    selected = effective[slot]
    value = dict(base, status='resolved', slot=slot, target_prompt_id=selected['source_prompt_id'],
        target_slot_id=selected['slot_id'], target_text_hash=goal.digest(selected['text']))
    matching=[p['prompt_id'] for p in chain.get('pending_revisions',[])
        if p['replacement_quote'].strip().rstrip('。.!！？?')==request['replacement_quote'].strip().rstrip('。.!！？?')]
    if len(matching)==1:
        value['resolves_pending_prompt_id']=matching[0]
    return value


def prepare(c, packet, request, *, before_turn_id=None):
    from .document_spoken_conversation import read
    return resolve(read(c,packet,before_turn_id=before_turn_id), request) if request is not None else None


def append(effective, pending, segment, revision):
    """Keep all original segments elsewhere; this is only the current text view."""
    if revision is None:
        effective.append(dict(slot_id=segment['prompt_id'], source_prompt_id=segment['prompt_id'],
            text=segment['transcript'], original_text=True))
        return
    if revision['status'] == 'needs_clarification':
        pending.append(dict(prompt_id=segment['prompt_id'], transcript=segment['transcript'],
            replacement_quote=revision['request']['replacement_quote']))
        return
    slot = revision['slot']
    goal.require(0 <= slot < len(effective) and effective[slot]['slot_id'] == revision['target_slot_id']
        and effective[slot]['source_prompt_id'] == revision['target_prompt_id']
        and goal.digest(effective[slot]['text']) == revision['target_text_hash'], 'DOCUMENT_SPOKEN_REVISION_SOURCE_CHANGED')
    effective[slot] = dict(slot_id=revision['target_slot_id'], source_prompt_id=segment['prompt_id'],
        text=revision['request']['replacement_quote'], original_text=False)
    resolved=revision.get('resolves_pending_prompt_id')
    if resolved:
        goal.require(sum(p['prompt_id']==resolved for p in pending)==1, 'DOCUMENT_SPOKEN_REVISION_PENDING_CHANGED')
        pending[:]=[p for p in pending if p['prompt_id']!=resolved]
