"""Grounded unconfirmed fields, with a separate ownership and final readback."""
import json
import re

from .learning_input_revision import _compact, _json, _version, duration_minutes


def title_literal_text(text):
    match = re.fullmatch(r'((?:老师说)?[^“”「」『』"\n]{1,60}(?:名字|标题|名称)改成)[“「『"]([^“”「」『』"\n]{1,60})[”」』"][。！!\s]*', text)
    return match[1] + match[2] if match else None


def needs_ownership(text):
    return bool(re.search(r'[“”「」『』"]|(?:他说|她说|老师说)', text))


def estimate(text):
    value = _compact(text)
    match = re.fullmatch(r'(?:大概|大约|预计|估计|差不多)?([0-9一二两三四五六七八九十百零]+(?:分钟|分))(?:左右)?', value)
    return duration_minutes(match[1]) if match else None


def witness(arrival, quote, value, source):
    return {'arrival_id': arrival['arrival_id'], 'quote': quote, 'value': value, 'source': source}


def begin(proposal, arrival, arguments):
    proposal['intake'] = {'version': 'literal_input_intake_v1', 'root_arrival_id': arrival['arrival_id'],
        'fields': {name: witness(arrival, arguments['change_quotes'][name], value,
                    'child_estimate' if name == 'expected_minutes' else 'child_report')
                   for name, value in arguments['changes'].items()},
        'ownership': None if needs_ownership(arrival['utterance']) else witness(
            arrival, arrival['utterance'], True, 'child_request')}


def missing(proposal):
    if not proposal['intake']['ownership']:
        return 'ownership'
    if proposal['operation'] == 'add' and 'expected_minutes' not in proposal['changes']:
        return 'expected_minutes'
    return None


def reply_requested(connection, binding, transcript):
    from .learning_input_revision import LearningInputRevisionService
    row = connection.execute('SELECT proposal_json FROM learning_input_proposal WHERE request_id=?', (binding['request_id'],)).fetchone()
    proposal = json.loads(row[0]) if row else {}
    return bool(proposal.get('intake') and not proposal.get('target')
                and LearningInputRevisionService._matches(proposal.get('candidates', []), transcript))


def valid(service, connection, child_id, session_id, proposal):
    try:
        intake = proposal['intake']
        if intake['version'] != 'literal_input_intake_v1' or set(intake['fields']) != set(proposal['changes']):
            return False

        def journal_matches(arrival, entry, field=None):
            command = connection.execute('''SELECT t.transcript,t.tool_status,s.capability_name,r.result_json,
                p.status AS plan_status,s.status AS step_status
                FROM agent_turn t JOIN learning_input_command r ON r.turn_id=t.agent_turn_id
                JOIN agent_plan p ON p.agent_turn_id=t.agent_turn_id
                JOIN agent_step s ON s.plan_id=p.plan_id AND s.step_index=0
                WHERE t.client_action_id=? AND t.child_id=? AND t.session_id=? AND r.child_id=t.child_id
                    AND p.child_id=t.child_id AND p.session_id=t.session_id''',
                (arrival['client_action_id'], child_id, session_id)).fetchone()
            if (not command or command['transcript'] != arrival['utterance'] or command['tool_status'] != 'completed'
                    or command['plan_status'] == 'cancelled' or command['step_status'] == 'cancelled'
                    or command['capability_name'] not in {'learning_inputs.propose_revision', 'learning_inputs.select_target'}):
                return False
            saved = json.loads(command['result_json']).get('input_provenance', {})
            return (saved.get('root_arrival_id') == intake['root_arrival_id']
                    and (saved.get('fields', {}).get(field) if field else saved.get('ownership')) == entry)

        for field, entry in intake['fields'].items():
            row = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=?',
                                     (entry['arrival_id'], child_id, session_id)).fetchone()
            if not row or not entry['quote'] or entry['quote'] not in row['utterance'] or entry['value'] != proposal['changes'][field]:
                return False
            if not journal_matches(row, entry, field):
                return False
            normalized = estimate(entry['quote']) if field == 'expected_minutes' else entry['quote']
            if normalized != entry['value'] or entry['source'] != ('child_estimate' if field == 'expected_minutes' else 'child_report'):
                return False
            if field == 'expected_minutes' and entry['arrival_id'] != intake['root_arrival_id']:
                from .learning_input_field_semantics import recorded
                semantic = recorded(connection, row, field, entry.get('semantic_request_key'))
                if (not semantic or semantic['meaning'] != 'estimate' or semantic['literal_quote'] != entry['quote']
                        or semantic['minutes'] != entry['value']):
                    return False
        ownership = intake['ownership']
        if ownership:
            row = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=? AND child_id=? AND session_id=?',
                                     (ownership['arrival_id'], child_id, session_id)).fetchone()
            if not row or ownership['quote'] != row['utterance'] or ownership['value'] is not True or not journal_matches(row, ownership):
                return False
            if ownership['source'] == 'child_ownership':
                from .learning_input_field_semantics import recorded
                semantic = recorded(connection, row, 'ownership', ownership.get('semantic_request_key'))
                if not semantic or semantic['meaning'] != 'own':
                    return False
            elif ownership['source'] != 'child_request' or needs_ownership(row['utterance']):
                return False
        if proposal['operation'] == 'add':
            from .learning_input_omission import current
            return current(connection, child_id, session_id, proposal)
        target = proposal['target']
        if not target:
            candidates = proposal.get('candidates', [])
            return bool(candidates and all((service._item(connection, child_id, session_id, proposal['kind'], item['target_id']) or {}).get('version') == item['version'] for item in candidates))
        current = service._item(connection, child_id, session_id, proposal['kind'], target['target_id'])
        return bool(current and current['version'] == target['version'])
    except (KeyError, TypeError, ValueError):
        return False


