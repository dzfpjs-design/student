"""Narrow, revocable projection of a previously delivered ordinary Tutor turn.

This is an internal building block, not an intent decision or public endpoint.
Source ownership and text consent are rechecked on every use.
"""
from __future__ import annotations

import json

from .ordinary_learning_text_projection import (
    TUTOR_KEY, TextProjectionBlocked, _decode, _hash, _known_family_names,
    _material, _plain, _privacy, _tutor_version,
)
from .teaching_presentation_scope import replaced_by_question


def snapshot(connection, *, session_id: str, child_id: str, turn_id: str, replay_turn_id: str | None = None) -> dict:
    def require(condition):
        if not condition:
            raise TextProjectionBlocked('tutor_followup_source_unavailable')

    session = connection.execute('SELECT child_id,status FROM daily_session WHERE session_id=?', (session_id,)).fetchone()
    deleting = connection.execute("""SELECT 1 FROM deletion_job WHERE child_id=? AND target_type='child_scope'
        AND scope IN ('learning','knowledge','media','tutor','all_history') AND status!='completed' LIMIT 1""", (child_id,)).fetchone()
    row = connection.execute('SELECT * FROM tutor_turn WHERE turn_id=? AND session_id=? AND child_id=?',
                             (turn_id, session_id, child_id)).fetchone()
    latest = connection.execute('SELECT turn_id FROM tutor_turn WHERE session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
                                (session_id, child_id)).fetchone()
    require(bool(session and session['child_id'] == child_id and session['status'] == 'active' and not deleting and row and latest
                 and latest['turn_id'] == (replay_turn_id or turn_id) and row['status'] == 'completed'
                 and row['safety_status'] == 'passed' and row['hint_level'] == 6
                 and not row['ocr_confirmation_id']))
    require(not replaced_by_question(connection, session_id, child_id, row['created_at']))
    envelope = _decode(row['output_envelope_json'])
    model = connection.execute('SELECT * FROM model_call WHERE call_id=?', (envelope.get('request_id'),)).fetchone()
    require(bool(model and model['status'] == 'completed' and model['child_id'] == child_id and model['session_id'] == session_id))
    manifest = _decode(model['context_manifest_json'])
    proof = manifest.get(TUTOR_KEY) or {}
    expected = {'version', 'policy', 'call_id', 'tutor_turn_id', 'child_id', 'session_id', 'subject', 'privacy',
                'source_sha256', 'request_messages_sha256', 'admitted_privacy_sha256', 'unique_question_sha256', 'model_identity'}
    require(bool(type(proof) is dict and set(proof) == expected and type(proof.get('version')) is int
                 and proof.get('version') == 1 and proof.get('policy') == 'actual_tutor_text_source_v1'
                 and proof.get('call_id') == model['call_id'] and proof.get('tutor_turn_id') == turn_id
                 and proof.get('child_id') == child_id and proof.get('session_id') == session_id
                 and proof.get('subject') == row['subject']
                 and model['purpose'] == 'tutor_hint_level_6'
                 and proof.get('model_identity') == {k: model[k] for k in ('purpose','prompt_version','policy_version','knowledge_version')}
                 and proof.get('source_sha256') == _tutor_version(row)
                 and proof.get('privacy') == _privacy(connection)
                 and proof.get('admitted_privacy_sha256') == _hash(manifest.get('privacy_transform'))
                 and not manifest.get('knowledge_excerpt_count') and not manifest.get('recall_count')
                 and not _material({k: v for k, v in envelope.items() if k != 'local_teaching_control'})))
    names = _known_family_names(connection)
    question, answer = _plain(row['question'], names), _plain(row['answer'], names)
    board = (envelope.get('result') or {}).get('blackboard')
    require(isinstance(board, dict) and isinstance(board.get('segments'), list))
    board_text = json.dumps(board, ensure_ascii=False, separators=(',', ':'))
    _plain(board_text, names)
    require(len(question) <= 2000 and len(answer) <= 2400 and len(board_text) <= 7000)
    lineage = envelope.get('ordinary_followup_origin')
    if lineage is None:
        origin_question, origin_board = question, board
    else:
        require(type(lineage) is dict and set(lineage) == {'question', 'blackboard'})
        origin_question, origin_board = _plain(lineage['question'], names), lineage['blackboard']
        require(type(origin_board) is dict)
        origin_text = json.dumps(origin_board, ensure_ascii=False, separators=(',', ':'))
        _plain(origin_text, names)
        require(len(origin_question) <= 2000 and len(origin_text) <= 7000)
    return {'turn_id': turn_id, 'source_sha256': proof['source_sha256'], 'subject': row['subject'],
            'question': question, 'answer': answer, 'blackboard': board,
            'origin_question': origin_question, 'origin_blackboard': origin_board,
            'privacy_sha256': _hash(proof['privacy'])}
