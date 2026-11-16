"""Paper answer -> feedback -> day closure -> the next day's actual task."""
import json
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResult
from test_diagnostic_prompt_http import environment, CHILD, OTHER
from test_cloud_first_agent import Planner
from test_paper_writing_runtime_http import synthetic_page
from test_voice_runtime_binding import capture, interpret
from test_writing_feedback_focus_http import PaperJourney, JourneyGateway, assert_specific_feedback
from test_writing_followup_paper_http import revised_page
from test_writing_stage_review_consumers import advance_clock


class NextDayGateway(JourneyGateway):
    async def complete(self, request):
        if self.control in {'task.start', 'task.complete'}:
            selector = {'subject': None, 'task_kind': 'writing', 'source_type': None,
                        'target_quote': self.quote}
            if request.request_type == 'agent_plan':
                self.requests.append(request)
                arguments = {'start_selector': selector} if self.control == 'task.start' else {}
                return await Planner(self.control, arguments, self.quote).complete(request)
            if request.request_type == 'voice_interaction':
                self.requests.append(request)
                payload = {'role': 'control', 'control': self.control,
                           'quote': self.quote, 'confidence': .99}
                if self.control == 'task.start':
                    payload['start_selector'] = selector
                return ModelResult(content=json.dumps(payload), provider='fixture', model='fixture', latency_ms=1)
        return await super().complete(request)


@pytest.fixture
def next_day_ready(environment, monkeypatch):
    settings, db = environment
    journey = PaperJourney(settings, db, monkeypatch)
    journey.gateway = NextDayGateway()
    with TestClient(create_app(settings, model_gateway=journey.gateway)) as client:
        journey.begin(client)
        _, evaluated = journey.submit(client, journey.prompt, 'original-day', *synthetic_page(monkeypatch))
        outcome = evaluated['execution']['result']['outcome_id']
        transition = client.post(f'/v1/sessions/{journey.sid}/transition', json={
            'expected_state': 'study', 'target_state': 'reflection', 'reason': 'Today is finished.',
            'client_action_id': 'writing-day-reflection'})
        assert transition.status_code == 200, transition.text
        closed = client.post(f'/v1/sessions/{journey.sid}/reflection/complete', json={
            'learned_text': '', 'difficult_text': '', 'client_action_id': 'writing-day-close'})
        assert closed.status_code == 200, closed.text
        with db.connect() as c:
            old_day = c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (journey.sid,)).fetchone()[0]
            row = c.execute("""SELECT * FROM agent_commitment WHERE child_id=?
                AND json_extract(payload_json,'$.source_outcome_id')=?""", (CHILD, outcome)).fetchone()
            assert row, 'Actual paper feedback has no persistent next-day teaching obligation.'
            commitment = dict(row)
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    # A real family cutoff remains enabled; choose the next day's 16:00 Shanghai,
    # not whatever night hour the machine happened to run this fixture.
    next_day = datetime.fromisoformat(commitment['due_at']) + timedelta(hours=16)
    clock = advance_clock(monkeypatch, next_day)
    class FamilyDate(date):
        @classmethod
        def today(cls):
            return clock.now(timezone(timedelta(hours=8))).date()
    from home_study_agent import repository
    monkeypatch.setattr(repository,'date',FamilyDate)
    from home_study_agent import learning_input_replan
    monkeypatch.setattr(learning_input_replan, 'utc_now', lambda: clock.now(timezone.utc))
    journey.gateway.control, journey.gateway.quote = 'task.start', '开始今天的写作复习'
    with ExitStack() as stack:
        client=stack.enter_context(TestClient(create_app(settings, model_gateway=journey.gateway),raise_server_exceptions=False))
        unlocked=client.post('/v1/parent/unlock',json={'pin':'8264'})
        assert unlocked.status_code==200,unlocked.text
        journey.headers={'X-HSC-Parent':unlocked.json()['parent_token']}
        created = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'writing-next-day'})
        assert created.status_code == 201, created.text
        sid = created.json()['session_id']
        with db.connect() as c:
            c.execute("""UPDATE daily_session SET state='homework_capture',
                homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?""", (sid,))
            c.execute("UPDATE family_rule SET latest_end_time='20:30',pet_min_minutes=0,competition_minutes=0 WHERE child_id=?", (CHILD,))
            c.commit()
        plan = client.post(f'/v1/sessions/{sid}/plan/generate', json={'client_action_id': 'writing-next-day-plan'})
        assert plan.status_code == 200, plan.text
        task = next(p for p in plan.json()['plan_items'] if p['source_type'] == 'agent_retention'
            and p['source_ref'] == commitment['commitment_id'])
        accepted = client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'plan_review',
            'target_state': 'study', 'reason': 'Accept today plan.', 'client_action_id': 'accept-next-day-writing'})
        assert accepted.status_code == 200, accepted.text
        heard = capture(client, settings, monkeypatch, sid, journey.gateway.quote, key='next-writing-voice')
        turn = interpret(client, sid, journey.gateway.quote, 'next-writing-command', heard)
        request=next(r for r in reversed(journey.gateway.requests) if r.request_type=='agent_plan')
        transmitted=json.dumps({'messages':request.messages,'context':request.context_manifest},ensure_ascii=False)
        for local_value in ('daily_writing_arrival','daily_writing_binding',commitment['commitment_id'],
                            journey.prompt['prompt_id'],journey.prompt['item']['prompt']):
            assert local_value not in transmitted
        yield SimpleNamespace(journey=journey,db=db,settings=settings,client=client,sid=sid,task=task,
            commitment=commitment,turn=turn,clock=clock,stack=stack)


