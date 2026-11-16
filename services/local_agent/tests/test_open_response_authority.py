"""Published school questions use original-answer and explicit-purpose authority."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from home_study_agent.writing_question_authorization import WritingQuestionAuthorizationService, authorized_question_projection
from home_study_agent.writing_response_origin import WritingOriginalService, original_written_response
from test_open_response_supply_http import CHILD, prepared, publish_open
from test_writing_question_authorization import LICENSE


@pytest.mark.parametrize('subject',['数学','物理'])
def test_open_written_original_and_question_authority_survive_restart_without_scores(tmp_path,subject):
    db,supply,spec,_,_ = prepared(tmp_path,subject,license_status='public')
    published = publish_open(supply,spec)
    item_id = published['diagnostic_item_id']
    privacy = PrivacyLifecycleService(db,db.path.parent)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    authority = WritingQuestionAuthorizationService(db)
    authority.initialize()
    initial = authority.read(CHILD,diagnostic_item_id=item_id)
    assert initial['status']=='authorization_required',initial
    sid = StudyRepository(db).start_session(CHILD,'start')['session_id']
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid,track='school',subject=subject,preferred_item_id=item_id,client_action_id='ask')['prompt']
    text = '我先核对题中的条件，再说明选择这一关系的理由；还需要说明结论在什么条件下成立。'
    original = WritingOriginalService(db).capture_native_response(sid,prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'],text=text,client_action_id='original')
    result = prompts.answer(sid,prompt_id=prompt['prompt_id'],item_version=prompt['item_version'],answer=text,
        writing_original_artifact_id=original['artifact_id'],client_action_id='answer')
    assert result['attempt']['score_status']=='needs_review' and not result['attempt']['mastery_update_allowed']
    scope = {key:initial[key] for key in ('diagnostic_item_id','item_version','source_version','curriculum_version')}
    granted = authority.grant(CHILD,**scope,license_basis=LICENSE,expected_revision=0,
        client_action_id='grant',actor_id='parent_ui')
    assert granted['current']['status']=='authorized'
    with Database(db.path).connect() as c:
        pinned = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
        attempt = c.execute('SELECT * FROM diagnostic_attempt WHERE attempt_id=?',(pinned['attempt_id'],)).fetchone()
        proof = original_written_response(c,pinned,attempt)
        assert proof['artifact_id']==original['artifact_id'] and not proof['cloud_transmission_allowed']
        projected = authorized_question_projection(c,child_id=CHILD,diagnostic_item_id=item_id)
        assert projected['question']==prompt['item']['prompt']
    authority.revoke(CHILD,diagnostic_item_id=item_id,expected_revision=1,client_action_id='revoke',actor_id='parent_ui')
    with db.connect() as c,pytest.raises(ValueError):
        authorized_question_projection(c,child_id=CHILD,diagnostic_item_id=item_id)


def test_spoken_explanation_cannot_acquire_written_original(tmp_path):
    db,supply,spec,source,_ = prepared(tmp_path,'数学')
    source['response_mode']='spoken_explanation'
    with db.connect() as c:
        c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE excerpt_id=?',(json.dumps(source),spec['excerpt_id']))
        c.commit()
    item_id = publish_open(supply,spec)['diagnostic_item_id']
    sid = StudyRepository(db).start_session(CHILD,'start')['session_id']
    prompt = DiagnosticPromptService(db).prepare(sid,track='school',subject='数学',
        preferred_item_id=item_id,client_action_id='ask')['prompt']
    with pytest.raises(ValueError,match='WRITTEN_RESPONSE_MODE_REQUIRED'):
        WritingOriginalService(db).capture_native_response(sid,prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'],text='A spoken explanation is not a manuscript.',client_action_id='invalid')
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM capture_artifact').fetchone()[0]==0
