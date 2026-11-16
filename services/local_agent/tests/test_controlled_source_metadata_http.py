"""Corrupt optional catalog metadata cannot block unrelated actual voice HTTP."""
import json
import base64
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.controlled_voice import snapshot
from home_study_agent.local_media_ai import LocalMediaAI
from test_app import client_for
from test_learning_runtime_http import NoCloud
from test_controlled_cloud_semantics_http import prepare, provider, Semantic
from test_controlled_safe_stop_http import wav_file
from test_controlled_start_atomic_http import CHILD


TABLES = {'content': 'controlled_content_approval', 'games': 'controlled_game_approval',
          'corpus': 'curriculum_corpus'}


def capture_question(client, tmp_path, monkeypatch, sid, text, prompt, key):
    monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k: (text, 1000, 'synthetic-local', False))
    audio = tmp_path / 'source-question.wav'
    wav_file(audio)
    response = client.post(f'/v1/sessions/{sid}/asr', json={
        'wav_base64': base64.b64encode(audio.read_bytes()).decode(), 'purpose': 'agent_command',
        'client_action_id': key, 'capture_reference': {'child_id': CHILD,
            'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}}})
    assert response.status_code == 200, response.text
    return response.json()['voice_input_id']


@pytest.mark.parametrize('kind', ['content', 'games', 'corpus'])
@pytest.mark.parametrize('bad_date', ['not-an-iso-date', b'not-a-text-date'])
def test_actual_nonmedia_voice_is_not_blocked_by_corrupt_optional_source_date(tmp_path, monkeypatch, kind, bad_date):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, _, _, _ = prepare(client, db, tmp_path, 'content' if kind == 'corpus' else kind)
        prepared = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare', json={
            'track': 'school', 'client_action_id': 'original-source-question'})
        assert prepared.status_code == 200, prepared.text
        prompt = prepared.json()['prompt']
        with db.connect() as c:
            c.execute(f'UPDATE {TABLES[kind]} SET updated_at=?', (bad_date,))
            c.commit()
        text = '告诉我此刻哪些采集设备还在运行'
        voice = capture_question(client, tmp_path, monkeypatch, sid, text, prompt, 'source-metadata-audio')
        gateway = Semantic('device.status', text)
        provider(client, gateway)
        # The outer context owns the real app lifespan; suppressing exception
        # rethrow here records the same HTTP 500 a native client would receive.
        http = TestClient(client.app, raise_server_exceptions=False)
        try:
            response = http.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': text, 'voice_input_id': voice, 'client_action_id': 'unrelated-query',
                'cloud_text_allowed': True})
        finally:
            http.close()
        assert response.status_code == 200, response.text
        assert response.json()['action']['type'] == 'query_device_status', response.json()
        assert len(gateway.requests) == 1
        with db.connect() as c:
            assert c.execute(f'SELECT updated_at FROM {TABLES[kind]}').fetchone()[0] == bad_date
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            scope = snapshot(c, CHILD, sid)
            target = scope['games' if kind == 'games' else 'content']['sources']
            assert len(target) == 1 and target[0]['unavailable'] is True
            assert target[0]['error_code'] == 'CONTROL_SOURCE_DATE_INVALID'
        wire = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        assert 'not-an-iso-date' not in wire and str(tmp_path) not in wire
        assert 'CONTROL_SOURCE_DATE_INVALID' not in wire
        assert 'approval_id' not in wire and 'unavailable' not in wire


@pytest.mark.parametrize('kind', ['content', 'games'])
@pytest.mark.parametrize('change_selected', [False, True])
def test_healthy_launch_and_exact_late_corruption_are_isolated(tmp_path, kind, change_selected):
    db = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=NoCloud(), agent_runtime_enabled=True) as client:
        sid, pid, path, headers = prepare(client, db, tmp_path, kind)
        bad_asset = tmp_path / 'unrelated.exe'
        bad_asset.write_bytes(b'inert synthetic file; never launched')
        saved = client.post(f'/v1/parent/children/{CHILD}/controlled-games/approvals',
            headers=headers, json={'title': 'Unrelated synthetic resource',
                'executable_path': str(bad_asset), 'max_minutes': 6})
        assert saved.status_code == 200, saved.text
        with db.connect() as c:
            c.execute('UPDATE controlled_game_approval SET updated_at=? WHERE executable_path=?',
                      ('bad-unrelated-date', str(bad_asset)))
            c.commit()
            sources = snapshot(c, CHILD, sid)
            assert sum(s.get('error_code') == 'CONTROL_SOURCE_DATE_INVALID'
                       for s in sources['games']['sources']) == 1
            assert any(not s.get('unavailable') for s in sources[kind]['sources'])

        control = 'controlled_content.play' if kind == 'content' else 'controlled_game.play'
        quote = '我想看看图形练习这段视频' if kind == 'content' else '我想玩玩图形练习这个游戏'
        gateway = Semantic(control, quote, '图形练习')
        provider(client, gateway)
        heard = client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': quote, 'client_action_id': 'healthy-source-start', 'cloud_text_allowed': True})
        assert heard.status_code == 200, heard.text
        turn = heard.json()
        assert turn['action']['type'] == ('start_controlled_content' if kind == 'content' else 'start_controlled_game'), turn
        admitted = client.post(f"/v1/sessions/{sid}/agent-turns/{turn['agent_turn_id']}/control-admission")
        assert admitted.status_code == 200, admitted.text
        if change_selected:
            with db.connect() as c:
                c.execute(f'UPDATE {TABLES[kind]} SET updated_at=? WHERE updated_at<>?',
                          ('bad-selected-date', 'bad-unrelated-date'))
                c.commit()
        payload = {'query': turn['action']['query'], 'client_action_id': turn['agent_turn_id'],
                   'selection_binding': turn['action']['selection_binding']}
        started = client.post(path + '/start', json=payload)
        assert started.status_code == (409 if change_selected else 200), started.text
        if not change_selected:
            assert started.json()['launch_allowed'] is True
            replay = client.post(path + '/start', json=payload)
            assert replay.status_code == 200 and replay.json()['launch_allowed'] is False
        table = 'controlled_content_session' if kind == 'content' else 'controlled_game_session'
        with db.connect() as c:
            assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == (1 if change_selected else 2)
            assert c.execute('SELECT updated_at FROM controlled_game_approval WHERE executable_path=?',
                             (str(bad_asset),)).fetchone()[0] == 'bad-unrelated-date'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            # A real SQLite authorization fault must not become source-unavailable.
            c.set_authorizer(lambda op, table, *args: sqlite3.SQLITE_DENY
                             if op == sqlite3.SQLITE_READ and table == 'controlled_game_approval'
                             else sqlite3.SQLITE_OK)
            try:
                with pytest.raises(sqlite3.DatabaseError):
                    snapshot(c, CHILD, sid)
            finally:
                c.set_authorizer(None)
        assert len(gateway.requests) == 1
        wire = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
        for private in (str(tmp_path), 'bad-unrelated-date', 'CONTROL_SOURCE_DATE_INVALID', 'approval_id'):
            assert private not in wire