def offer(connection, original, proposal, turn_id):
    field = missing(proposal)
    if field == 'ownership':
        response = '这是你自己的作业，还是在转述别人的话？请确认是不是你的作业；现在还没有改记录。'
    elif field == 'expected_minutes':
        response = '这项作业预计还要几分钟？不确定时可以说大概用时，我不会替你猜。'
    else:
        changes = proposal['changes']
        details = '；'.join(str(changes[name]) for name in ('subject', 'title', 'range_text', 'special_requirements') if changes.get(name))
        response = f"补录{details}，预计{changes['expected_minutes']}分钟。这样对吗？确认后才保存并核对剩余预算，原任务和复习义务保留。"
    state = 'clarification_required' if field else 'awaiting_confirmation'
    version = _version([proposal, turn_id, response])
    connection.execute('''INSERT INTO learning_input_proposal VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(request_id) DO UPDATE SET state=excluded.state,proposal_json=excluded.proposal_json,
        readback_version=excluded.readback_version,offered_by_turn_id=excluded.offered_by_turn_id,response_text=excluded.response_text''',
        (original['arrival_id'], original['child_id'], original['session_id'], state, _json(proposal), version, turn_id, response))
    return {'status': state, 'request_id': original['arrival_id'], 'readback_version': version,
            'missing_field': field, 'target_id': proposal['target']['target_id'], 'response_text': response,
            'requires_child_reply': True, 'input_provenance': proposal['intake']}


def respond(service, connection, arrival, pending, proposal, turn_id):
    if not valid(service, connection, arrival['child_id'], arrival['session_id'], proposal):
        connection.execute("UPDATE learning_input_proposal SET state='conflict' WHERE request_id=?", (pending['request_id'],))
        return {'status': 'conflict', 'response_text': '作业或计划已经变化，刚才的草稿不能继续确认。请重新核对这项作业。'}
    field, text = missing(proposal), arrival['utterance']
    from .learning_input_field_semantics import for_turn
    semantic, request_key = for_turn(connection, turn_id, arrival, field)
    meaning = (semantic or {}).get('meaning', 'unclear')
    if field == 'ownership':
        if meaning == 'not_mine':
            connection.execute("UPDATE learning_input_proposal SET state='cancelled' WHERE request_id=?", (pending['request_id'],))
            return {'status': 'cancelled', 'response_text': '知道了，不作为你的作业保存，原记录不变。'}
        if meaning == 'own':
            proposal['intake']['ownership'] = {**witness(arrival, text, True, 'child_ownership'), 'semantic_request_key': request_key}
    elif field == 'expected_minutes':
        if meaning == 'estimate':
            minutes = semantic['minutes']
            proposal['changes'][field] = minutes
            proposal['intake']['fields'][field] = {**witness(arrival, semantic['literal_quote'], minutes, 'child_estimate'), 'semantic_request_key': request_key}
    original = connection.execute('SELECT * FROM learning_input_arrival WHERE arrival_id=?', (pending['request_id'],)).fetchone()
    if proposal['operation'] == 'add' or missing(proposal):
        return offer(connection, original, proposal, turn_id)
    return {**service._offer(connection, original, proposal, turn_id), 'input_provenance': proposal['intake']}
