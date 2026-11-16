"""Question publication does not grant model access or preserve stale modality."""
from test_document_open_check import opened, answer_open, CHILD
from test_document_revisit import say


def test_written_original_can_use_local_feedback_after_question_grant_is_revoked(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'written') as j:
        selected = say(j, monkeypatch, '换一道新题', 'independent_check', 'open-private-new')
        prompt = selected['execution']['result']['diagnostic_prompt']['prompt']
        token = j['client'].post('/v1/parent/unlock', json={'pin':'8264'}).json()['parent_token']
        path = f'/v1/parent/children/{CHILD}/writing-question-authorizations/{prompt["diagnostic_item_id"]}/revoke'
        revoked = j['client'].post(path, headers={'X-HSC-Parent':token}, json={
            'expected_revision':1,'client_action_id':'open-revoke-question'})
        assert revoked.status_code == 200, revoked.text
        saved, _ = answer_open(j, monkeypatch, prompt, 'written')
        follow = saved['local_learning_evaluation']
        assert follow['status'] == 'local_feedback_required', follow
        assert follow['local_feedback_turn_id']
        count = len(j['calls'])
        result = j['client'].post('/v1/agent-turns/'+follow['local_feedback_turn_id']+'/execute')
        assert result.status_code == 200, result.text
        assert result.json()['execution']['response_text']
        assert len(j['calls']) == count
        with j['db'].connect() as c:
            assert c.execute('SELECT count(*) FROM rubric_assessment').fetchone()[0] == 0
            assert c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchone()[0] == j['answer']


def test_withdrawn_open_source_does_not_keep_an_authoritative_input_mode(tmp_path, monkeypatch):
    with opened(tmp_path, monkeypatch, 'spoken_explanation') as j:
        selected = say(j, monkeypatch, '换一道口头解释题', 'independent_check', 'open-mode-new')
        prompt = selected['execution']['result']['diagnostic_prompt']['prompt']
        assert prompt['item']['response_mode'] == 'spoken_explanation'
        with j['db'].connect() as c:
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (prompt['diagnostic_item_id'],))
            c.commit()
        result = j['client'].get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt')
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'invalidated'
        assert result.json()['prompt']['item']['response_mode'] is None
