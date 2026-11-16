import asyncio
from normal_provider_owner import normal_provider_owner
from datetime import datetime, timezone

from home_study_agent.child_safety import ChildSafetyService, persist_safety_event, classify_child_safety
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.parent_work_queue import ParentWorkQueueService
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from home_study_agent.variant_supply import VariantSupplyService
from test_learning_outcome_mastery_gate import gate, flow
from test_learning_rubric_review import review
from test_daily_report_evidence_versions import flow as report_flow, CHILD
from test_report_delivery_recovery_journey import smtp, prepare
from test_variant_supply import _prepared, FakeVariantGateway
from test_specialized_supply import FakeSupplyGateway
from test_parent_work_queue import work


def test_actual_rubric_retraction_and_safety_acknowledgement(gate):
    result,_,turn=gate.run(local=True)
    assessment=result['evaluation']['rubric_assessment']
    with gate.db.connect() as c:
        action={'risk_level':'R3','category':'immediate_danger'}
        persist_safety_event(c,CHILD,gate.sid,turn,action,datetime.now(timezone.utc).isoformat())
        c.commit()
    service=ParentWorkQueueService(gate.db)
    projected=service.list(child_id=CHILD)
    assert projected['items'][0]['kind']=='safety'
    assert any(i['object_id']==assessment['assessment_id'] for i in projected['items'])
    ChildSafetyService(gate.db).acknowledge(projected['items'][0]['object_id'],'ack-queue-safety')
    rubric=RubricEvidenceService(gate.db)
    review(rubric,assessment)
    assert not any(i['kind'] in ('rubric','safety') for i in service.list(child_id=CHILD)['items'])
    review(rubric,assessment,revision=1,operation='retract')
    assert any(i['object_id']==assessment['assessment_id'] for i in service.list(child_id=CHILD)['items'])


def test_real_report_approval_unknown_receipt_and_explicit_keep_without_resend(report_flow,smtp,normal_provider_owner):
    db,_,report,delivery=report_flow
    queue=ParentWorkQueueService(db)
    draft=delivery.snapshots.create_from_daily(report)
    assert any(i['object_id']==draft['snapshot_id'] for i in queue.list(child_id=CHILD)['items'])
    snapshot,recipient=prepare(report_flow,smtp,'daily')
    assert not any(i['object_id']==draft['snapshot_id'] for i in queue.list(child_id=CHILD)['items'])
    queued=delivery.enqueue(snapshot['snapshot_id'],recipient['recipient_id'])
    smtp.mode='unknown'
    delivery.process_due()
    found=next(i for i in queue.list(child_id=CHILD)['items'] if i['kind']=='delivery_unknown')
    assert found['object_id']==queued['delivery_id'] and found['target']['related_id']==snapshot['snapshot_id']
    assert 'address' not in str(found) and 'smtp' not in str(found)
    assert len(smtp.submissions)==1
    delivery.revoke_recipient(recipient['recipient_id'])
    assert any(i['kind']=='delivery_unknown' for i in queue.list(child_id=CHILD)['items'])
    delivery.keep_unknown(queued['delivery_id'])
    assert not any(i['kind']=='delivery_unknown' for i in queue.list(child_id=CHILD)['items'])
    assert len(smtp.submissions)==1


def test_actual_variant_and_specialized_candidates_leave_queue_after_review(tmp_path):
    db,_,mistake=_prepared(tmp_path)
    variants=VariantSupplyService(db,FakeVariantGateway())
    variants.enqueue(mistake,'child-junior-3','new-variants')
    asyncio.run(variants.process_next())
    specialized=SpecializedDiagnosticSupplyService(db,FakeSupplyGateway())
    specialized.enqueue_gaps(CHILD,'competition_math','new-specialized',4)
    asyncio.run(specialized.process_next())
    CurriculumSupplyService(db).ensure_schema()
    queue=ParentWorkQueueService(db)
    for kind,child,service in [('variant','child-junior-3',variants),('specialized',CHILD,specialized)]:
        before=[i for i in queue.list(child_id=child,limit=50)['items'] if i['kind']==kind]
        assert before
        service.review(before[0]['object_id'],'reject','合成材料需要修订','reject-'+kind)
        after=[i for i in queue.list(child_id=child,limit=50)['items'] if i['kind']==kind]
        assert len(after)==len(before)-1


def test_real_blocked_local_job_opens_its_own_readonly_detail_not_a_package(work):
    db,_,payload,_,queue=work
    service=SpecializedDiagnosticSupplyService(db)
    job=service.enqueue_local('diag-pet-writing-1',CHILD,'blocked-local-job')
    service.process_local_pending()
    row=next(i for i in queue.list(child_id=CHILD,limit=50)['items'] if i['object_id']==job['job_id'])
    assert row['target']['panel']=='parent-work-supply-gap'
    detail=queue.supply_gap(CHILD,job['job_id'])
    assert detail['job_id']==job['job_id'] and detail['status']=='blocked'
    assert detail['evidence'][0]['text']
    assert detail['cloud_transmission_allowed'] is False
