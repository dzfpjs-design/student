"""Real scheduled writing HTTP and durable breaks, synthetic voice and model."""
import pytest

from test_scheduled_writing_natural_runtime_http import scheduled,native_answer,post
from test_voice_runtime_binding import capture,interpret


@pytest.mark.parametrize('when',['before_answer','after_answer'])
def test_writing_break_does_not_erase_original_or_block_feedback(tmp_path,monkeypatch,when):
    with scheduled(tmp_path) as case:
        if when=='after_answer':
            result,_=native_answer(case)
        for control,quote in [('break.begin','休息一下，等会再继续。'),('break.resume','休息好了，接着刚才的作文。')]:
            case['transport'].control=control
            voice=capture(case['client'],case['settings'],monkeypatch,case['sid'],quote,
                case['prompt'],control+'-audio',child=case['prompt']['child_id'])
            turn=interpret(case['client'],case['sid'],quote,control+'-intent',voice)
            assert turn.get('runtime_capability')==control,turn
            post(case['client'],'/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})
        if when=='before_answer':
            result,_=native_answer(case)
        follow=result['local_learning_evaluation']
        assert follow['status']=='feedback_required',follow
        evaluated=post(case['client'],'/v1/agent-turns/'+follow['feedback_turn_id']+'/execute',{})
        value=evaluated['execution']['result']
        assert value['formative_only'] and value['rubric_assessment']['source_evidence_id']==result['attempt']['evidence_id']
        with case['db'].connect() as c:
            saved=c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?',
                (result['attempt']['attempt_id'],)).fetchone()
            assert saved[0]==result['attempt']['submitted_answer']
            assert c.execute('SELECT count(*) FROM break_control_receipt WHERE session_id=?',(case['sid'],)).fetchone()[0]==2
