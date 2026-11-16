"""Display authority, rollback and process-reopen boundaries on actual parent APIs."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.visual_question_evidence import current_visual
from home_study_agent.local_supply import validate_item
from test_visual_rubric_identity import published, assessment, display_image, review_body
from test_visual_material_host import headers, REGION
from test_pet_reviewed_source_actual_http import NoCloud


def review_url(snap, child='child'):
    return '/v1/parent/children/'+child+'/learning-rubrics/'+snap['assessment_id']+'/review'


def test_display_receipt_cannot_be_consumed_by_another_real_original_assessment(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        _, first = assessment(case, monkeypatch)
        # The imported no-model fixture owns a synthetic active objective per
        # assessment. Park it before creating the second; no score is fabricated.
        with case.db.connect() as c:
            c.execute("UPDATE agent_objective SET status='paused' WHERE objective_id=? AND child_id='child'",
                      (first['objective_id'],))
            c.commit()
        _, second = assessment(case, monkeypatch, key='second-original')
        display = display_image(case, 'both-originals-exist-display')
        one = case.client.post(review_url(first), headers=case.headers, json=review_body(case, first, display))
        assert one.status_code == 200, one.text
        body = review_body(case, second, display)
        body['client_action_id'] = 'second-review'
        two = case.client.post(review_url(second), headers=case.headers, json=body)
        assert two.status_code == 409, two.text
        assert two.json()['detail']['code'] == 'RUBRIC_VISUAL_DISPLAY_ALREADY_USED'
        body['visual_evidence']['displays'][0]['display_receipt_id'] = display_image(case, 'second-assessment-display')
        two = case.client.post(review_url(second), headers=case.headers, json=body)
        assert two.status_code == 200, two.text
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 2
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_original_visual_review_survives_actual_app_reopen_and_source_revoke(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        _, snap = assessment(case, monkeypatch)
        body = review_body(case, snap, display_image(case, 'answer-display'))
        result = case.client.post(review_url(snap), headers=case.headers, json=body)
        assert result.status_code == 200, result.text
        event = result.json()['event']
        with case.db.connect() as c:
            original = tuple(c.execute('SELECT payload_json,fingerprint FROM rubric_assessment WHERE assessment_id=?',
                                      (snap['assessment_id'],)).fetchone())
    with TestClient(create_app(case.settings, model_gateway=NoCloud()), client=('127.0.0.1', 42181)) as client:
        h = headers(client)
        current = client.get(review_url(snap).removesuffix('/review'), headers=h)
        assert current.status_code == 200, current.text
        assert current.json()['assessment']['material']['visual']['status'] == 'current'
        other = client.post(review_url(snap, 'child-primary-4'), headers=h, json=body)
        assert other.status_code == 409, other.text
        assert other.json()['detail']['code'] == 'RUBRIC_ASSESSMENT_NOT_OWNED'
        revoke = client.post(REGION+'/retract', headers=h, json={**case.args, 'client_action_id':'after-restart-retract'})
        assert revoke.status_code == 200, revoke.text
        replay = client.post(review_url(snap), headers=h, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()['event'] == event
        assert replay.json()['assessment']['source_status'] == 'changed'
        assert not replay.json()['qualification']['eligible_for_mastery_gate']
        changed = client.post(review_url(snap), headers=h, json={**body, 'reason':'Changed payload, same key'})
        assert changed.status_code == 409
        assert changed.json()['detail']['code'] == 'RUBRIC_IDEMPOTENCY_CONFLICT'
        db = client.app.state.repository.database
        with db.connect() as c:
            assert tuple(c.execute('SELECT payload_json,fingerprint FROM rubric_assessment WHERE assessment_id=?',
                (snap['assessment_id'],)).fetchone()) == original
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def test_visual_review_failed_insert_rolls_back_and_original_key_can_retry(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        service, snap = assessment(case, monkeypatch)
        body = review_body(case, snap, display_image(case, 'answer-display'))
        with case.db.connect() as c:
            c.execute("CREATE TRIGGER visual_review_write_failure BEFORE INSERT ON rubric_review_event BEGIN SELECT RAISE(ABORT,'isolated review disk failure'); END")
            c.commit()
        with pytest.raises(sqlite3.IntegrityError, match='isolated review disk failure'):
            case.client.post(review_url(snap), headers=case.headers, json=body)
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
            c.execute('DROP TRIGGER visual_review_write_failure')
            c.commit()
        result = case.client.post(review_url(snap), headers=case.headers, json=body)
        assert result.status_code == 200, result.text
        with case.db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 1


def test_visual_current_uses_owned_borrowed_snapshot_and_never_commits_it(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        with case.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            item = validate_item(c, 'child', case.item_id)
            before = c.total_changes
            boundaries = []
            c.set_trace_callback(lambda sql: boundaries.append(sql) if sql.split()[0] in {'COMMIT','ROLLBACK','END'} else None)
            assert current_visual(c, 'child', item)['status'] == 'current'
            assert c.in_transaction and c.total_changes == before and boundaries == []
            source = case.source['material_bindings'][0]['source_id']
            c.execute("UPDATE knowledge_source SET version_label='uncommitted revision' WHERE source_id=?", (source,))
            with pytest.raises(ValueError):
                current_visual(c, 'child', item)
            assert c.in_transaction and boundaries == []
            c.set_trace_callback(None)
            c.rollback()
            c.execute('BEGIN')
            assert current_visual(c, 'child', item)['status'] == 'current'
