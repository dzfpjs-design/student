"""Source text erasure preserves actual child answers, never their old qualification."""
from functools import partial
import json
import sqlite3

import pytest

from home_study_agent.database import Database, LocalMediaRuntime
from home_study_agent.learning_state import LearningStateService
from home_study_agent.learning_obligations import project_child_mastery_evidence
from home_study_agent.repository import StudyRepository
from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
from test_curriculum_supply import prepared, publish, CHILD, PROMPT
from test_mistake_gate_reliability import flow, ITEM
from test_learning_outcome_mastery_gate import gate
from home_study_agent.privacy_evidence_cleanup import parent_textbook_sources, purge_parent_textbook_sources
from home_study_agent.knowledge_graph.privacy_cleanup import purge_imported
from test_pet_media_evidence import media


def enable_parent_page_media(database: Database) -> None:
    database.local_media_runtime=LocalMediaRuntime(database.path.parent,b'0'*32)


@pytest.fixture
def native(tmp_path):
    db, supply, payload = prepared(tmp_path)
    enable_parent_page_media(db)
    from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
    from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageService, TextbookOcrPageInput
    from test_parent_textbook_capture import Adapter
    from home_study_agent.repository import utc_now
    with db.connect() as c:
        c.execute("INSERT INTO device_binding(device_type,system_id_hash,label,capabilities_json,verified_at,updated_at) VALUES('document_camera','synthetic-native-camera','synthetic','{}',?,?)", (utc_now(),utc_now()))
        c.commit()
    capture = ParentTextbookCaptureService(db, Adapter())
    lease = capture.acquire(CHILD, 'synthetic-native-camera', {})
    page = capture.recognize(CHILD, lease['lease_id'], 'aGVsbG8=', {'title':'isolated native page'}, 'native-ocr')
    capture.review(CHILD, page['capture_ref'], PROMPT, 'native-reviewed-text')
    ref = capture.page_source_ref(page['capture_ref'])
    TextbookOcrPageService(db).import_page(TextbookOcrPageInput(title='isolated native page', ocr_text=PROMPT, source_ref=ref))
    # Establish synthetic imported-page provenance before review/publication.
    # No already-qualified item or receipt is modified to make it eligible.
    with db.connect() as c:
        c.execute('UPDATE knowledge_source SET source_ref=? WHERE child_id=?', (ref,CHILD))
        c.commit()
    published = publish(supply, payload)
    learning = LearningStateService(db)
    sid = StudyRepository(db).start_session(CHILD, 'native-erasure-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    attempt = learning.submit_diagnostic(sid, published['diagnostic_item_id'], '5000', 'native-original-answer')
    assert attempt['score_status'] == 'verified_local'
    return db, supply, published, attempt, sid


def cleanup(c, scopes):
    return purge_parent_textbook_sources(c, CHILD, purge_imported=partial(purge_imported, affected_scopes=scopes))


def test_actual_published_source_erasure_preserves_answer_not_question_and_replays(native):
    db, supply, published, attempt, _ = native
    scopes = {}
    with db.connect() as c:
        before = project_child_mastery_evidence(c, CHILD)
        node = 'node-p4-math-arithmetic'
        assert attempt['evidence_id'] in {r['evidence_id'] for r in before[node]['effective_evidence']}
        c.execute('BEGIN IMMEDIATE')
        counts = cleanup(c, scopes)
        assert scopes == {CHILD: {node}}
        assert counts['diagnostic_item_redacted'] == 1
        assert c.in_transaction
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone()[0] == '5000'
        assert c.execute('SELECT evidence_id FROM learning_evidence WHERE evidence_id=?', (attempt['evidence_id'],)).fetchone()
        assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (published['diagnostic_item_id'],)).fetchone()[0] != PROMPT
        assert c.execute('SELECT COUNT(*) FROM knowledge_excerpt').fetchone()[0] == 0
        for table, column in [('curriculum_supply_package','source_snapshot_json'), ('curriculum_supply_package','payload_json'),
                              ('specialized_supply_candidate','prompt'), ('specialized_supply_job','source_snapshot_json')]:
            assert all(PROMPT not in row[0] for row in c.execute(f'SELECT {column} FROM {table}'))
        assert attempt['evidence_id'] not in {r['evidence_id'] for r in project_child_mastery_evidence(c, CHILD)[node]['effective_evidence']}
        LearningStateService(db).replay_mastery(CHILD, node, connection=c, reason='source_deleted')
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        c.commit()
    assert supply.get(CHILD, published['candidate_id'])['eligible'] is False
    with Database(db.path).connect() as c:
        assert c.execute('SELECT evidence_count FROM mastery_state WHERE child_id=? AND knowledge_node_id=?', (CHILD,node)).fetchone()[0] == 0
        c.execute('BEGIN IMMEDIATE')
        assert cleanup(c, {}).get('diagnostic_item_redacted', 0) == 0
        c.rollback()


