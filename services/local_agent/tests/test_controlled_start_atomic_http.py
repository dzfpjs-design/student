"""Real host transactions; synthetic inert media/executable, never launched."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_app import advance_to_study, client_for
from test_companion_direct_start_atomic_http import task
from test_learning_runtime_http import NoCloud


CHILD = 'child-junior-3'


class DropFirstSuccessfulReply:
    def __init__(self, app, path):
        self.app, self.path, self.dropped = app, path, False

    async def __call__(self, scope, receive, send):
        async def reply(message):
            if (scope.get('path') == self.path and not self.dropped
                    and message['type'] == 'http.response.start' and message['status'] == 200):
                self.dropped = True
                raise OSError('isolated response connection lost after endpoint commit')
            await send(message)
        await self.app(scope, receive, reply)


def configure(client, db, tmp_path, kind):
    token = client.post('/v1/parent/setup', json={'pin': '4826'})
    assert token.status_code == 200, token.text
    headers = {'X-HSC-Parent': token.json()['parent_token']}
    path = f'/v1/parent/children/{CHILD}/controlled-{kind}'
    enabled = client.put(path + '/rule', headers=headers,
        json={'enabled': True, 'max_daily_minutes': 20, 'max_item_minutes': 8})
    assert enabled.status_code == 200, enabled.text
    if kind == 'games':
        executable = tmp_path / 'SyntheticExercise.exe'
        executable.write_bytes(b'inert synthetic executable; never launched')
        body = {'title': 'Synthetic exercise', 'executable_path': str(executable), 'max_minutes': 6, 'note': 'Isolated test only'}
    else:
        video_root = tmp_path / 'videos'
        video_root.mkdir()
        (video_root / 'lesson.mp4').write_bytes(b'inert synthetic video; never played')
        with db.connect() as c:
            c.execute('''INSERT INTO curriculum_corpus(corpus_id,title,corpus_type,source_database_path,
                transcript_root_path,video_root_path,source_ref,license_status,review_status,status,
                resource_count,transcript_count,local_video_count,imported_at,updated_at)
                VALUES('synthetic-media','Synthetic media','seyk',?,?,?,'local-test','public_personal_use',
                'approved_baseline','ready',1,1,1,datetime('now'),datetime('now'))''',
                (str(tmp_path/'synthetic.db'),str(tmp_path/'transcripts'),str(video_root)))
            resource = c.execute('''INSERT INTO curriculum_resource(corpus_id,external_resource_id,title,
                description,stage_title,grade_title,subject_title,semester,unit_path,transcript_text,
                video_relative_path,video_duration,local_video_available,import_job_id)
                VALUES('synthetic-media','101','Synthetic exercise','Synthetic only','初中','初三','数学',
                '上册','Synthetic unit','Synthetic reference','lesson.mp4',600,1,'synthetic-job')''').lastrowid
            c.commit()
        body = {'curriculum_row_id': resource, 'max_minutes': 6, 'note': 'Isolated test only'}
    approved = client.post(path + '/approvals', headers=headers, json=body)
    assert approved.status_code == 200, approved.text


def setup(client, db, tmp_path, kind):
    sid, pid = advance_to_study(client, action_prefix='controlled-atomic')
    started = client.post(f'/v1/plan-items/{pid}/start', json={'client_action_id': 'controlled-original-start'})
    assert started.status_code == 200, started.text
    configure(client, db, tmp_path, kind)
    return sid, pid


def table(kind):
    return 'controlled_game_session' if kind == 'games' else 'controlled_content_session'


def counts(db, pid, kind):
    with db.connect() as c:
        return (c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='paused'", (pid,)).fetchone()[0],
                c.execute(f'SELECT COUNT(*) FROM {table(kind)}').fetchone()[0],
                c.execute('SELECT COUNT(*) FROM companion_activity_outcome').fetchone()[0])


BODY = {'query': 'Synthetic exercise', 'client_action_id': 'controlled-start-original'}


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('restart', [False, True])
def test_committed_start_lost_http_reply_replays_original_pause_without_relaunch(tmp_path, kind, restart):
    db = Database(tmp_path/'test.db')
    app = client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True).app
    wrapper = DropFirstSuccessfulReply(app, '')
    with TestClient(wrapper, raise_server_exceptions=False) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}/start'
        wrapper.path = path
        failed = client.post(path, json=BODY)
        assert wrapper.dropped and failed.status_code == 500, failed.text
        assert counts(db, pid, kind) == (1, 1, 1)
        if not restart:
            result = client.post(path, json=BODY)
            assert result.status_code == 200, result.text
            assert result.json()['paused_plan_item_id'] == pid
            assert result.json()['replayed'] is True
            assert result.json()['launch_allowed'] is False
            assert counts(db, pid, kind) == (1, 1, 1)
    if restart:
        with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
            result = client.post(path, json=BODY)
            assert result.status_code == 200, result.text
            assert result.json()['paused_plan_item_id'] == pid
            assert result.json()['status'] == 'interrupted'
            assert result.json()['replayed'] is True and result.json()['launch_allowed'] is False
            assert counts(db, pid, kind) == (1, 1, 1)
            assert task(db, pid)['status'] == 'paused'


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('failure', ['activity', 'outcome'])
def test_start_insert_failure_rolls_back_original_timer_and_all_events(tmp_path, kind, failure):
    db = Database(tmp_path/'test.db')
    app = client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True).app
    with TestClient(app, raise_server_exceptions=False) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        before = task(db, pid)
        target = table(kind) if failure == 'activity' else 'companion_activity_outcome'
        with db.connect() as c:
            c.execute(f"CREATE TRIGGER fail_start BEFORE INSERT ON {target} BEGIN SELECT RAISE(ABORT,'isolated write failure'); END")
            c.commit()
        failed = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start', json=BODY)
        assert failed.status_code >= 500, failed.text
        assert task(db, pid) == before
        assert counts(db, pid, kind) == (0, 0, 0)
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE client_action_id LIKE '%rollback-resume%'").fetchone()[0] == 0
            c.execute('DROP TRIGGER fail_start')
            c.commit()
        saved = client.post(f'/v1/sessions/{sid}/controlled-{kind}/start', json=BODY)
        assert saved.status_code == 200, saved.text
        assert saved.json()['paused_plan_item_id'] == pid
        assert counts(db, pid, kind) == (1, 1, 1)


@pytest.mark.parametrize('kind', ['content', 'games'])
def test_old_start_key_cannot_change_query_or_move_to_another_child(tmp_path, kind):
    db = Database(tmp_path/'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, kind)
        path = f'/v1/sessions/{sid}/controlled-{kind}/start'
        saved = client.post(path, json=BODY)
        assert saved.status_code == 200, saved.text
        changed = client.post(path, json={**BODY, 'query': 'Different request'})
        assert changed.status_code == 409, changed.text
        switched = client.post(f'/v1/sessions/{sid}/switch-child', json={
            'target_child_id': 'child-primary-4', 'client_action_id': 'controlled-switch-child'})
        assert switched.status_code == 200, switched.text
        other = client.post(f"/v1/sessions/{switched.json()['session_id']}/controlled-{kind}/start", json=BODY)
        assert other.status_code == 409, other.text
        assert counts(db, pid, kind) == (1, 1, 1)
        with db.connect() as c:
            assert c.execute(f"SELECT COUNT(*) FROM {table(kind)} WHERE child_id='child-primary-4'").fetchone()[0] == 0


def test_video_old_receipt_after_stop_does_not_pause_resumed_timer(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, 'content')
        path = f'/v1/sessions/{sid}/controlled-content/start'
        first = client.post(path, json=BODY)
        assert first.status_code == 200, first.text
        assert first.json()['launch_allowed'] is True
        stopped = client.post(f'/v1/sessions/{sid}/controlled-content/end',
                              json={'reason': 'child_requested', 'actual_seconds': 1,
                                    'content_session_id': first.json()['content_session_id']})
        assert stopped.status_code == 200, stopped.text
        resumed = client.post(f'/v1/plan-items/{pid}/resume', json={'client_action_id': 'new-learning-segment'})
        assert resumed.status_code == 200, resumed.text
        before = task(db, pid)
        # Revoking future playback cannot turn a historical read into a new launch.
        client.app.state.controlled_content.update_rule(CHILD, enabled=False, max_daily_minutes=20, max_item_minutes=8)
        replay = client.post(path, json=BODY)
        assert replay.status_code == 200, replay.text
        assert replay.json()['content_session_id'] == first.json()['content_session_id']
        assert replay.json()['status'] == 'stopped'
        assert replay.json()['replayed'] is True and replay.json()['launch_allowed'] is False
        assert task(db, pid) == before
        assert counts(db, pid, 'content') == (1, 1, 1)
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_video_legacy_pause_receipt_cannot_acknowledge_pausing_a_new_segment(tmp_path):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid = setup(client, db, tmp_path, 'content')
        # The old host committed this exact pause before trying media creation.
        paused = StudyRepository(db).pause_plan_item(pid, BODY['client_action_id'] + ':pause', 'controlled_content')
        assert paused['status'] == 'paused'
        resumed = client.post(f'/v1/plan-items/{pid}/resume', json={'client_action_id': 'legacy-compensation'})
        assert resumed.status_code == 200, resumed.text
        before = task(db, pid)
        result = client.post(f'/v1/sessions/{sid}/controlled-content/start', json=BODY)
        assert result.status_code == 409, result.text
        assert task(db, pid) == before
        assert counts(db, pid, 'content') == (1, 0, 0)
