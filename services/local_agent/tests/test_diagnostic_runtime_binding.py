from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository


def test_runtime_tracks_actual_question_not_page_or_agent_claim(tmp_path):
    db = Database(tmp_path / 'binding.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    sid = repo.start_session('child-primary-4', 'start')['session_id']
    store = AgentRuntimeStore(db)
    assert store.pending_child_reply(sid) is None
    prompts = DiagnosticPromptService(db)
    result = prompts.prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1', client_action_id='prepare')
    prompt = result['prompt']
    assert prompt
    pending = AgentRuntimeStore(Database(db.path)).pending_child_reply(sid)
    assert pending['capability_name'] == 'diagnostic.start'
    assert pending['result']['prompt_id'] == prompt['prompt_id']
    assert 'expected_answer' not in str(pending)
    assert 'item_json' not in str(pending)
    prompts.cancel(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
    assert store.pending_child_reply(sid) is None
    for name in ('diagnostic.start', 'diagnostic.replay', 'diagnostic.submit'):
        assert CapabilityRegistry().require(name, 'check_in').name == name


def test_invalidated_source_cannot_capture_next_utterance(tmp_path):
    db = Database(tmp_path / 'source.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    sid = repo.start_session('child-primary-4', 'start')['session_id']
    DiagnosticPromptService(db).prepare(sid, preferred_item_id='diag-p4-arithmetic-closed-1', client_action_id='prepare')
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt='changed source' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
        c.commit()
    assert AgentRuntimeStore(db).pending_child_reply(sid) is None