def test_failure_rolls_back_source_body_receipts_and_collector(native):
    db, _, published, attempt, _ = native
    with db.connect() as c:
        c.execute("CREATE TRIGGER synthetic_native_failure BEFORE UPDATE ON diagnostic_item BEGIN SELECT RAISE(ABORT,'native erasure fault'); END")
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        scopes = {}
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_STORAGE_FAILURE'):
            cleanup(c, scopes)
        assert scopes == {}
        assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (published['diagnostic_item_id'],)).fetchone()[0] == PROMPT
        assert c.execute('SELECT COUNT(*) FROM knowledge_excerpt').fetchone()[0] == 1
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone()[0] == '5000'
        c.rollback()


def test_native_cleanup_cannot_omit_deferred_projection(native):
    db, _, published, _, _ = native
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_NATIVE_PROJECTION_COLLECTOR_REQUIRED'):
            cleanup(c, None)
        assert c.execute('SELECT status FROM diagnostic_item WHERE diagnostic_item_id=?', (published['diagnostic_item_id'],)).fetchone()[0] == 'active'
        c.rollback()


def test_saved_prompt_is_invalidated_and_exact_retry_cannot_restore_source(native):
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    db, _, published, _, sid = native
    prompts = DiagnosticPromptService(db)
    request = dict(track='school',preferred_item_id=published['diagnostic_item_id'],client_action_id='native-prompt')
    before = prompts.prepare(sid, **request)
    assert before['status'] == 'waiting_answer'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        cleanup(c, {})
        c.commit()
    replay = DiagnosticPromptService(Database(db.path)).prepare(sid, **request)
    assert replay['status'] == 'invalidated'
    assert replay['prompt']['prompt_id'] == before['prompt']['prompt_id']
    assert PROMPT not in json.dumps(replay,ensure_ascii=False)


def test_explicit_reimport_is_new_source_not_revival_of_old_qualification(native):
    from home_study_agent.knowledge_base import KnowledgeBaseService
    from home_study_agent.local_supply import validate_item, LocalSupplyError
    db, _, published, _, _ = native
    with db.connect() as c:
        old = c.execute('SELECT source_id FROM knowledge_source WHERE child_id=?', (CHILD,)).fetchone()[0]
        c.execute('BEGIN IMMEDIATE')
        cleanup(c, {})
        c.commit()
    restored = KnowledgeBaseService(db).import_source(CHILD, {
        'source_type':'school_material', 'title':'Explicitly reimported synthetic material',
        'subject':'数学', 'grade_band':'四年级', 'source_ref':'isolated-new-parent-import',
        'license_status':'family_owned_index', 'parent_reviewed':True, 'content_text':PROMPT})
    assert restored['source_id'] != old
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_excerpt WHERE source_id=?', (restored['source_id'],)).fetchone()[0] > 0
        with pytest.raises(LocalSupplyError):
            validate_item(c, CHILD, published['diagnostic_item_id'])


