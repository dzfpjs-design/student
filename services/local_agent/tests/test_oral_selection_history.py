"""Real answered school prompt, local selection, no paid model or family data."""
import json

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from test_open_response_spoken_runtime_http import (
    test_natural_spoken_process_asr_bound_answer_formative_and_restart as run_journey,
)


@pytest.mark.parametrize('history', ['clean', 'shown', 'broken_json', 'scalar', 'missing_prompt',
    'seal_damaged', 'source_deleted', 'legacy', 'other_child', 'withdrawn_unrelated'])
def test_independent_selection_uses_frozen_history_before_expensive_exposure(tmp_path, monkeypatch, history):
    def inspect(j):
        reply = j['client'].post('/v1/agent-turns/' + j['follow']['feedback_turn_id'] + '/execute')
        assert reply.status_code == 200, reply.text
        prompt = j['prompt']
        with j['db'].connect() as c:
            c.execute('BEGIN')
            ready = AgentRuntimeStore.resolve_oral_followup(c, child_id=j['child'], session_id=j['sid'],
                prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], kind='independent_check')
            assert ready['status'] == 'ready', ready
            other_id = ready['action']['diagnostic_item_id']
            if history != 'clean':
                from home_study_agent.diagnostic_prompt import DiagnosticPromptService, _ITEM_FIELDS, _version
                from home_study_agent.diagnostic_history_identity import snapshot
                other = DiagnosticPromptService._item(c, j['child'], other_id)
                frozen = json.dumps({key: other[key] for key in _ITEM_FIELDS}, ensure_ascii=False)
                damaged = {'broken_json': '{', 'scalar': 'null', 'missing_prompt': '{}'}
                values = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',
                    (prompt['prompt_id'],)).fetchone())
                # Earlier displayed prompt, not the current answer focus. Preserve
                # the frozen material even when its catalog reference is old.
                values.update(prompt_id='synthetic-earlier-prompt', diagnostic_item_id=other_id,
                    item_json=damaged.get(history, frozen), status='cancelled', attempt_id=None,
                    prepare_action_id='synthetic-earlier-prepare', answer_action_id=None,
                    item_version=_version(other), identity_snapshot_json=snapshot(c,
                        child_id=j['child'], session_id=j['sid'], prompt_id='synthetic-earlier-prompt', item=other))
                if history == 'seal_damaged':
                    values['identity_snapshot_json'] = '{}'
                elif history == 'source_deleted':
                    values.update(identity_snapshot_json=None, item_json='{}', status='invalidated', reason='source_deleted')
                elif history == 'legacy':
                    values['identity_snapshot_json'] = None
                elif history == 'other_child':
                    # Earlier sibling session; corrupt sibling history must not affect
                    # this child's new-question admission or leak its contents.
                    other_session = 'synthetic-sibling-session'
                    c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,
                        last_checkpoint_at,client_action_id) VALUES (?,?,'completed','completed',?,?,?)''',
                        (other_session,'child-primary-4',values['created_at'],values['created_at'],'synthetic-sibling'))
                    values.update(child_id='child-primary-4', origin_session_id=other_session,
                        identity_snapshot_json='{}', item_json='{')
                elif history == 'withdrawn_unrelated':
                    unrelated = DiagnosticPromptService._item(c, j['child'], 'diag-authored-oral-v1-j3-chinese-action')
                    values.update(diagnostic_item_id=unrelated['diagnostic_item_id'], item_version=_version(unrelated),
                        item_json=json.dumps({key: unrelated[key] for key in _ITEM_FIELDS}, ensure_ascii=False),
                        identity_snapshot_json=snapshot(c, child_id=j['child'], session_id=j['sid'],
                            prompt_id='synthetic-earlier-prompt', item=unrelated))
                    c.execute("UPDATE diagnostic_item SET prompt='Later withdrawn text',status='retired' WHERE diagnostic_item_id=?",
                        (unrelated['diagnostic_item_id'],))
                columns = list(values)
                c.execute('INSERT INTO diagnostic_prompt(rowid,' + ','.join(columns) + ') VALUES (' +
                    ','.join('?' for _ in range(len(columns) + 1)) + ')', [-1, *values.values()])
                c.commit()
        import home_study_agent.learning_obligations as obligations
        original = obligations.project_item_exposure
        checked = []
        def measured(connection, child_id, item_id, **kwargs):
            checked.append(item_id)
            return original(connection, child_id, item_id, **kwargs)
        monkeypatch.setattr(obligations, 'project_item_exposure', measured)
        calls = len(j['posts'])
        with j['db'].connect() as c:
            c.execute('BEGIN')
            before = c.total_changes
            selected = AgentRuntimeStore.resolve_oral_followup(c, child_id=j['child'], session_id=j['sid'],
                prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], kind='independent_check')
            assert c.total_changes == before
            if history in {'clean', 'other_child', 'withdrawn_unrelated'}:
                assert selected['status'] == 'ready', selected
                assert selected['action']['diagnostic_item_id'] == other_id
                assert checked == [other_id], 'Already known original should not rescan all exposure evidence.'
            else:
                assert selected['status'] == 'blocked', selected
                assert selected['reason'] == ('oral_exposure_history_unavailable'
                    if history in {'seal_damaged', 'source_deleted'} else 'oral_no_unexposed_source')
                assert not checked, 'Displayed or unverifiable history cannot qualify an independent new question.'
            retried = AgentRuntimeStore.resolve_oral_followup(c, child_id=j['child'], session_id=j['sid'],
                prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], kind='retry')
            assert retried['status'] == 'ready' and retried['action']['required_hint_level'] == 1
            assert not retried['action']['independent_check']
            assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',
                (j['attempt']['attempt_id'],)).fetchone()[0] == j['quote']
        assert len(j['posts']) == calls
        if history in {'seal_damaged', 'source_deleted'}:
            from test_voice_runtime_binding import capture, interpret
            j['command_state'].update(control='oral.independent_check', quote='换一道没做过的题，我自己试试')
            voice = capture(j['client'], j['settings'], monkeypatch, j['sid'],
                j['command_state']['quote'], prompt, 'damaged-history-request', child=j['child'])
            response = interpret(j['client'], j['sid'], j['command_state']['quote'],
                'damaged-history-turn', voice)
            assert response.get('runtime_capability') != 'oral.independent_check', response
            assert '核对不了之前看过哪些题' in response['response_text'], response
            assert '没有符合要求的新题' not in response['response_text']
            assert response['action']['type'] == 'none'
            with j['db'].connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',
                    (j['child'],)).fetchone()[0] == 1
    run_journey(tmp_path, monkeypatch, 'authored:数学', None, True, None, after_saved=inspect)
