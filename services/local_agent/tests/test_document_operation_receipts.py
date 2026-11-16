import pytest

from home_study_agent.document_operation_receipts import DocumentOperationReceipts
from test_device_receipt_validation import runtime
from test_document_inspection_receipt import inspection


@pytest.mark.parametrize('operation', ['ocr', 'revision'])
def test_only_final_receipt_survives_restart_without_reexecuting(inspection, operation):
    runtime, result, _ = inspection
    db, _, sid, _ = runtime
    revision = result['execution_receipt']['output']['revision_id']
    action = 'ocr-action' if operation == 'ocr' else 'save-inspection-page'
    service = DocumentOperationReceipts(db)
    assert service.read(sid, action, operation)['status'] == 'unconfirmed'
    service.record(sid, action, operation, 'artifact', revision if operation == 'revision' else None)
    service.record(sid, action, operation, 'artifact', revision if operation == 'revision' else None)
    restarted = DocumentOperationReceipts(db)
    assert restarted.read(sid, action, operation) == dict(
        session_id=sid, client_action_id=action, operation=operation, status='saved',
        local_only=True, replay_allowed=False, artifact_id='artifact',
        revision_id=revision if operation == 'revision' else None)
    assert restarted.read('another-session', action, operation)['status'] == 'unconfirmed'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM document_operation_receipt').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM document_page_revision').fetchone()[0] == 1


def test_changed_or_deleted_source_is_not_restored(inspection):
    runtime, result, _ = inspection
    db, _, sid, _ = runtime
    service = DocumentOperationReceipts(db)
    service.record(sid, 'ocr-action', 'ocr', 'artifact')
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='Changed material'")
        c.commit()
    assert service.read(sid, 'ocr-action', 'ocr')['status'] == 'unconfirmed'
    with db.connect() as c:
        c.execute('DELETE FROM document_page_revision')
        c.execute('DELETE FROM capture_artifact')
        c.commit()
    assert DocumentOperationReceipts(db).read(sid, 'ocr-action', 'ocr')['status'] == 'unconfirmed'


def test_fabricated_source_cannot_be_completed(inspection):
    runtime, _, _ = inspection
    service = DocumentOperationReceipts(runtime[0])
    with pytest.raises(ValueError, match='SOURCE_INVALID'):
        service.record(runtime[2], 'some-other-action', 'ocr', 'artifact')


def test_prepared_but_unknown_operation_is_durable_and_not_acknowledged(inspection):
    runtime, _, _ = inspection
    db, _, sid, _ = runtime
    service = DocumentOperationReceipts(db)
    service.prepare(sid, 'not-yet-sent', 'ocr')
    pending = DocumentOperationReceipts(db).pending(sid)
    assert len(pending) == 1 and pending[0]['status'] == 'unconfirmed'
    with pytest.raises(ValueError, match='NOT_CONFIRMED'):
        service.acknowledge(sid, 'not-yet-sent', 'ocr')
    with pytest.raises(ValueError, match='IDENTITY_CONFLICT'):
        service.prepare(sid, 'not-yet-sent', 'revision')
    assert service.pending(sid) == pending
