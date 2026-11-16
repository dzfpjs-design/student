"""Shared consumers of real typed original audio reviews; no C05 adapter veto."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from uuid import uuid4
from copy import deepcopy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_obligations import project_item_exposure
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.repository import StudyRepository
from home_study_agent.teaching_question_target import resolve_question_target, _digest
from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, AgentPlanDecision, ObjectiveDraft, PlannedStep
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
import test_companion_reviewed_return_http as original
from test_companion_reviewed_return_http import CHILD, OTHER, balance, facts, journey, update, unlock


def register(db, sid, capability, arguments, mode):
    key = str(uuid4())
    cap = CapabilityRegistry().require(capability, 'study')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,tool_status,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,'','local_evaluation','','{"local_only":true,"cloud_transmission_allowed":false}',
                'pending','controlled-local-review',?,?)''',
            (key, CHILD, sid, datetime.now(timezone.utc).isoformat(), key))
        AgentRuntimeStore(db).persist_plan(c, child_id=CHILD, session_id=sid, agent_turn_id=key,
            client_action_id=key, context_manifest={'local_only': True}, decision=AgentPlanDecision(
                ObjectiveDraft(mode, 'learning', 'Review this original answer', ('Answer independently',), 'diagnose'),
                PlannedStep(cap, arguments, 'Review the saved original', cap.expected_evidence), '', False, 1))
        c.commit()
    return key


@contextmanager
def reviewed_material(tmp_path, monkeypatch):
    def before_answer(case):
        register(case.db, case.sid, 'learning.request_response', {'response': 'Answer the original question'}, 'new')

    def assess(db, sid, answer):
        candidate = RubricEvidenceService(db).diagnostic_candidate_input(CHILD, answer['attempt']['evidence_id'])
        key = register(db, sid, 'learning.evaluate_response', candidate['arguments'], 'continue')
        return LearningOutcomeEvaluator(db, StudyRepository(db), None, None).prepare_local_evaluation(key)['evaluation']['rubric_assessment']

    monkeypatch.setattr(original, 'local_assessment', assess)
    with journey(tmp_path, monkeypatch, 'speaking', before_answer=before_answer) as case:
        yield case


def saved_hint(case):
    with case.db.connect() as c:
        target = resolve_question_target(c, case.sid, subject='英语',
            question=case.prompt['item']['prompt'], prompt_id=case.prompt['prompt_id'],
            item_version=case.prompt['item_version'])
    assert target['status'] == 'resolved', target
    turn = StudyRepository(case.db).save_tutor_turn(case.sid, case.pid, '英语',
        case.prompt['item']['prompt'], 'Synthetic exact original hint.', 'local', 'synthetic',
        'typed-tutor-history', hint_level=1,
        output_envelope={'local_teaching_control': {'question_target': target}})
    # Recovered persisted history. Do not mutate the original answer/review.
    at = (datetime.fromisoformat(case.answer['attempt']['created_at']) - timedelta(microseconds=1)).isoformat()
    update(case.db, 'UPDATE tutor_turn SET created_at=? WHERE turn_id=?', (at, turn['turn_id']))
    return turn, target


def shared_receipt(case):
    with case.db.connect() as c:
        step = c.execute('SELECT step_id FROM rubric_assessment WHERE assessment_id=?',
            (case.assessment['assessment_id'],)).fetchone()[0]
        return RubricEvidenceService(case.db).reviewed_receipt(c, CHILD, step,
            now=datetime.now(timezone.utc))


def exposure(case):
    with case.db.connect() as c:
        return project_item_exposure(c, CHILD, case.prompt['diagnostic_item_id'],
            as_of=case.answer['attempt']['created_at'])


@pytest.mark.parametrize('consumer', ['exposure', 'reviewed_receipt'])
def test_material_typed_hint_reaches_shared_consumers(tmp_path, monkeypatch, consumer):
    with reviewed_material(tmp_path, monkeypatch) as case:
        assert case.prompt['item']['stimulus_text']
        assert not exposure(case)['tutor_assisted']
        assert shared_receipt(case)['independent'] is True
        turn, _ = saved_hint(case)
        before = facts(case.db)
        if consumer == 'exposure':
            result = exposure(case)
            assert result['tutor_assisted'], result
            assert [r['turn_id'] for r in result['tutor_refs']] == [turn['turn_id']]
        else:
            receipt = shared_receipt(case)
            assert receipt is not None and receipt['independent'] is False, receipt
        assert facts(case.db) == before


def put_target(case, turn, target):
    update(case.db, 'UPDATE tutor_turn SET output_envelope_json=? WHERE turn_id=?',
        (json.dumps({'local_teaching_control': {'question_target': target}}), turn['turn_id']))


def test_corrupt_and_foreign_typed_audits_never_bind_by_node_or_current_prompt(tmp_path, monkeypatch):
    with reviewed_material(tmp_path, monkeypatch) as case:
        turn, target = saved_hint(case)
        assert exposure(case)['tutor_assisted']
        before = facts(case.db)
        variants = [
            ('child_id', OTHER), ('session_id', 'other-session'),
            ('knowledge_node_id', 'node-pet-writing-email'), ('subject', '数学'),
            ('question_hash', 'other-question'), ('status', 'unknown'),
            ('version', 'model_target_v1'), ('fingerprint', 'corrupt'),
        ]
        for field, value in variants:
            bad = deepcopy(target)
            bad[field] = value
            if field != 'fingerprint':
                bad['fingerprint'] = _digest(bad)
            put_target(case, turn, bad)
            assert not exposure(case)['tutor_assisted'], field
            assert shared_receipt(case)['independent'] is True, field
        for field, value in [('prompt_id', 'missing-prompt'), ('item_version', 'stale-version'),
                ('diagnostic_item_id', 'diag-pet-writing-1'), ('prompt_id', []), ('item_version', {}),
                ('review_task_id', 'other-review'), ('ocr_confirmation_id', 'other-ocr')]:
            bad = deepcopy(target)
            bad['binding'][field] = value
            bad['fingerprint'] = _digest(bad)
            put_target(case, turn, bad)
            assert not exposure(case)['tutor_assisted'], (field, value)
        for value in ('{', '[]', 'null', '1', json.dumps({'question_target': target}),
                json.dumps({'result': {'local_teaching_control': {'question_target': target}}}),
                json.dumps({'local_teaching_control': {'question_target': []}})):
            update(case.db, 'UPDATE tutor_turn SET output_envelope_json=? WHERE turn_id=?', (value, turn['turn_id']))
            assert not exposure(case)['tutor_assisted'], value
        put_target(case, turn, target)
        assert exposure(case)['tutor_assisted']
        assert shared_receipt(case)['independent'] is False
        assert facts(case.db) == before


def test_typed_prompt_snapshot_owner_and_original_time_are_required(tmp_path, monkeypatch):
    with reviewed_material(tmp_path, monkeypatch) as case:
        turn, target = saved_hint(case)
        with case.db.connect() as c:
            row = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
                (case.prompt['prompt_id'],)).fetchone())
            turn_at = c.execute('SELECT created_at FROM tutor_turn WHERE turn_id=?', (turn['turn_id'],)).fetchone()[0]
        for field, value in [('child_id', OTHER), ('item_version', 'stale'), ('item_json', '{}'),
                ('item_json', '[]'), ('item_json', '{'), ('created_at', 'bad-time'),
                ('created_at', case.answer['attempt']['created_at'])]:
            update(case.db, f'UPDATE diagnostic_prompt SET {field}=? WHERE prompt_id=?',
                (value, case.prompt['prompt_id']))
            assert not exposure(case)['tutor_assisted'], field
            update(case.db, f'UPDATE diagnostic_prompt SET {field}=? WHERE prompt_id=?',
                (row[field], case.prompt['prompt_id']))
        after = (datetime.fromisoformat(case.answer['attempt']['created_at']) + timedelta(microseconds=1)).isoformat()
        update(case.db, 'UPDATE tutor_turn SET created_at=? WHERE turn_id=?', (after, turn['turn_id']))
        assert not exposure(case)['tutor_assisted']
        assert shared_receipt(case)['independent'] is True
        update(case.db, 'UPDATE tutor_turn SET created_at=? WHERE turn_id=?', (turn_at, turn['turn_id']))
        assert exposure(case)['tutor_assisted']
        with case.db.connect() as c:
            future = datetime.fromisoformat(turn_at) + timedelta(hours=24)
            assert not project_item_exposure(c, CHILD, case.prompt['diagnostic_item_id'],
                as_of=future.isoformat())['tutor_assisted']
            assert not project_item_exposure(c, OTHER, case.prompt['diagnostic_item_id'],
                as_of=case.answer['attempt']['created_at'])['tutor_assisted']
        # Current source change cannot be treated as the original frozen version.
        update(case.db, "UPDATE diagnostic_item SET stimulus_text=stimulus_text||' revised' WHERE diagnostic_item_id=?",
            (case.prompt['diagnostic_item_id'],))
        assert not exposure(case)['tutor_assisted']
        assert shared_receipt(case) is None


def test_completed_safe_typed_hints_flow_through_shared_audio_and_restart(tmp_path, monkeypatch):
    with reviewed_material(tmp_path, monkeypatch) as case:
        turn, _ = saved_hint(case)
        before = facts(case.db)
        calls = len(case.gateway.requests)
        for kind, safety in [('hint', 'passed'), ('full_explanation', 'passed'), ('hint', 'answer_leak_replaced')]:
            update(case.db, 'UPDATE tutor_turn SET response_kind=?,safety_status=? WHERE turn_id=?',
                (kind, safety, turn['turn_id']))
            assert shared_receipt(case)['independent'] is False
            refs = exposure(case)['tutor_refs']
            assert refs == [{'turn_id': turn['turn_id'], 'occurred_at': refs[0]['occurred_at'],
                'matched_by': 'exact_persisted_question_reference'}]
            assert balance(case)['verified_child_return_sessions'] == 0
        for field, value in [('status', 'failed'), ('safety_status', 'blocked'), ('answer', ''),
                ('response_kind', 'child_answer')]:
            with case.db.connect() as c:
                old = c.execute(f'SELECT {field} FROM tutor_turn WHERE turn_id=?', (turn['turn_id'],)).fetchone()[0]
            update(case.db, f'UPDATE tutor_turn SET {field}=? WHERE turn_id=?', (value, turn['turn_id']))
            assert not exposure(case)['tutor_assisted'], field
            update(case.db, f'UPDATE tutor_turn SET {field}=? WHERE turn_id=?', (old, turn['turn_id']))
        assert facts(case.db) == before
    with TestClient(create_app(case.settings, model_gateway=case.gateway)) as client:
        assert shared_receipt(case)['independent'] is False
        assert balance(case, client, unlock(client))['verified_child_return_sessions'] == 0
        assert facts(case.db) == before
        assert len(case.gateway.requests) == calls


def test_shared_typed_lookup_does_not_swallow_sqlite_failures(tmp_path, monkeypatch):
    with reviewed_material(tmp_path, monkeypatch) as case:
        saved_hint(case)
        with case.db.connect() as c:
            c.set_authorizer(lambda operation, table, column, *rest:
                sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and table == 'diagnostic_prompt'
                else sqlite3.SQLITE_OK)
            with pytest.raises(sqlite3.DatabaseError, match='prohibited'):
                project_item_exposure(c, CHILD, case.prompt['diagnostic_item_id'],
                    as_of=case.answer['attempt']['created_at'])