def test_unscored_probe_answer_is_not_destroyed_by_parent_probe_fk(flow):
    from test_mistake_gate_reliability import prepare
    from test_mistake_diagnosis import propose, ask, respond
    from home_study_agent.privacy_native_sources import purge_native_sources
    db, _, _, _ = flow
    book, case = prepare(flow)
    propose(book, case)
    probe = ask(book, case)
    answer = respond(book, case, probe['event_id'], 'child original explanation awaiting score')
    from home_study_agent.privacy_preserved_response import ensure_schema, read_preserved_responses
    with db.connect() as c:
        ensure_schema(c)
        c.execute("UPDATE diagnostic_item SET source_ref='unscored-probe-origin' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        counts = purge_native_sources(c, CHILD, {'unscored-probe-origin'}, affected_scopes={})
        assert counts['child_responses_preserved'] == 1
        assert c.execute('SELECT COUNT(*) FROM mistake_diagnosis_event WHERE mistake_id=?', (case,)).fetchone()[0] == 0
        saved = read_preserved_responses(c, CHILD, case)
        assert saved[0]['response_text'] == 'child original explanation awaiting score'
        assert saved[0]['response_id'] == answer['event_id']
        assert saved[0]['score_authority'] == 'none' and saved[0]['source_erased']
        assert read_preserved_responses(c, 'child-junior-3', case) == []
        c.commit()
    with Database(db.path).connect() as c:
        assert read_preserved_responses(c, CHILD, case) == saved


def test_local_rubric_execution_copies_erased_without_losing_child_response(gate):
    from home_study_agent.privacy_native_sources import purge_native_sources
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.agent_runtime import AgentRuntimeStore
    result, receipt, turn = gate.run(local=True)
    snapshot = result['evaluation']['rubric_assessment']
    rubrics = RubricEvidenceService(gate.db)
    rubrics.review(CHILD, snapshot['assessment_id'], expected_revision=0, client_action_id='source-review',
        reviewer_id='parent', reviewer_role='parent', operation='approve', reason='synthetic original review',
        independent=True, hint_level=0, dimensions=[],
        criterion_evidence=[{'criterion': value, 'response_quote': '90'} for value in snapshot['criteria']])
    runtime = AgentRuntimeStore(gate.db)
    with gate.db.connect() as c:
        # Model a legacy native item with an exact, now-withdrawn page origin.
        c.execute("UPDATE diagnostic_item SET source_ref='isolated-native-source' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        scopes = {}
        counts = purge_native_sources(c, CHILD, {'isolated-native-source'}, affected_scopes=scopes)
        assert counts['rubric_assessment'] == 1 and counts['rubric_review_event'] == 1
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        execution = c.execute('SELECT * FROM learning_evaluation_execution').fetchone()
        assert execution['state'] == 'unknown' and execution['error_code'] == 'SOURCE_DELETED'
        saved = json.loads(execution['record_json'])
        assert saved['child_response'] == '90' and 'question_asked' not in saved
        assert execution['evaluation_json'] is None and execution['result_json'] is None
        assert c.execute('SELECT transcript FROM agent_turn WHERE agent_turn_id=?', (turn,)).fetchone()[0] == '90'
        assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (receipt['attempt_id'],)).fetchone()[0] == '90'
        for child, nodes in scopes.items():
            for node in nodes:
                gate.learning.replay_mastery(child, node, connection=c, reason='source_deleted')
            runtime.reconcile_learning_state(child, connection=c)
        c.commit()
    assert gate.evaluator.execution_status(turn)['status'] == 'unknown'
    from home_study_agent.learning_outcome_evaluator import LearningEvaluationUnknownError
    with pytest.raises(LearningEvaluationUnknownError):
        gate.evaluator.recover_ready(turn)
    assert not gate.evaluator.current_mastery_gate(gate.objective)['outcomes'][0]['eligible']


def test_shared_source_preserves_both_child_answers_but_not_question_copies(flow):
    from home_study_agent.privacy_native_sources import purge_native_sources
    db, learning, sid, clock = flow
    first = learning.submit_diagnostic(sid, ITEM, '90', 'child-original')
    repo = StudyRepository(db)
    from home_study_agent.document_monitoring import DocumentMonitoringService
    DocumentMonitoringService(db)
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='四年级' WHERE child_id='child-junior-3'")
        c.commit()
    other_sid = repo.switch_active_session_child(sid, 'child-junior-3', 'other-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other_sid,))
        c.commit()
    second = learning.submit_diagnostic(other_sid, ITEM, '91', 'other-original')
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET source_ref='isolated-shared-native' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        scopes = {}
        purge_native_sources(c, CHILD, {'isolated-shared-native'}, affected_scopes=scopes)
        assert set(scopes) == {CHILD, 'child-junior-3'}
        for receipt, answer in [(first, '90'), (second, '91')]:
            assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (receipt['attempt_id'],)).fetchone()[0] == answer
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
        c.commit()


def test_actual_http_native_source_erasure_replays_and_restart_keeps_receipt(native):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, _, published, attempt, _ = native
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='native-test', data_encryption_key=b'0'*32)
    local = {'X-HSC-Session': settings.session_token}
    deletion_id = None
    for restarted in (False, True):
        with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
            token = client.post('/v1/parent/' + ('unlock' if restarted else 'setup'), headers=local, json={'pin':'2468'}).json()['parent_token']
            response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
                json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'native-source-delete'})
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'completed'
            if deletion_id:
                assert response.json()['deletion_id'] == deletion_id
            deletion_id = response.json()['deletion_id']
            with db.connect() as c:
                assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone()[0] == '5000'
                assert c.execute('SELECT evidence_count FROM mastery_state WHERE child_id=? AND knowledge_node_id=?', (CHILD,'node-p4-math-arithmetic')).fetchone()[0] == 0
                assert c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (published['diagnostic_item_id'],)).fetchone()[0] == '题源已删除'
                assert not parent_textbook_sources(c, CHILD)