def test_paper_feedback_next_day_task_prepares_formal_writing_not_free_text(next_day_ready, monkeypatch):
    case=next_day_ready
    journey,db,settings,client,sid,task,commitment,turn=(getattr(case,key) for key in
        ('journey','db','settings','client','sid','task','commitment','turn'))
    execution = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert execution.status_code == 200, execution.text
    result = execution.json()['execution']['result']
    assert result['plan_item_id'] == task['plan_item_id'], result
    prompt = (result.get('diagnostic_prompt') or {}).get('prompt')
    assert prompt, 'Next-day task only starts a timer/free-text prompt instead of the original writing follow-up.'
    assert prompt['origin_session_id'] == sid and prompt['child_id'] == CHILD
    assert prompt['prompt_id'] != journey.prompt['prompt_id']
    assert prompt['item']['answer_type'] == 'writing'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT status FROM agent_commitment WHERE commitment_id=?',
            (commitment['commitment_id'],)).fetchone()[0] != 'completed'
    journey.sid = sid
    monitor = client.post(f'/v1/sessions/{sid}/document-monitor/start',
        json={'client_action_id':'next-writing-monitor'})
    assert monitor.status_code==200,monitor.text
    second_turn,second = journey.submit(client,prompt,'next-manuscript',*revised_page(monkeypatch,'revision'))
    assert_specific_feedback(second)
    assert len(journey.gateway.evaluations)==2
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==2
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0]==2
        assert c.execute('SELECT COUNT(*) FROM writing_followup').fetchone()[0]==1
    case.stack.close()
    with TestClient(create_app(settings,model_gateway=journey.gateway)) as client:
        assert_specific_feedback(journey.recovered(client,second_turn))
        replay=client.post(f'/v1/agent-turns/{second_turn}/execute')
        assert replay.status_code==200,replay.text
        assert len(journey.gateway.evaluations)==2


@pytest.mark.parametrize('recovery_change',['unchanged','cancel','other_error'])
def test_daily_prompt_and_timer_share_one_write_transaction(next_day_ready,recovery_change):
    case=next_day_ready
    with case.db.connect() as c:
        c.execute("""CREATE TRIGGER fail_daily_timer BEFORE UPDATE OF status ON plan_item
            WHEN NEW.source_type='agent_retention' AND NEW.status='active'
            BEGIN SELECT RAISE(ABORT,'synthetic daily timer storage failure'); END""")
        c.commit()
    failed=case.client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
    assert failed.status_code==500,failed.text
    with case.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(case.sid,)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM writing_followup WHERE session_id=?',(case.sid,)).fetchone()[0]==0
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case.task['plan_item_id'],)).fetchone()[0]=='pending'
        c.execute('DROP TRIGGER fail_daily_timer')
        if recovery_change=='other_error':
            c.execute("UPDATE agent_step SET error_code='UntrustedError' WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)",
                (case.turn['agent_turn_id'],))
            c.execute("UPDATE agent_turn SET tool_error_code='UntrustedError' WHERE agent_turn_id=?",(case.turn['agent_turn_id'],))
        c.commit()
    if recovery_change=='cancel':
        stopped=case.client.post(f'/v1/sessions/{case.sid}/agent-turn',json={
            'transcript':'别讲了','client_action_id':'cancel-failed-start','cloud_text_allowed':False})
        assert stopped.status_code==200,stopped.text
    case.stack.close()
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        retry=client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
        if recovery_change!='unchanged':
            assert retry.status_code==409,retry.text
            with case.db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(case.sid,)).fetchone()[0]==0
                assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?',(case.task['plan_item_id'],)).fetchone()[0]=='pending'
            return
        assert retry.status_code==200,retry.text
        assert retry.json()['execution']['result']['diagnostic_prompt']['status']=='waiting_answer'
        repeated=client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
        assert repeated.status_code==200,repeated.text
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(case.sid,)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type=\'started\'',
                (case.task['plan_item_id'],)).fetchone()[0]==1


