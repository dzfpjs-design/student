"""Transfer requires a new question, not merely a day since its explanation."""
from datetime import timedelta
from uuid import uuid4
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.learning_obligations import (
    _question_catalog, project_child_mastery_evidence, project_item_exposure, tutor_question_history,
)
from test_mistake_gate_reliability import CHILD, ITEM, SECOND, flow, prepare, submit, alias


def taught(flow, *, item=SECOND, age=timedelta(days=3), other=False):
    db, _, sid, clock = flow
    repo = StudyRepository(db)
    if other:
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
            c.commit()
        sid = repo.start_session('child-junior-3', 'other-child-hint')['session_id']
    with db.connect() as c:
        question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (item,)).fetchone()[0]
    turn = repo.save_tutor_turn(sid, None, '数学', question, 'Synthetic explanation, not a child record.',
        'local', 'synthetic', str(uuid4()))
    with db.connect() as c:
        c.execute('UPDATE tutor_turn SET created_at=? WHERE turn_id=?',
            ((clock[0]-age).isoformat(), turn['turn_id']))
        if other:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
            c.execute("UPDATE daily_session SET status='active',state='study' WHERE session_id=?", (flow[2],))
        c.commit()
    return turn


def test_already_explained_question_is_not_offered_as_new_transfer(flow):
    taught(flow)
    book, case = prepare(flow)
    assert book.next_activity(case, CHILD)['kind'] == 'blocked'


def test_direct_submission_keeps_response_but_cannot_certify_explained_transfer(flow):
    taught(flow)
    book, case = prepare(flow)
    result = submit(book, case)
    assert result['attempt_result']['correctness'] == 1
    assert result['attempt_result']['independent'] is False
    assert '不能把这次作答当作新题迁移通过' in result['attempt_result']['feedback']
    assert result['mastery_gate']['satisfied'] == {}
    with flow[0].connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 1
        assert 'transfer_question_previously_taught' in c.execute('SELECT evidence_summary FROM learning_evidence').fetchone()[0]


@pytest.mark.parametrize('change', ['other_child', 'other_question', 'future', 'unsafe', 'wrong_session'])
def test_unrelated_or_unavailable_tutor_does_not_poison_new_question(flow, change):
    turn = taught(flow, item=ITEM if change == 'other_question' else SECOND,
        age=timedelta(seconds=-1) if change == 'future' else timedelta(days=3), other=change == 'other_child')
    with flow[0].connect() as c:
        if change == 'unsafe':
            c.execute("UPDATE tutor_turn SET safety_status='blocked' WHERE turn_id=?", (turn['turn_id'],))
        elif change == 'wrong_session':
            c.execute("UPDATE tutor_turn SET child_id='child-junior-3' WHERE turn_id=?", (turn['turn_id'],))
        c.commit()
    book, case = prepare(flow)
    assert book.next_activity(case, CHILD)['item']['diagnostic_item_id'] == SECOND
    assert submit(book, case)['mastery_gate']['satisfied'] == {'near_transfer': 1}


def test_renaming_a_taught_question_does_not_create_transfer_supply(flow, monkeypatch):
    taught(flow)
    alias(flow, monkeypatch, '000-renamed-taught-question')
    book, case = prepare(flow)
    assert book.next_activity(case, CHILD)['kind'] == 'blocked'


def test_material_prompt_without_exact_material_binding_is_not_a_history_match(flow):
    db, _, sid, clock = flow
    with db.connect() as c:
        item = dict(c.execute("SELECT * FROM diagnostic_item WHERE trim(stimulus_text)<>'' LIMIT 1").fetchone())
    StudyRepository(db).save_tutor_turn(sid, None, '英语', item['prompt'], 'Synthetic unrelated material.',
        'local', 'synthetic', 'no-material-binding')
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        catalog, identities = _question_catalog(c)
        history = tutor_question_history(c, CHILD, catalog, identities, as_of=clock[0].isoformat())
        assert identities[item['diagnostic_item_id']] not in history


def test_typed_material_history_survives_short_assistance_window(tmp_path, monkeypatch):
    from test_typed_tutor_exposure_http import reviewed_material, saved_hint
    from datetime import datetime

    with reviewed_material(tmp_path, monkeypatch) as case:
        turn, _ = saved_hint(case)
        at = (datetime.fromisoformat(case.answer['attempt']['created_at']) + timedelta(days=3)).isoformat()
        with case.db.connect() as c:
            c.execute('PRAGMA query_only=ON')
            before = c.total_changes
            item_id = case.prompt['diagnostic_item_id']
            assert not project_item_exposure(c, case.prompt['child_id'], item_id, as_of=at)['tutor_assisted']
            catalog, identities = _question_catalog(c)
            history = tutor_question_history(c, case.prompt['child_id'], catalog, identities, as_of=at)
            assert history[identities[item_id]][1] == 'tutor:' + turn['turn_id']
            assert c.total_changes == before