@pytest.mark.parametrize('callback', ['replay_source_mastery', 'reconcile_source_deletion'])
def test_actual_native_callback_failure_rolls_back_then_same_key_recovers(native, monkeypatch, callback):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, _, published, attempt, _ = native
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='native-fault', data_encryption_key=b'0'*32)
    local = {'X-HSC-Session':settings.session_token}
    app = create_app(settings, model_gateway=NoCloud())
    with TestClient(app, raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        with db.connect() as c:
            before_item = tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (published['diagnostic_item_id'],)).fetchone())
            before_attempt = tuple(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone())
            before_cache = [tuple(r) for r in c.execute('SELECT * FROM mastery_state WHERE child_id=?', (CHILD,))]
            before_sources = parent_textbook_sources(c, CHILD)
        def fail(*args, **kwargs):
            assert kwargs['connection'].in_transaction
            raise sqlite3.OperationalError('synthetic final projection failure')
        with monkeypatch.context() as scoped:
            scoped.setattr(app.state.privacy_lifecycle, callback, fail)
            response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
                json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'native-callback-fault'})
        assert response.headers.get('content-type', '').startswith('application/json'), response.text
        assert response.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE'
        assert response.json()['retryable']
        with db.connect() as c:
            assert tuple(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (published['diagnostic_item_id'],)).fetchone()) == before_item
            assert tuple(c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone()) == before_attempt
            assert [tuple(r) for r in c.execute('SELECT * FROM mastery_state WHERE child_id=?', (CHILD,))] == before_cache
            assert parent_textbook_sources(c, CHILD) == before_sources
            job = c.execute("SELECT deletion_id,status FROM deletion_job WHERE client_action_id='native-callback-fault'").fetchone()
            assert job['status'] == 'failed'
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', headers=local, json={'pin':'2468'}).json()['parent_token']
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
            json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'native-callback-fault'})
        assert response.status_code == 200, response.text
        assert response.json()['deletion_id'] == job['deletion_id']
        assert response.json()['status'] == 'completed'


@pytest.mark.parametrize('column', ['payload_json', 'request_json'])
def test_actual_native_and_unrelated_corrupt_audio_roll_back_together(media, column):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
    from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageService, TextbookOcrPageInput
    from home_study_agent.repository import utc_now
    from test_parent_textbook_capture import Adapter
    from test_pet_audio_rubric_review import prepared as audio_prepared
    from test_companion_activity_host import NoCloud
    db, rubric, snapshot, body, _ = audio_prepared(media)
    enable_parent_page_media(db)
    rubric.review(CHILD, snapshot['assessment_id'], **body)
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='native-audio-test')
    local = {'X-HSC-Session': settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        with db.connect() as c:
            c.execute("INSERT INTO device_binding(device_type,system_id_hash,label,capabilities_json,verified_at,updated_at) VALUES('document_camera','native-audio-camera','synthetic','{}',?,?)", (utc_now(),utc_now()))
            c.commit()
        capture = ParentTextbookCaptureService(db, Adapter())
        lease = capture.acquire(CHILD, 'native-audio-camera', {})
        artifact = capture.recognize(CHILD, lease['lease_id'], 'aGVsbG8=', {'title':'synthetic native page'}, 'native-audio-page')
        ref = capture.page_source_ref(artifact['capture_ref'])
        page = TextbookOcrPageService(db).import_page(TextbookOcrPageInput(title='synthetic native', ocr_text='private synthetic native text', source_ref=ref))
        with db.connect() as c:
            original = dict(c.execute('SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?', (ITEM,)).fetchone())
            original.update(diagnostic_item_id='native-with-unrelated-audio',source_ref=ref,prompt='private synthetic native text')
            c.execute('INSERT INTO diagnostic_item (' + ','.join(original) + ') VALUES (' + ','.join('?' for _ in original) + ')', tuple(original.values()))
            event = dict(c.execute('SELECT * FROM rubric_review_event WHERE assessment_id=?', (snapshot['assessment_id'],)).fetchone())
            event.update(event_id='damaged-unrelated-audio',revision=2,client_action_id='damaged-unrelated-audio')
            intact = dict(event)
            event[column] = '{broken'
            c.execute('INSERT INTO rubric_review_event (' + ','.join(event) + ') VALUES (' + ','.join('?' for _ in event) + ')', tuple(event.values()))
            c.commit()
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
            json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'native-audio-delete'})
        assert response.headers.get('content-type', '').startswith('application/json'), response.text
        assert response.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE'
        assert response.json()['retryable']
        with db.connect() as c:
            assert c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (page['resource_id'],)).fetchone()
            assert c.execute("SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id='native-with-unrelated-audio'").fetchone()[0] == 'private synthetic native text'
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event WHERE assessment_id=?', (snapshot['assessment_id'],)).fetchone()[0] == 2
            job = c.execute("SELECT deletion_id,status FROM deletion_job WHERE client_action_id='native-audio-delete'").fetchone()
            assert job['status'] == 'failed'
            # Restore exactly the synthetic damaged copy; no trigger bypass.
            c.execute("DELETE FROM rubric_review_event WHERE event_id='damaged-unrelated-audio'")
            c.execute('INSERT INTO rubric_review_event (' + ','.join(intact) + ') VALUES (' + ','.join('?' for _ in intact) + ')', tuple(intact.values()))
            c.commit()
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/unlock', headers=local, json={'pin':'2468'}).json()['parent_token']
        response = client.post(f'/v1/parent/children/{CHILD}/delete', headers={**local,'X-HSC-Parent':token},
            json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'native-audio-delete'})
        assert response.status_code == 200, response.text
        assert response.json()['deletion_id'] == job['deletion_id'] and response.json()['status'] == 'completed'


