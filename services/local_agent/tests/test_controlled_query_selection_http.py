"""Actual parent approval and child HTTP; inert assets, no player/model launch."""
import pytest

from home_study_agent.database import Database
from test_app import client_for
from test_companion_direct_start_atomic_http import task
from test_controlled_start_atomic_http import BODY, CHILD, counts, setup
from test_learning_runtime_http import NoCloud


def approve_second(client, db, tmp_path, kind, title='Synthetic story'):
    unlocked = client.post('/v1/parent/unlock', json={'pin': '4826'})
    assert unlocked.status_code == 200, unlocked.text
    headers = {'X-HSC-Parent': unlocked.json()['parent_token']}
    if kind == 'games':
        executable = tmp_path / 'SecondSynthetic.exe'
        executable.write_bytes(b'inert second synthetic executable; never launched')
        body = {'title': title, 'executable_path': str(executable), 'max_minutes': 6}
    else:
        (tmp_path / 'videos' / 'second.mp4').write_bytes(b'inert second video; never played')
        with db.connect() as c:
            resource = c.execute('''INSERT INTO curriculum_resource(corpus_id,external_resource_id,title,
                description,stage_title,grade_title,subject_title,semester,unit_path,transcript_text,
                video_relative_path,video_duration,local_video_available,import_job_id)
                VALUES('synthetic-media','102',?,'Synthetic only','初中','初三','数学','上册',
                'Synthetic unit','Synthetic reference','second.mp4',600,1,'synthetic-job')''',
                (title,)).lastrowid
            c.commit()
        body = {'curriculum_row_id': resource, 'max_minutes': 6}
    approved = client.post(f'/v1/parent/children/{CHILD}/controlled-{kind}/approvals',
                          headers=headers, json=body)
    assert approved.status_code == 200, approved.text


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('query,phrase', [('Ocean geography', '没有找到'), ('Synthetic', '多个')])
def test_explicit_unmatched_or_ambiguous_request_clarifies_without_pause_then_exact_choice_replays(
        tmp_path, kind, query, phrase):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        approve_second(client, db, tmp_path, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}/start'
        before = task(db, pid)
        refused = client.post(path, json={'query': query, 'client_action_id': 'ambiguous-original-request'})
        assert refused.status_code == 409, refused.text
        assert phrase in refused.json()['message']
        assert 'Synthetic exercise' in refused.json()['message']
        assert 'Synthetic story' in refused.json()['message']
        assert str(tmp_path) not in refused.text
        assert task(db, pid) == before
        assert counts(db, pid, kind) == (0, 0, 0)
        chosen_body = {'query': 'Synthetic story', 'client_action_id': 'child-explicit-corrected-choice'}
        chosen = client.post(path, json=chosen_body)
        assert chosen.status_code == 200, chosen.text
        assert chosen.json()['title'] == 'Synthetic story'
        assert counts(db, pid, kind) == (1, 1, 1)
        changed = client.post(path, json={**chosen_body, 'query': 'Synthetic exercise'})
        assert changed.status_code == 409, changed.text
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        replay = client.post(path, json=chosen_body)
        assert replay.status_code == 200, replay.text
        identity = 'game_session_id' if kind == 'games' else 'content_session_id'
        assert replay.json()[identity] == chosen.json()[identity]
        assert replay.json()['title'] == 'Synthetic story'
        assert replay.json()['launch_allowed'] is False and replay.json()['replayed'] is True
        assert counts(db, pid, kind) == (1, 1, 1)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_unique_case_insensitive_query_selects_intended_approved_item(tmp_path, kind):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        approve_second(client, db, tmp_path, kind)
        result = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start',
                             json={**BODY, 'query': 'synthetic STORY'})
        assert result.status_code == 200, result.text
        assert result.json()['title'] == 'Synthetic story'
        assert counts(db, pid, kind) == (1, 1, 1)


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('query', ['', 'Synthetic exercise'])
def test_generic_empty_request_and_exact_title_keep_existing_contract(tmp_path, kind, query):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        approve_second(client, db, tmp_path, kind, title='Synthetic exercise extended')
        result = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start', json={**BODY, 'query': query})
        assert result.status_code == 200, result.text
        assert result.json()['title'] == 'Synthetic exercise'
        assert counts(db, pid, kind) == (1, 1, 1)


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_duplicate_exact_titles_do_not_make_distinct_files_interchangeable(tmp_path, kind):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        approve_second(client, db, tmp_path, kind, title='Synthetic exercise')
        before = task(db, pid)
        result = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start', json=BODY)
        assert result.status_code == 409, result.text
        assert '同名' in result.json()['message']
        assert task(db, pid) == before
        assert counts(db, pid, kind) == (0, 0, 0)