def test_later_teaching_does_not_revoke_prior_independent_transfer_or_delayed_reuse(flow):
    book, case = prepare(flow)
    assert submit(book, case)['mastery_gate']['satisfied'] == {'near_transfer': 1}
    flow[3][0] += timedelta(seconds=1)
    taught(flow, age=timedelta())
    flow[3][0] += timedelta(hours=49)
    assert submit(book, case, 'delayed_48h')['status'] == 'mastered'


def test_historical_gate_rejects_older_tutor_source_readonly_after_restart(flow):
    book, case = prepare(flow)
    submit(book, case)
    flow[3][0] += timedelta(hours=49)
    assert submit(book, case, 'delayed_48h')['status'] == 'mastered'
    taught(flow, age=timedelta(days=5))
    with Database(flow[0].path).connect() as c:
        c.execute('PRAGMA query_only=ON')
        before = c.total_changes
        gate = MistakeBookService(Database(flow[0].path))._gate_summary(c, case)
        assert gate['missing'][0] == 'near_transfer'
        projection = project_child_mastery_evidence(c, CHILD, as_of=flow[3][0].isoformat())
        result = next(r for r in projection['node-p4-math-arithmetic']['effective_evidence'] if r['stage'] == 'near_transfer')
        assert result['independent'] == 0
        assert result['independence_reason'] == 'transfer_question_previously_taught'
        assert result['transfer_tutor_refs']
        assert c.total_changes == before
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 2


def test_qualified_distinct_local_supply_replaces_taught_item_and_advances_gate(flow):
    from home_study_agent.variant_supply import VariantSupplyService
    from home_study_agent.local_supply import validate_item

    class NoCloud:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError('Cloud calls forbidden')

    taught(flow)
    book, case = prepare(flow)
    assert book.next_activity(case, CHILD)['kind'] == 'blocked'
    supply = VariantSupplyService(flow[0], NoCloud())
    supply.enqueue_local(case, CHILD, 'supply-new-transfer')
    assert supply.process_local_pending()['jobs'][0]['status'] == 'completed'
    with flow[0].connect() as c:
        candidate = c.execute("SELECT candidate_id FROM variant_candidate WHERE mistake_id=? AND stage='near_transfer'",
            (case,)).fetchone()[0]
    qualified = supply.qualify_local(candidate, CHILD, 'qualify-new-transfer')
    with flow[0].connect() as c:
        item = validate_item(c, CHILD, qualified['diagnostic_item_id'])
    activity = book.next_activity(case, CHILD)
    assert activity['item']['diagnostic_item_id'] == item['diagnostic_item_id']
    result = submit(book, case, item=item['diagnostic_item_id'], answer=json.loads(item['expected_answer_json'])['exact_answers'][0])
    assert result['attempt_result']['independent'] is True
    assert result['mastery_gate']['satisfied'] == {'near_transfer': 1}


def test_http_submission_replay_restart_and_report_keep_reference_not_mastery(flow):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_mistake_report_plan_projection import finish_report

    taught(flow)
    db = flow[0]
    original = flow[1].submit_diagnostic(flow[2], ITEM, 'wrong', 'report-original-failure')
    book = MistakeBookService(db)
    with db.connect() as c:
        question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone()[0]
    case = book.capture(child_id=CHILD, knowledge_node_id='node-p4-math-arithmetic', subject='数学',
        title='Synthetic original failure', prompt_summary=question, submitted_answer='wrong', source_type='manual',
        source_ref=None, confidence=1, client_action_id='original-failure-case', diagnostic_item_id=ITEM,
        source_evidence_id=original['evidence_id'])['mistake_id']
    book.confirm(case, CHILD, 'confirm', 'original-confirm')
    book.diagnose(case, CHILD, 'unknown', 'Candidate, not a diagnosis', .6, 'original-diagnose')
    book.add_remediation(case, CHILD, 'active_recall', 'completed', None, 'original-remediate')
    config = Settings(data_dir=db.path.parent, database_path=db.path, data_encryption_key=b'0'*32,
        maintenance_enabled=False)
    payload = dict(child_id=CHILD, stage='near_transfer', diagnostic_item_id=SECOND,
        answer='42', hint_level=0, client_action_id='already-taught-http-answer')
    for _ in range(2):
        with TestClient(create_app(config)) as client:
            reply = client.post(f'/v1/mistakes/{case}/attempts', json=payload)
            assert reply.status_code == 200, reply.text
            assert reply.json()['mastery_gate']['satisfied'] == {}
    repo, report = finish_report(flow)
    saved = StudyRepository(Database(db.path)).get_daily_report(report['report_id'])
    mistake = next(r for r in saved['evidence_detail']['mistakes'] if r['mistake_id'] == case)
    assert mistake['status'] != 'mastered'
    assert mistake['mastery_gate']['missing'][0] == 'near_transfer'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 1
        assert c.execute('SELECT independent FROM learning_evidence WHERE evidence_id IN (SELECT evidence_id FROM mistake_attempt)').fetchone()[0] == 0
