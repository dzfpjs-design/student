"""Real host admission and historical projection; synthetic sound is not PET content QA."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_obligations import project_child_mastery_evidence
from test_pet_media_evidence import media, CHILD, complete, review
from test_pet_scoped_listening import twice


def projected(db, evidence_id):
    with db.connect() as connection:
        before = connection.total_changes
        result = project_child_mastery_evidence(connection, CHILD)
        assert connection.total_changes == before
    return any(row['evidence_id'] == evidence_id for node in result.values()
               for row in node['effective_evidence'])


@pytest.mark.parametrize('plays', [1, 2])
def test_actual_answer_host_and_restart_use_original_scoped_audio(media, tmp_path, plays):
    db, _, sid, prompt, path, _ = media
    row = twice(media) if plays == 2 else complete(media)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token='synthetic-scoring-session')
    headers = {'X-HSC-Session': settings.session_token}
    prefix = f'/v1/sessions/{sid}/diagnostic-prompt'
    payload = {'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
               'answer': 'in front of the library', 'client_action_id': 'real-host-answer'}
    configured = Database(db.path, media_root=tmp_path)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post(prefix + '/answer', headers=headers, json=payload)
        assert response.status_code == 200, response.text
        answer = response.json()['attempt']
        evidence = answer['evidence_id']
        assert (answer['score_status'] == 'verified_local') is (plays == 2), answer
        assert projected(configured, evidence) is (plays == 2)
        with configured.connect() as connection:
            state = connection.execute('''SELECT ms.evidence_count FROM mastery_state ms
                JOIN learning_evidence le ON le.child_id=ms.child_id AND le.knowledge_node_id=ms.knowledge_node_id
                WHERE le.evidence_id=?''', (evidence,)).fetchone()
            assert (state['evidence_count'] if state else 0) == (1 if plays == 2 else 0)
            assert connection.execute('SELECT COUNT(*) FROM mastery_update_event WHERE evidence_id=?',
                                      (evidence,)).fetchone()[0] == (1 if plays == 2 else 0)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get(prefix, headers=headers, params={'prompt_id': prompt['prompt_id']})
        assert response.status_code == 200, response.text
        answer = response.json()['attempt']
        assert answer['evidence_id'] == evidence
        assert (answer['score_status'] == 'verified_local') is (plays == 2), answer
        with configured.connect() as connection:
            assert connection.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    # A caller without trusted runtime media configuration cannot infer authority.
    assert not projected(Database(db.path), evidence)
    if plays == 2:
        review(media, row, 'retract', 'withdraw-after-answer')
        assert not projected(configured, evidence)


def test_historical_receipt_rechecks_original_file_not_only_stored_score(media, tmp_path):
    twice(media)
    db, _, sid, prompt, path, _ = media
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    configured = Database(db.path, media_root=tmp_path)
    answer = DiagnosticPromptService(configured).answer(sid, prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], answer='in front of the library', client_action_id='answer')
    evidence = answer['attempt']['evidence_id']
    assert projected(configured, evidence)
    path.write_bytes(b'changed synthetic source')
    assert not projected(configured, evidence)


def test_materialization_failure_rolls_back_attempt_but_preserves_saved_answer(media, tmp_path):
    import sqlite3
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    twice(media)
    configured = Database(media[0].path, media_root=tmp_path)
    with configured.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_pet_mastery BEFORE INSERT ON mastery_update_event
            BEGIN SELECT RAISE(ABORT, 'synthetic mastery failure'); END""")
        connection.commit()
    service = DiagnosticPromptService(configured)
    kwargs = dict(prompt_id=media[3]['prompt_id'], item_version=media[3]['item_version'],
                  answer='in front of the library', client_action_id='retry-answer')
    with pytest.raises(sqlite3.IntegrityError, match='synthetic mastery failure'):
        service.answer(media[2], **kwargs)
    with configured.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        row = connection.execute('SELECT status,answer_json FROM diagnostic_prompt WHERE prompt_id=?',
                                 (media[3]['prompt_id'],)).fetchone()
        assert row['status'] == 'answer_saved' and 'library' in row['answer_json']
        connection.execute('DROP TRIGGER fail_pet_mastery')
        connection.commit()
    first = service.answer(media[2], **kwargs)
    second = service.answer(media[2], **kwargs)
    assert first['attempt']['evidence_id'] == second['attempt']['evidence_id']
    assert first['attempt']['score_status'] == 'verified_local'
    with configured.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM mastery_update_event').fetchone()[0] == 1
