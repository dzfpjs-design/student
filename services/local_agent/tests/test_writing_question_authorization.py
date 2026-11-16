"""Public reviewed publication is not a question-purpose cloud permission."""
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.writing_question_authorization import WritingQuestionAuthorizationService
from test_writing_source_publication import prepared, publish, CHILD


LICENSE = {'license_id':'CC0-1.0','reference':'https://example.org/isolated-source-license',
    'evidence':'Synthetic author dedication of this exact original question under CC0 1.0; reviewed by parent.'}


@pytest.fixture
def published_question(tmp_path):
    db,supply,spec,_ = prepared(tmp_path)
    published = publish(supply,spec)
    privacy = PrivacyLifecycleService(db,db.path.parent)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    service = WritingQuestionAuthorizationService(db)
    service.initialize()
    return db,service,supply,published,privacy


def grant(case, **changes):
    _,service,_,published,_ = case
    view = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    kwargs = {key:view[key] for key in ('diagnostic_item_id','item_version','source_version','curriculum_version')}
    kwargs.update(license_basis=LICENSE,expected_revision=view['revision'],client_action_id='grant',actor_id='authenticated-parent')
    kwargs.update(changes)
    return service.grant(CHILD,**kwargs)


def test_publication_defaults_denied_explicit_grant_restarts_and_exact_retry_never_regrants(published_question):
    db,service,_,published,_ = published_question
    item = published['diagnostic_item_id']
    initial = service.read(CHILD,diagnostic_item_id=item)
    assert initial['status']=='authorization_required' and initial['revision']==0
    granted = grant(published_question)
    assert granted['current']['status']=='authorized'
    revoked = service.revoke(CHILD,diagnostic_item_id=item,expected_revision=1,client_action_id='revoke',actor_id='authenticated-parent')
    assert revoked['current']['status']=='revoked'
    restarted = WritingQuestionAuthorizationService(Database(db.path))
    scope = {key:initial[key] for key in ('diagnostic_item_id','item_version','source_version','curriculum_version')}
    replay = restarted.grant(CHILD,**scope,license_basis=LICENSE,expected_revision=0,client_action_id='grant',actor_id='authenticated-parent')
    assert replay['receipt']==granted['receipt'] and replay['current']['status']=='revoked'
    with db.connect() as c:
        before = c.total_changes
        assert restarted.read(CHILD,diagnostic_item_id=item)['revision']==2
        assert c.total_changes==before
        assert c.execute('SELECT COUNT(*) FROM writing_question_authorization_event').fetchone()[0]==2
        assert c.execute('SELECT source_ref FROM diagnostic_item WHERE diagnostic_item_id=?',(item,)).fetchone()[0].startswith('parent_approved_specialized:')


@pytest.mark.parametrize('fault',['missing_consent','default_consent','family','unknown','license','reference','version','curriculum','actor','cas'])
def test_grant_requires_current_parent_source_and_license_not_public_label(published_question,fault):
    db,service,_,published,privacy = published_question
    args = {}
    if fault=='missing_consent': privacy.update_preferences({'cloud_model_text_allowed':False})
    elif fault=='default_consent':
        with db.connect() as c:
            c.execute("DELETE FROM consent_record WHERE scope='cloud_model_text_allowed'")
            c.commit()
    elif fault in {'family','unknown'}:
        with db.connect() as c:
            c.execute('UPDATE knowledge_source SET license_status=?',('family_owned_index' if fault=='family' else 'unknown',))
            c.commit()
    elif fault=='license': args['license_basis']={**LICENSE,'license_id':'public'}
    elif fault=='reference': args['license_basis']={**LICENSE,'reference':'file:///private/textbook.pdf'}
    elif fault=='version': args['item_version']='old'
    elif fault=='curriculum': args['curriculum_version']='old'
    elif fault=='actor': args['actor_id']=''
    elif fault=='cas': args['expected_revision']=7
    with pytest.raises(ValueError): grant(published_question,**args)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM writing_question_authorization_event').fetchone()[0]==0


@pytest.mark.parametrize('fault',['source','curriculum','item','withdraw','privacy'])
def test_permission_revalidates_all_versions_and_cannot_reactivate_after_privacy_reversal(published_question,fault):
    db,service,supply,published,privacy = published_question
    grant(published_question)
    if fault=='withdraw':
        supply.withdraw(CHILD,published['candidate_id'],actor='parent',note='Source withdrawn',action_id='withdraw',expected_version=4)
    elif fault=='privacy':
        privacy.update_preferences({'cloud_model_text_allowed':False})
        privacy.update_preferences({'cloud_model_text_allowed':True})
    else:
        with db.connect() as c:
            if fault=='source': c.execute("UPDATE knowledge_source SET source_ref='changed'")
            elif fault=='curriculum': c.execute("UPDATE curriculum_standard_page SET content_text='changed'")
            else: c.execute("UPDATE diagnostic_item SET prompt='changed' WHERE diagnostic_item_id=?",(published['diagnostic_item_id'],))
            c.commit()
    view = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    assert view['status']=='invalidated' and view['reason']
    assert service.revoke(CHILD,diagnostic_item_id=published['diagnostic_item_id'],expected_revision=1,
        client_action_id='revoke-invalid',actor_id='authenticated-parent')['current']['status']=='revoked'


