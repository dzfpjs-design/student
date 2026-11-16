"""An explicit legacy fixture, never a repair of real household history."""
import json
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment
from test_writing_persistent_focus_http import ConversationGateway
from test_writing_feedback_focus_http import PaperJourney, assert_specific_feedback
from test_paper_writing_runtime_http import synthetic_page


@pytest.mark.parametrize('mode', ['feedback', 'revision', 'guards'])
def test_legacy_neutral_plan_does_not_destroy_original_teaching(environment, monkeypatch, mode):
    settings, db = environment
    journey = PaperJourney(settings, db, monkeypatch)
    journey.gateway = ConversationGateway()
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        journey.begin(client)
        first_id, first = journey.submit(client, journey.prompt, 'original', *synthetic_page(monkeypatch))
        assert_specific_feedback(first)
        chat = journey.say(client, '我今天上体育课很开心', 'legacy-neutral-chat', 'conversation.respond', None)
        done = client.post(f"/v1/agent-turns/{chat['agent_turn_id']}/execute")
        assert done.status_code == 200, done.text
        with db.connect() as c:
            original_goal = c.execute('SELECT objective_id FROM agent_plan WHERE agent_turn_id=?', (first_id,)).fetchone()[0]
            # Reproduce the old writer's erroneous association, not new runtime behavior.
            c.execute('UPDATE agent_plan SET objective_id=? WHERE agent_turn_id=?', (original_goal, chat['agent_turn_id']))
            c.execute("UPDATE agent_turn SET action_json=json_set(action_json,'$.agent_runtime.objective_id',?) WHERE agent_turn_id=?", (original_goal, chat['agent_turn_id']))
            c.commit()
            legacy = dict(c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?', (chat['agent_turn_id'],)).fetchone())
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        if mode == 'feedback':
            assert_specific_feedback(journey.recovered(client, first_id))
        elif mode == 'revision':
            chosen = journey.say(client, '我想按刚才的反馈自己改一遍', 'resume-original', 'writing.revise_original', journey.prompt)
            assert chosen.get('runtime_capability') == 'writing.revise_original', chosen
            packet = chosen['action']['writing_followup_binding']['action']
            assert packet['parent_prompt_id'] == journey.prompt['prompt_id']
            assert packet['assessment_id'] == first['execution']['result']['rubric_assessment']['assessment_id']
        else:
            from home_study_agent.formative_evaluation_projection import build_formative_projection
            with db.connect() as c:
                c.execute('BEGIN')
                record = json.loads(c.execute('''SELECT e.record_json FROM learning_evaluation_execution e
                    JOIN agent_step s USING(step_id) JOIN agent_plan p USING(plan_id)
                    WHERE p.agent_turn_id=?''', (first_id,)).fetchone()[0])
                audit = record['formative_projection']['local_audit']
                scope = {key:audit[key] for key in ('child_id','session_id','prompt_id','item_version','objective_id')}
                baseline = build_formative_projection(c, **scope)
                assert baseline['status'] == 'ready_for_runtime_checks', baseline
                assert baseline['payload']['response'] == record['child_response']
                assert baseline['payload']['question'] == record['question_asked']
                assert '体育课' not in json.dumps(baseline['payload'], ensure_ascii=False)
                original_action = json.loads(c.execute('SELECT action_json FROM agent_turn WHERE agent_turn_id=?', (chat['agent_turn_id'],)).fetchone()[0])
                for fault in ('transport','source','reference_action','reference_manifest','private_manifest',
                              'foreign_voice','changed_response','unfinished_step','unfinished_turn'):
                    c.execute('SAVEPOINT broken_neutral')
                    action = json.loads(json.dumps(original_action))
                    if fault == 'transport': action['agent_runtime']['step_id'] = 'different-step'
                    elif fault == 'source': action['source'] = 'writing_local_reference'
                    elif fault == 'reference_action': action['writing_reference'] = {}
                    elif fault == 'reference_manifest':
                        c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.writing_reference',json('{}')) WHERE agent_turn_id=?", (chat['agent_turn_id'],))
                    elif fault == 'private_manifest':
                        c.execute("UPDATE agent_plan SET context_manifest_json=json_set(context_manifest_json,'$.knowledge_context',json('{\"local_only\":true}')) WHERE agent_turn_id=?", (chat['agent_turn_id'],))
                    elif fault == 'foreign_voice': action['voice_input_binding']['child_id'] = 'child-junior-3'
                    elif fault == 'changed_response':
                        c.execute("UPDATE agent_turn SET response_text='different reply' WHERE agent_turn_id=?", (chat['agent_turn_id'],))
                    elif fault == 'unfinished_step':
                        c.execute("UPDATE agent_step SET status='planned' WHERE plan_id=?", (legacy['plan_id'],))
                    elif fault == 'unfinished_turn':
                        c.execute("UPDATE agent_turn SET tool_status='pending' WHERE agent_turn_id=?", (chat['agent_turn_id'],))
                    c.execute('UPDATE agent_turn SET action_json=? WHERE agent_turn_id=?', (json.dumps(action),chat['agent_turn_id']))
                    bad = build_formative_projection(c, **scope)
                    assert bad['status']=='local_required' and bad['payload'] is None, (fault,bad)
                    c.execute('ROLLBACK TO broken_neutral')
                    c.execute('RELEASE broken_neutral')
                assert build_formative_projection(c, **scope) == baseline
                c.rollback()
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM agent_plan WHERE agent_turn_id=?', (chat['agent_turn_id'],)).fetchone()) == legacy
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