@pytest.mark.parametrize('fault',['cancel','child','plan_version','source','budget','privacy'])
def test_new_day_actual_voice_cannot_start_changed_work(next_day_ready,fault):
    case=next_day_ready
    if fault=='cancel':
        result=case.client.post(f'/v1/sessions/{case.sid}/agent-turn',json={
            'transcript':'别讲了','client_action_id':'cancel-daily-start','cloud_text_allowed':False})
        assert result.status_code==200,result.text
    elif fault=='child':
        result=case.client.post(f'/v1/sessions/{case.sid}/switch-child',json={
            'target_child_id':OTHER,'client_action_id':'switch-daily-start'})
        assert result.status_code==200,result.text
    elif fault=='privacy':
        result=case.client.put('/v1/parent/privacy',headers=case.journey.headers,json={'cloud_model_text_allowed':False})
        assert result.status_code==200,result.text
    else:
        with case.db.connect() as c:
            if fault=='plan_version':
                c.execute('UPDATE daily_plan SET version=version+1 WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)',
                    (case.task['plan_item_id'],))
            elif fault=='source':
                c.execute("UPDATE diagnostic_item SET prompt=prompt||' Revised source' WHERE diagnostic_item_id=?",
                    (case.journey.prompt['diagnostic_item_id'],))
            else:
                c.execute('UPDATE family_rule SET max_learning_minutes=1 WHERE child_id=?',(CHILD,))
            c.commit()
    refused=case.client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
    assert refused.status_code==409,refused.text
    with case.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(case.sid,)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type=\'started\'',
            (case.task['plan_item_id'],)).fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
    assert len(case.journey.gateway.evaluations)==1


def test_new_day_cancel_after_prepare_cannot_score_or_cancel_parent_goal(next_day_ready,monkeypatch):
    case=next_day_ready
    started=case.client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
    assert started.status_code==200,started.text
    prompt=started.json()['execution']['result']['diagnostic_prompt']['prompt']
    voice=capture(case.client,case.settings,monkeypatch,case.sid,'别讲了',prompt,key='cancel-new-prompt-voice')
    stopped=case.client.post(f'/v1/sessions/{case.sid}/agent-turn',json={
        'transcript':'别讲了','client_action_id':'cancel-new-prompt','cloud_text_allowed':False,'voice_input_id':voice})
    assert stopped.status_code==200,stopped.text
    case.stack.close()
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        read=client.get(f'/v1/sessions/{case.sid}/diagnostic-prompt',params={'prompt_id':prompt['prompt_id']})
        assert read.status_code==200,read.text
        assert read.json()['status']=='invalidated',read.text
        late=client.post(f'/v1/sessions/{case.sid}/diagnostic-prompt/answer',json={
            'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
            'answer':'Here is a new answer.','client_action_id':'cancelled-daily-answer'})
        assert late.status_code==200,late.text
        assert late.json()['status']=='invalidated' and late.json()['attempt'] is None,late.text
        old=client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
        assert old.status_code==409,old.text
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
            assert c.execute('SELECT status FROM agent_objective WHERE objective_id=?',
                (case.commitment['objective_id'],)).fetchone()[0]=='paused'


def test_daily_saved_start_survives_failed_final_receipt_and_restart(next_day_ready):
    case=next_day_ready
    with case.db.connect() as c:
        c.execute("""CREATE TRIGGER fail_daily_receipt BEFORE UPDATE OF status ON agent_step
            WHEN NEW.capability_name='task.start' AND NEW.status='completed'
            BEGIN SELECT RAISE(ABORT,'synthetic daily receipt failure'); END""")
        c.commit()
    failed=case.client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
    assert failed.status_code==500,failed.text
    with case.db.connect() as c:
        original=dict(c.execute('SELECT * FROM diagnostic_prompt WHERE origin_session_id=?',(case.sid,)).fetchone())
        assert original['status']=='waiting_answer'
        c.execute('DROP TRIGGER fail_daily_receipt')
        c.commit()
    case.stack.close()
    with TestClient(create_app(case.settings,model_gateway=case.journey.gateway)) as client:
        retry=client.post(f"/v1/agent-turns/{case.turn['agent_turn_id']}/execute")
        assert retry.status_code==200,retry.text
        recovered=retry.json()['execution']
        assert recovered['result']['diagnostic_prompt']['prompt']['prompt_id']==original['prompt_id']
        assert recovered['result']['diagnostic_prompt']['status']=='waiting_answer'
        assert recovered.get('recorded_receipt') is True
        assert recovered.get('replay_audio_allowed') is False
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(case.sid,)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type=\'started\'',
                (case.task['plan_item_id'],)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