def test_actual_preserved_probe_history_auth_child_scope_and_restart(flow):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.repository import utc_now
    from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
    from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageService, TextbookOcrPageInput
    from test_parent_textbook_capture import Adapter
    from test_mistake_gate_reliability import prepare
    from test_mistake_diagnosis import propose, ask, respond
    from test_companion_activity_host import NoCloud
    db, _, _, _ = flow
    enable_parent_page_media(db)
    book, case = prepare(flow)
    propose(book, case)
    question = ask(book, case)
    answer = respond(book, case, question['event_id'], 'Synthetic original answer, still unscored')
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='archive-host')
    local = {'X-HSC-Session': settings.session_token}
    path = f'/v1/parent/children/{CHILD}/mistakes/{case}/preserved-responses'
    saved = None
    for restarted in (False, True):
        with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
            assert client.get(path, headers=local).status_code in {401,403}
            token = client.post('/v1/parent/' + ('unlock' if restarted else 'setup'), headers=local, json={'pin':'2468'}).json()['parent_token']
            headers = {**local,'X-HSC-Parent':token}
            assert client.get(f'/v1/parent/children/child-junior-3/mistakes/{case}/preserved-responses', headers=headers).status_code == 404
            if not restarted:
                with db.connect() as c:
                    c.execute("INSERT INTO device_binding(device_type,system_id_hash,label,capabilities_json,verified_at,updated_at) VALUES('document_camera','probe-archive-camera','synthetic','{}',?,?)", (utc_now(),utc_now()))
                    c.commit()
                capture = ParentTextbookCaptureService(db, Adapter())
                lease = capture.acquire(CHILD, 'probe-archive-camera', {})
                page = capture.recognize(CHILD, lease['lease_id'], 'aGVsbG8=', {'title':'synthetic probe original'}, 'probe-original-page')
                ref = capture.page_source_ref(page['capture_ref'])
                TextbookOcrPageService(db).import_page(TextbookOcrPageInput(title='synthetic',ocr_text='Synthetic original question source',source_ref=ref))
                with db.connect() as c:
                    c.execute('UPDATE diagnostic_item SET source_ref=? WHERE diagnostic_item_id=?', (ref,ITEM))
                    c.commit()
                response = client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers,
                    json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'preserve-unscored-probe'})
                assert response.status_code == 200, response.text
                assert response.json()['status'] == 'completed'
            result = client.get(path, headers=headers)
            assert result.status_code == 200, result.text
            assert result.headers.get('cache-control') == 'no-store'
            rows = result.json()['items']
            assert len(rows) == 1 and rows[0]['response_id'] == answer['event_id']
            assert rows[0]['response_text'] == 'Synthetic original answer, still unscored'
            assert rows[0]['source_erased'] and rows[0]['score_authority'] == 'none'
            if saved:
                assert rows == saved
            saved = rows
