"""A spoken source correction must escape recapture's number-selection prompt."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.mistake_recapture import select_reply
from test_mistake_recapture_http import setup, photograph, finish, begin
from test_mistake_source_revision_http import Gateway
from test_formula_voice_confirmation import turn
from test_paper_variant_line_projection_http import captured_question, QUESTION, ANSWER


@pytest.mark.parametrize('text,forward', [
    ('错题题干改成3+5等于多少', True),
    ('题干改成3+5等于多少', True),
    ('题目是3+5等于多少', True),
    ('题目应该是：\n3+5等于多少', True),
    ('我想看这道题', False),
    ('他说题目是3+5等于多少', False),
    ('题目是', False),
])
@pytest.mark.parametrize('pending_answer', [False, True])
def test_helper_forwards_only_explicit_current_correction(text, forward, pending_answer):
    focus = {'phase': 'remediation', 'mistake_id': 'synthetic-mistake',
        'state': {'question_confirmation': {'recapture': {'turn_id': 'synthetic'}, 'question': ''}}}
    if pending_answer:
        focus['state']['pending_operation'] = {'utterance': 'original answer'}
    before = deepcopy(focus)
    class Dialogue:
        def _save(self, *args):
            return 'clarification'
    result = select_reply(Dialogue(), focus, text, 'synthetic-action')
    assert result == (None if forward and not pending_answer else 'clarification')
    assert focus == before


def test_multicandidate_to_spoken_source_revision_preserves_original_binding(captured_question):
    db, settings, sid, original, mid, candidate = captured_question
    gateway = Gateway(settings)
    corrected = QUESTION.replace('三十七', '四十一')
    assert corrected != QUESTION
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        requested = setup(client, captured_question)
        photograph_result = photograph(client, sid)
        finish(client, requested, photograph_result)
        assert begin(client, sid, photograph_result).status_code == 200
        offered = turn(client, sid, '题目应该是' + corrected, 'spoken-instead-of-pick')
        pending = offered['execution']['result']['state']['question_confirmation']
        assert pending['question'] == corrected
        assert 'recapture' not in pending
        assert offered['execution']['result']['mistake_id'] == mid
        late = begin(client, sid, photograph_result, key='late-recapture-after-spoken-offer')
        assert late.status_code == 422, late.text
        assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == pending
        with db.connect() as c:
            row = c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()
            assert tuple(row) == (QUESTION, ANSWER)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        confirmed = turn(client, sid, '没错，讲吧', 'confirm-spoken-after-recapture', gateway=gateway)
        assert confirmed['execution']['tutor_turn']['question'] == corrected
        current = MistakeDialogueService(db).state(sid)
        assert current['mistake_id'] == mid
        assert current['candidate_id'] == candidate['candidate_id']
        assert current['state']['artifact_id'] == original['artifact_id']
        with db.connect() as c:
            row = c.execute('SELECT prompt_summary,submitted_answer,source_ref FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()
            assert tuple(row) == (corrected, ANSWER, original['artifact_id'] + '#' + candidate['candidate_id'])
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 1
