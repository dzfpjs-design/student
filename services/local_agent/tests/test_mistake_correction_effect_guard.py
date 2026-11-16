"""Synthetic correction waits for the external-effect guard before writes."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from home_study_agent.database import Database
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_gate_reliability import flow
from test_mistake_dialogue_diagnosis import opened, speak


def test_busy_correction_preserves_original_action_across_restart(flow,monkeypatch):
    db,_,_,_=flow
    dialogue,_,_,sid=opened(flow)
    speak(dialogue,sid,'我不知道哪里错了','synthetic-reason')
    speak(dialogue,sid,'41','synthetic-answer')
    original_guard=db.external_effect_guard
    reached=Event()
    def short_guard(timeout=120,**kwargs):
        reached.set()
        return original_guard(timeout=.4,**kwargs)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original_guard():
            monkeypatch.setattr(db,'external_effect_guard',short_guard)
            pending=pool.submit(speak,dialogue,sid,'刚才的答案改成42','synthetic-original-correction')
            assert reached.wait(10)
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert c.execute('SELECT COUNT(*) FROM learning_evidence_withdrawal').fetchone()[0]==0
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
                c.rollback()
            result=pending.result(timeout=10)
            assert '重试' in result['response_text']
    restarted=MistakeDialogueService(Database(db.path))
    result=speak(restarted,sid,'重试','synthetic-retry')
    assert 'pending_operation' not in result['state']
    with db.connect() as c:
        assert [row[0] for row in c.execute('SELECT submitted_answer FROM diagnostic_attempt ORDER BY rowid')]==['41','42']
        rows=c.execute('SELECT client_action_id,reason FROM learning_evidence_withdrawal').fetchall()
        assert len(rows)==1 and rows[0]['reason']=='answer_corrected'
        assert rows[0]['client_action_id'].startswith('synthetic-original-correction')


def test_new_answer_does_not_wait_for_smtp_guard(flow):
    db,_,_,_=flow
    dialogue,_,_,sid=opened(flow)
    speak(dialogue,sid,'我不知道哪里错了','synthetic-reason')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.external_effect_guard():
            result=pool.submit(speak,dialogue,sid,'41','synthetic-answer').result(timeout=5)
    assert '重试' not in result['response_text']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==1
