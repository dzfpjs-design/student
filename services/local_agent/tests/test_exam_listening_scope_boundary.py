"""The exam surface cannot present text as delivered listening audio."""
import pytest

from home_study_agent.exam_simulation import ExamSimulationService,ExamSimulationError,EXAM_LISTENING_BLOCKER
from test_exam_simulation import _database


def setup(tmp_path,monkeypatch):
    db,sid = _database(tmp_path/'isolated-exam-listening.db')
    service = ExamSimulationService(db)
    with db.connect() as c:
        items = [dict(c.execute('''SELECT di.diagnostic_item_id,kn.knowledge_node_id,kn.subject,kn.title
            FROM diagnostic_item di JOIN knowledge_node kn USING(knowledge_node_id) WHERE di.diagnostic_item_id=?''',
            (key,)).fetchone()) for key in ('diag-pet-listening-1','diag-pet-reading-1')]
    monkeypatch.setattr(service,'_balanced_items',lambda *args:[('pet.listening.detail',items[0]),('pet.reading.main_idea',items[1])])
    return db,sid,service


def test_selected_listening_paper_is_preserved_untimed_without_original_audio(tmp_path,monkeypatch):
    db,sid,service = setup(tmp_path,monkeypatch)
    run=service.start(sid,'pet','英语','practice',2,30,'real-listening-scope-missing')
    assert run['status']=='paused' and run['delivery_status']=='preparing'
    assert run['total_items']==2 and run['started_at'] is None and run['elapsed_seconds']==0
    assert run['current_item']['stimulus_text'] is None
    with db.connect() as c:
        original=c.execute("SELECT stimulus_text FROM diagnostic_item WHERE diagnostic_item_id='diag-pet-listening-1'").fetchone()[0]
        if original:
            import json
            assert original not in json.dumps(run,ensure_ascii=False)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM specialized_exam_run').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM specialized_exam_item').fetchone()[0]==2
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0


def test_legacy_listening_slot_hides_audio_text_and_rejects_capture_and_answers(tmp_path,monkeypatch):
    db,sid,service = setup(tmp_path,monkeypatch)
    # Construct the historical run shape allowed before the start boundary existed.
    with monkeypatch.context() as legacy:
        legacy.setattr(service,'_listening_item',lambda *args:False)
        run = service.start(sid,'pet','英语','practice',2,30,'historical-listening-run')
    status = service.status(run['run_id'])
    assert status['delivery_status']=='blocked' and status['delivery_blockers']==[EXAM_LISTENING_BLOCKER]
    assert status['current_item']['exam_audio_modality']=='listening'
    assert status['current_item']['stimulus_text'] is None and status['current_item']['stimulus_asset'] is None
    assert status['answer_binding'] is None and service.arrival_binding(sid) is None
    with db.connect() as c:
        slot = c.execute('SELECT * FROM specialized_exam_item WHERE run_id=? AND position=1',(run['run_id'],)).fetchone()
        binding = service._review_binding(run,slot)
    with pytest.raises(ExamSimulationError,match=EXAM_LISTENING_BLOCKER):
        service.submit_bound(sid,**binding,answer='library',client_action_id='unplayed-listening-answer')
    with pytest.raises(ExamSimulationError,match=EXAM_LISTENING_BLOCKER):
        service.submit_answer(run['run_id'],'library','legacy-unplayed-answer')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]==0
