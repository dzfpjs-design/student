"""Observed paper is retained while actual school/PET/math work continues."""
import base64

import cv2
import numpy as np

from home_study_agent.local_ocr.service import RapidOcrEngine
from home_study_agent.local_media_ai import LocalMediaAI
from test_pet_media_evidence import wav_file
from test_primary_mixed_daily_budget import mixed_day, flow, CHILD
from test_primary_mixed_voice_journey import test_three_lanes_keep_actual_tasks_answers_and_recovery as run_journey
from test_voice_runtime_binding import interpret

QUESTION = '1. 计算：(24 + 6) × 3 = ?'


def test_paper_wrong_question_does_not_steal_other_tasks_or_disappear(mixed_day, monkeypatch):
    saved = {}

    def observe(case, school):
        def recognize(_engine, image, *args, **kwargs):
            upright = float(image[35:115, 35:115].mean()) < 210
            confidence = .96 if upright else .34
            return [
                {'text': '四年级数学 四则运算', 'confidence': confidence,
                 'box': [[80, 80], [500, 80], [500, 120], [80, 120]], 'kind': 'printed'},
                {'text': QUESTION, 'confidence': confidence,
                 'box': [[80, 210], [560, 210], [560, 260], [80, 260]], 'kind': 'printed'},
            ]
        monkeypatch.setattr(RapidOcrEngine, 'recognize', recognize)
        monkeypatch.setattr(RapidOcrEngine, 'recognize_page_orientation', recognize)
        page = np.full((420, 640, 3), 245, dtype=np.uint8)
        cv2.rectangle(page, (55, 55), (95, 95), (0, 0, 0), -1)
        cv2.putText(page, '1. (24 + 6) x 3 = ?', (80, 245), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
        cv2.line(page, (500, 195), (575, 275), (0, 0, 230), 8)
        cv2.line(page, (575, 195), (500, 275), (0, 0, 230), 8)
        encoded = cv2.imencode('.png', cv2.rotate(page, cv2.ROTATE_180))[1].tobytes()
        post, client, sid = case['post'], case['client'], case['sid']
        post(f'/v1/sessions/{sid}/document-monitor/start', dict(client_action_id='school-paper-monitor'))
        artifact = post(f'/v1/sessions/{sid}/document-monitor/ocr', dict(
            image_base64=base64.b64encode(encoded).decode(), client_action_id='school-paper-ocr'))
        pipeline = artifact['details']['local_pipeline']
        assert pipeline['quality']['transforms']['orientation_degrees'] == 180
        assert pipeline['wrong_question_candidates'][0]['text'] == QUESTION
        assert pipeline['proven_wrong_questions'] == []
        revision = post(f'/v1/sessions/{sid}/document-monitor/revisions', dict(
            artifact_id=artifact['artifact_id'], content_signature='synthetic-school-paper', client_action_id='school-paper-revision'))
        assert revision['marked_question_count'] == 1
        with case['db'].connect() as c:
            assert c.execute('SELECT COUNT(*) FROM mistake_case').fetchone()[0] == 0
        opened = post(f'/v1/sessions/{sid}/mistake-dialogue', dict(
            artifact_id=artifact['artifact_id'], client_action_id='school-paper-open'))
        assert opened['state']['question_text'] == QUESTION
        for index, text in enumerate(('第一题', '对', '我当时写了72', '是的', '以后再做')):
            turn = case['say'](text, f'school-paper-{index}', 'mistake.dialogue')
            assert turn['action']['type'] == 'continue_mistake_dialogue', turn['action']
            post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
        focus = client.get(f'/v1/sessions/{sid}/mistake-dialogue').json()['dialogue']
        assert focus['phase'] == 'paused' and focus['mistake_id'], focus
        assert focus['state']['submitted_answer'] == '72'
        saved.update(artifact=artifact['artifact_id'], mistake=focus['mistake_id'], school=school['plan_item_id'])
        with case['db'].connect() as c:
            assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (school['plan_item_id'],)).fetchone()[0] == 'active'
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0

    def revisit(case):
        # The real planner prioritizes the deferred paper ahead of the math
        # follow-up. Follow that order instead of deleting or skipping it.
        first = case['first']
        assert first['source_type'] == 'mistake_review' and first['source_ref'] == saved['mistake']
        offered = case['say']('开始今天的复习', 'paper-followup-ambiguous', 'task.start')
        assert offered['action']['source'] == 'task_navigation_clarification'
        case['post'](f'/v1/agent-turns/{offered["agent_turn_id"]}/execute')
        with case['db'].connect() as c:
            assert not c.execute('''SELECT 1 FROM plan_item pi JOIN daily_plan dp USING(plan_id)
                WHERE dp.session_id=? AND pi.status='active' ''', (case['next_sid'],)).fetchone()
        narrowed = case['say']('开始数学错题复习', 'paper-followup-narrow', 'task.start')
        assert narrowed['action']['source'] == 'task_navigation_clarification'
        case['post'](f'/v1/agent-turns/{narrowed["agent_turn_id"]}/execute')
        reference = case['client'].get(
            f"/v1/sessions/{case['next_sid']}/task-start-choice"
        ).json()['reference']
        assert reference
        monkeypatch.setattr(
            LocalMediaAI, '_recognize_wav', lambda *a, **k: ('第一项', 1000, 'synthetic', False)
        )
        audio = case['settings'].data_dir / 'synthetic-paper-choice.wav'
        wav_file(audio)
        captured = case['post'](f"/v1/sessions/{case['next_sid']}/asr", dict(
            wav_base64=base64.b64encode(audio.read_bytes()).decode(),
            purpose='agent_command', client_action_id='paper-followup-choice-audio',
            capture_reference={'child_id': CHILD, 'task_choice_reference': reference},
        ))
        selected = interpret(case['client'], case['next_sid'], '第一项',
                             'paper-followup-start', captured['voice_input_id'])
        assert selected['runtime_capability'] == 'task.start', selected
        started = case['post'](
            f"/v1/agent-turns/{selected['agent_turn_id']}/execute"
        )['execution']['result']
        assert started['status'] == 'active' and started['plan_item_id'] == first['plan_item_id']
        focus = case['client'].get(f"/v1/sessions/{case['next_sid']}/mistake-dialogue").json()['dialogue']
        assert focus and focus['mistake_id'] == saved['mistake']
        assert focus['state']['submitted_answer'] == '72'
        assert focus['phase'] == 'diagnosis'
        assert started['mistake_dialogue']['mistake_id'] == saved['mistake']
        with case['db'].connect() as c:
            assert c.execute('SELECT submitted_answer FROM mistake_case WHERE mistake_id=?',
                             (saved['mistake'],)).fetchone()[0] == '72'
            for item in case['next_plan']['plan_items']:
                if item['plan_item_id'] != first['plan_item_id']:
                    row = c.execute('SELECT status,started_at FROM plan_item WHERE plan_item_id=?',
                                    (item['plan_item_id'],)).fetchone()
                    assert tuple(row) == ('pending', None)

    run_journey(mixed_day, monkeypatch, restart_midway=True, review_correct=True,
                paper_hook=observe, followup_hook=revisit)
    with mixed_day['db'].connect() as c:
        mistake = c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (saved['mistake'],)).fetchone()
        assert mistake and mistake['child_id'] == CHILD and mistake['status'] != 'excluded'
        cases = c.execute('SELECT mistake_id,submitted_answer,diagnostic_item_id FROM mistake_case').fetchall()
        assert len(cases) == 2
        assert [(r['mistake_id'], r['submitted_answer']) for r in cases if not r['diagnostic_item_id']] == [(saved['mistake'], '72')]
        assert [r['submitted_answer'] for r in cases if r['diagnostic_item_id']] == ['-999']
        assert c.execute('SELECT COUNT(*) FROM mistake_attempt WHERE mistake_id=?', (saved['mistake'],)).fetchone()[0] == 0
        assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (saved['school'],)).fetchone()[0] == 'completed'
        assert c.execute('SELECT artifact_id FROM capture_artifact WHERE artifact_id=?', (saved['artifact'],)).fetchone()
