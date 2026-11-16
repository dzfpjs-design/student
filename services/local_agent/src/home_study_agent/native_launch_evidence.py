"""Read trusted, exact no-dispatch facts without inferring them from silence."""
import hashlib
import json
from uuid import UUID


def unstarted_launches(c, child_id, since):
    sources = {'controlled_content': set(), 'controlled_game': set()}
    proofs = {}
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='controlled_launch_receipt'").fetchone():
        return sources, hashlib.sha256(b'[]').hexdigest()
    for event in c.execute('''SELECT * FROM audit_event WHERE child_id=? AND actor='native'
            AND action IN ('controlled_content_ended','controlled_game_ended')
            ORDER BY occurred_at,audit_id''', (child_id,)):
        kind = event['action'].removesuffix('_ended')
        key = 'content_session_id' if kind == 'controlled_content' else 'game_session_id'
        receipt_kind = 'video' if kind == 'controlled_content' else 'game'
        try:
            value = json.loads(event['summary_json'])
            if (not isinstance(value, dict) or value.get('reason') != 'native_not_dispatched'
                    or value.get('proof') not in {'cancelled_before_dispatch', 'owner_exit_before_registration'}
                    or type(value.get('actual_seconds')) is not int or value['actual_seconds'] != 0
                    or type(value.get('native_revision')) is not int or value['native_revision'] < 2
                    or not all(isinstance(value.get(k), str) and 0 < len(value[k]) <= 128
                               for k in ('native_intent_id', key))
                    or not isinstance(value.get('native_nonce'), str)
                    or str(UUID(value['native_nonce'])) != value['native_nonce']):
                continue
            source = c.execute(f'''SELECT s.* FROM {kind}_session s
                JOIN daily_session d ON d.session_id=s.session_id AND d.child_id=s.child_id
                JOIN controlled_launch_receipt r ON r.{key}=s.{key}
                    AND r.child_id=s.child_id AND r.session_id=s.session_id
                    AND r.request_id=s.client_action_id AND r.kind=? AND r.intent_id=?
                WHERE s.{key}=? AND s.child_id=? AND julianday(s.started_at)>=julianday(?)''',
                (receipt_kind, value.get('native_intent_id'), value.get(key), child_id, since)).fetchone()
            if (not source or source['status'] != 'stopped' or source['actual_seconds'] != 0
                    or source['end_reason'] != 'native_not_dispatched'
                    or source['ended_at'] != event['occurred_at']
                    or (receipt_kind == 'game' and source['os_process_id'] is not None)):
                continue
        except (ValueError, TypeError, KeyError):
            # Malformed evidence is not authority to remove recorded usage.
            continue
        sources[kind].add(source[key])
        proofs[(kind, source[key])] = {'audit_id': event['audit_id'], 'proof': value,
                                      'occurred_at': event['occurred_at']}
    digest = hashlib.sha256(json.dumps([proofs[k] for k in sorted(proofs)],
                                      sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    return sources, digest
