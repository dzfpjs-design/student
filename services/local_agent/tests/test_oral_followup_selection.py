"""Source selection is not command admission, scoring or prompt preparation."""
import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from test_oral_teaching import prepared, low
from test_pet_media_evidence import CHILD, media


def test_ordinary_arrival_without_diagnostic_schema_has_no_oral_focus(tmp_path):
    from test_pet_oral_supply_http import setup
    from home_study_agent.repository import StudyRepository
    from home_study_agent.learning_input_revision import LearningInputRevisionService
    db,_=setup(tmp_path)
    sid=StudyRepository(db).start_session(CHILD,'ordinary-session')['session_id']
    revisions=LearningInputRevisionService(db)
    revisions.initialize()
    with db.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='diagnostic_prompt'").fetchone()
        c.execute('BEGIN IMMEDIATE')
        arrival=revisions.capture_arrival(sid,'ordinary-arrival','你好',connection=c)
        assert AgentRuntimeStore.capture_oral_followup_arrival(c,arrival['arrival_id'],None) is None


def resolve(db, sid, prompt, kind='retry'):
    with db.connect() as c:
        c.execute('BEGIN')
        before = list(c.iterdump())
        result = AgentRuntimeStore.resolve_oral_followup(c, child_id=CHILD, session_id=sid,
            prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], kind=kind)
        assert list(c.iterdump()) == before
        return result


def test_retry_original_keeps_exact_source_and_is_supported_not_independent(media):
    db, _, _, _, prompt = prepared(media)
    packet = resolve(db,media[2],prompt)
    assert packet['status'] == 'ready'
    action = packet['action']
    assert action['diagnostic_item_id'] == prompt['diagnostic_item_id']
    assert action['parent_prompt_id'] == prompt['prompt_id']
    assert action['parent_item_version'] == prompt['item_version']
    assert action['required_hint_level'] == 1
    assert not action['independent_check'] and not action['mastery_claim_allowed']
    assert packet['requires_runtime_admission'] and packet['requires_new_prompt_and_capture']


@pytest.mark.parametrize('fault',['review','source','answer','other_child'])
def test_frozen_retry_cannot_follow_changed_source_or_review(media,fault):
    db, service, assessment, body, prompt = prepared(media)
    packet = resolve(db,media[2],prompt)
    if fault == 'review':
        service.review(CHILD,assessment['assessment_id'],**low(body))
    else:
        with db.connect() as c:
            if fault == 'source':
                c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(prompt['diagnostic_item_id'],))
            elif fault == 'answer':
                c.execute("UPDATE diagnostic_attempt SET submitted_answer='different answer' WHERE attempt_id=(SELECT attempt_id FROM diagnostic_prompt WHERE prompt_id=?)",(prompt['prompt_id'],))
            else:
                packet['action']['child_id'] = 'child-junior-9'
            c.commit()
    with db.connect() as c:
        c.execute('BEGIN')
        assert AgentRuntimeStore.revalidate_oral_followup(c,**{k:packet[k] for k in ('action','binding_version')})['status'] == 'blocked'


def test_independent_selection_cannot_relabel_same_or_untyped_question(media):
    db, _, _, _, prompt = prepared(media)
    result = resolve(db,media[2],prompt,'independent_check')
    assert result['status'] == 'blocked'
    assert result['reason'] in {'oral_task_family_unknown','oral_no_unexposed_source'}
