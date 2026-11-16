"""Await actual formative work inside the existing signed native execution lane.

The turn stays pending until the original UI has rendered/delivered its result
and submits the existing dedicated completion receipt.
"""
from __future__ import annotations

import asyncio
from time import monotonic
from .mistake_learning import _is_due


class MistakeLearningExecutionError(ValueError):
    pass


def learning_response(operation, value, practice_item_id=None):
    if operation == 'review_today':
        due = [v for v in value.get('items', []) if v.get('review_due') and _is_due(v)]
        if not due:
            return '今天暂时没有到期的错题。你也可以选一道想巩固的题，或告诉我一道新题。'
        titles = '、'.join(v.get('source', {}).get('title', '一道错题')[:36] for v in due[:3])
        return f'今天有{len(due)}道错题到期，先看{titles}。你想先复习哪一道？'
    if operation == 'select':
        return '这道题已经打开。你可以让我分析错在哪里、练几道类似题，或一步一步讲解。'
    if operation in ('analyze', 'capture'):
        analysis = value.get('analysis') or {}
        parts = [analysis.get('summary', '')]
        causes = analysis.get('wrong_causes') or []
        if causes:
            parts.append(causes[0].get('explanation', ''))
        labels = [p.get('label', '') for p in analysis.get('knowledge_points', [])]
        if any(labels):
            parts.append('这题主要涉及' + '、'.join(x for x in labels if x) + '。')
        checks = analysis.get('prerequisite_checks') or []
        if checks:
            parts.append('可以先核对' + checks[0]['title'] + '。' + checks[0]['question'])
        return ' '.join(p for p in parts if p)[:6000]
    if operation == 'coach':
        turns = [t for t in value.get('turns', []) if t.get('role') == 'assistant' and t.get('kind') == 'coach'
                 and t.get('practice_item_id') == practice_item_id]
        text = turns[-1]['text'] if turns else '你先说说这道题的第一步会怎么想？'
        variant = value.get('coach_variant')
        if value.get('coach_phase') == 'independent_practice' and variant:
            text += '\n' + variant['question']
        return text[:6000]
    if operation == 'answer':
        items = value.get('practice_items', [])
        variant = value.get('coach_variant')
        if variant and variant['practice_item_id'] == practice_item_id:
            feedback = variant.get('feedback') or {}
            return ' '.join(filter(None, (feedback.get('feedback'), feedback.get('next_hint')
                if feedback.get('judgement') in ('incorrect', 'partly_correct', 'uncertain') else None)))[:6000]
        answered = [x for x in items if x.get('feedback')]
        current = next((x for x in answered if x['practice_item_id'] == practice_item_id), None) if practice_item_id else (answered[-1] if answered else None)
        feedback = current.get('feedback', {}) if current else {}
        text = feedback.get('feedback', '')
        if value.get('today', {}).get('status') == 'needs_explanation':
            return (text + ' 我们先不加题。你觉得卡住的是哪个条件或哪一步？')[:6000]
        if feedback.get('judgement') in ('incorrect', 'partly_correct', 'uncertain'):
            return (text + ' ' + feedback.get('next_hint', '你愿意说说刚才那一步是怎么想的吗？'))[:6000]
        ready = next((x for x in items if x.get('status') == 'ready'), None)
        if ready:
            return (text + ' 再用一道题试试：' + ready['question'])[:6000]
        if value.get('today', {}).get('status') == 'done':
            return (text + ' 今天这一小组练习已经记录。下次复习时我会再提醒你。')[:6000]
        remaining = value.get('today', {}).get('remaining_count', 0)
        return (text + f' 这题已经记录，今天还剩{remaining}道巩固题。想再试一道吗？')[:6000]
    ready = next((x for x in value.get('practice_items', []) if x.get('status') == 'ready'), None)
    if ready:
        return f"这组有{value.get('today', {}).get('target_count', len(value.get('practice_items', [])))}道题，我们一道一道来。{ready['question']}"[:6000]
    return '今天这一组已经练过，进度已保留。你可以继续问刚才的方法，或复习下一道到期的题。'


async def execute_native_learning(database, service, intake, turn_id):
    from .realtime_native_mistake_learning import validate, record_actual_effect
    from .mistake_learning import graph_version_from_reader

    reader = service.graph_matcher.cache_version if service.graph_matcher else None

    with database.connect() as c:
        c.execute('BEGIN')
        source = validate(c, turn_id, require_claim=True, current_graph_version=graph_version_from_reader(reader,c))
    def guard(c):
        validate(c, turn_id, require_claim=True,current_graph_version=graph_version_from_reader(reader,c))
        return True
    operation = source['operation']
    child = source['child_id']
    sid = source['session_id']
    action = source['mistake_learning_action_id']
    snapshot = source.get('mistake_learning_source')
    mid = snapshot.get('mistake_id') if snapshot else None
    if operation == 'review_today':
        value = await asyncio.to_thread(service.library, child)
    else:
        if operation == 'capture':
            card = intake.create(child_id=child, session_id=sid, question=source['utterance'],
                                 submitted_answer='', source_type='spoken', client_action_id=turn_id+':mistake:intake', guard=guard)
            mid = card['mistake_id']
        if not mid:
            raise MistakeLearningExecutionError('MISTAKE_LEARNING_TARGET_REQUIRED')
        kwargs = dict(child_id=child, session_id=sid, client_action_id=action, guard=guard)
        if operation == 'select':
            value = service.focus(mid, **kwargs)
        else:
            if operation in ('capture', 'analyze', 'practice'):
                service.focus(mid, child_id=child, session_id=sid,
                              client_action_id=turn_id+':mistake:focus', guard=guard)
            method = getattr(service, 'analyze' if operation == 'capture' else operation)
            kwargs['utterance'] = source['utterance']
            if operation in ('coach', 'answer'):
                kwargs.update(utterance=source['utterance'], style=source.get('style', 'socratic'),
                              practice_item_id=snapshot.get('practice_item_id') if snapshot else None)
            if operation == 'coach' and snapshot and snapshot['practice_item_id'] is None:
                kwargs['original_review'] = True
            if operation == 'practice':
                kwargs['count'] = source.get('count', 3)
            value = await method(mid, **kwargs)
            # A UI analysis and a spoken request can meet the same saved job.
            # Read its outcome; a completed analyze/practice can then be reused
            # under this native action without issuing another model request.
            until = monotonic()+25
            while value.get('action_status') == 'processing' and monotonic() < until:
                await asyncio.sleep(.25)
                with database.connect() as c:
                    guard(c)
                current = service.state(mid, child_id=child, session_id=sid)
                if current.get('status') == 'ready' and operation in ('analyze', 'capture', 'practice'):
                    value = await method(mid, **kwargs)
                    break
                if current.get('status') in ('failed', 'unknown'):
                    break
            if value.get('action_status') != 'completed':
                raise MistakeLearningExecutionError('MISTAKE_LEARNING_RESULT_UNCONFIRMED')
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        guard(c)
        receipt = record_actual_effect(c, turn_id, value)
        c.commit()
    return {'capability': 'mistake.learn', 'response_text': learning_response(operation, value, snapshot.get('practice_item_id') if snapshot else None),
            'result': {'operation': operation, 'library' if operation == 'review_today' else 'state': value,
                       'mistake_learning_receipt': receipt}, 'evidence': []}