def test_request_key_payload_and_child_cannot_borrow_authority(published_question):
    _,service,_,published,_ = published_question
    grant(published_question)
    with pytest.raises(ValueError): grant(published_question,expected_revision=0,license_basis={**LICENSE,'evidence':'different'})
    view = service.read('child-junior-3',diagnostic_item_id=published['diagnostic_item_id'])
    assert view['status']=='blocked' and view['preview'] is None and view['authorization_id'] is None


@pytest.mark.parametrize('child,item', [('unknown-child','missing-item'),(CHILD,'missing-item')])
def test_unknown_objects_are_controlled_read_results_without_content(published_question,child,item):
    _,service,_,_,_ = published_question
    view = service.read(child,diagnostic_item_id=item)
    assert view['status']=='blocked' and view['reason'].startswith('WRITING_QUESTION_')
    assert view['preview'] is None and view['authorization_id'] is None


@pytest.mark.parametrize('license_status',['family_owned_index','unknown'])
def test_current_published_family_material_never_becomes_public_by_rights_declaration(tmp_path,license_status):
    db,supply,spec,_ = prepared(tmp_path)
    with db.connect() as c:
        c.execute('UPDATE knowledge_source SET license_status=?',(license_status,))
        c.commit()
    service = WritingQuestionAuthorizationService(db)
    service.initialize()
    if license_status=='unknown':
        blocked = supply.generate_draft(CHILD,spec,author='author',action_id='unknown-source',expected_scope_version=1)
        assert blocked['state']=='blocked' and blocked['blockers']==['CURRICULUM_TEXTBOOK_UNREVIEWED']
        assert blocked['diagnostic_item_id'] is None
        return
    published = publish(supply,spec)
    privacy = PrivacyLifecycleService(db,db.path.parent)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    case = (db,service,supply,published,privacy)
    view = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    assert view['reason']=='WRITING_QUESTION_SOURCE_NOT_AUTHORIZABLE'
    with pytest.raises(ValueError,match='WRITING_QUESTION_SOURCE_NOT_AUTHORIZABLE'):
        grant(case,license_basis={**LICENSE,'license_id':'original_author'})


@pytest.mark.parametrize('license_id',['CC0-1.0','original_author','explicit_cloud_processing_permission'])
def test_explicit_rights_basis_is_human_review_not_automatic_license_verification(published_question,license_id):
    _,service,_,published,_ = published_question
    view = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    assert set(view['license_options'])=={'CC0-1.0','original_author','explicit_cloud_processing_permission'}
    assert view['rights_review_authority']=='parent_declaration_not_legal_or_network_verification'
    assert grant(published_question,license_basis={**LICENSE,'license_id':license_id})['current']['status']=='authorized'


def test_restore_quarantine_is_transactional_idempotent_and_does_not_revive_old_commands(published_question):
    from home_study_agent.writing_question_authorization import quarantine_after_restore
    db,service,_,published,_ = published_question
    scope = service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])
    original = grant(published_question)
    with db.connect() as c:
        with pytest.raises(ValueError,match='TRANSACTION_REQUIRED'): quarantine_after_restore(c,'restore-1')
        c.execute('BEGIN IMMEDIATE')
        assert quarantine_after_restore(c,'restore-1')==1
        c.rollback()
    assert service.read(CHILD,diagnostic_item_id=published['diagnostic_item_id'])['status']=='authorized'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert quarantine_after_restore(c,'restore-1')==1
        assert quarantine_after_restore(c,'restore-1')==0
        c.commit()
    replay = service.grant(CHILD,**{key:scope[key] for key in ('diagnostic_item_id','item_version','source_version','curriculum_version')},
        license_basis=LICENSE,expected_revision=0,client_action_id='grant',actor_id='authenticated-parent')
    assert replay['receipt']==original['receipt']
    assert replay['current']['status']=='revoked' and 'RESTORE_REAUTHORIZATION' in replay['current']['reason']
    assert grant(published_question,client_action_id='fresh-parent-grant')['current']['status']=='authorized'
    with db.connect() as c:
        rows = c.execute('SELECT * FROM writing_question_authorization_event ORDER BY revision').fetchall()
        assert len(rows)==3
        assert rows[1]['actor_id']=='system:restore'
        assert all('school concert' not in r['binding_json'] for r in rows)
