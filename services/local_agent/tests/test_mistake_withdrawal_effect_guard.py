"""A scored answer's original withdrawal waits before taking SQLite's writer."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from home_study_agent.database import Database
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_gate_reliability import flow
from test_mistake_dialogue_diagnosis import opened, speak


def test_busy_scored_withdrawal_retains_original_action_for_restart(flow,monkeypatch):
    db,_,_,_=flow
    dialogue,_,_,sid=opened(flow)
    speak(dialogue,sid,'我不知道哪里错了','synthetic-reason')
    speak(dialogue,sid,'41','synthetic-answer')
    original_guard=db.external_effect_guard
    started=Event()
    def short_guard(timeout=120,**kwargs):
        started.set()
        return original_guard(timeout=.4,**kwargs)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original_guard():
            monkeypatch.setattr(db,'external_effect_guard',short_guard)
            pending=pool.submit(speak,dialogue,sid,'撤回刚才已评分的答案','synthetic-original-withdraw')
            assert started.wait(10)
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                assert c.execute('SELECT COUNT(*) FROM learning_evidence_withdrawal').fetchone()[0]==0
                c.rollback()
            result=pending.result(timeout=10)
            assert '重试' in result['response_text']
    restarted=MistakeDialogueService(Database(db.path))
    result=speak(restarted,sid,'重试','synthetic-retry')
    assert 'pending_operation' not in result['state']
    with db.connect() as c:
        rows=c.execute('SELECT client_action_id,reason FROM learning_evidence_withdrawal').fetchall()
        assert [tuple(row) for row in rows]==[('synthetic-original-withdraw:withdraw-score','answer_withdrawn')]
