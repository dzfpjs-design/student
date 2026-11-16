"""Current authorization checks over real HTTP-produced OCR artifacts."""
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.local_supply import digest, LocalSupplyError
from home_study_agent.paper_variant_authorization import PaperVariantAuthorizationService, validate_job_authorization
from home_study_agent.variant_supply import VariantSupplyService
from test_paper_variant_line_projection_http import captured_question, gateway_for, post, QUESTION
from test_paper_variant_authorization_http import CHILD, body, url
from test_writing_question_authorization_http import parent


def grant(db, mid, key='grant-projection'):
    service = PaperVariantAuthorizationService(db)
    view = service.read(CHILD, mistake_id=mid)
    assert view['question_preview'] == QUESTION
    return service.grant(CHILD, mistake_id=mid, actor_id='parent_ui', **body(view, key))


def test_original_page_lines_block_region_and_metadata_are_current_not_just_question(captured_question):
    db, _, _, artifact, mid, candidate = captured_question
    allowed = grant(db, mid)
    jid = allowed['generation_job']['job_id']
    with db.connect() as c:
        raw = c.execute('SELECT binding_json FROM paper_variant_authorization_event WHERE event_id=?',
            (allowed['receipt']['event_id'],)).fetchone()[0]
        binding = json.loads(raw)
        assert binding['question_start'] is None and binding['question_end'] is None
        projection = binding['question_projection']
        assert projection['version'] == 'paper_question_lines_v2'
        assert [r['index'] for r in projection['line_refs']] == [2, 1]
        assert projection['artifact_hash'] == digest(dict(c.execute(
            'SELECT * FROM capture_artifact WHERE artifact_id=?', (artifact['artifact_id'],)).fetchone()))
    for mutation in ('page', 'unselected_line', 'geometry', 'block', 'candidate_region', 'other_child',
                     'local_only', 'private', 'graph', 'consent'):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            details = deepcopy(artifact['details'])
            if mutation == 'page':
                c.execute("UPDATE capture_artifact SET extracted_text=extracted_text || 'changed' WHERE artifact_id=?", (artifact['artifact_id'],))
            elif mutation == 'unselected_line':
                details['lines'][0]['text'] += 'changed'
                c.execute('UPDATE capture_artifact SET extracted_text=? WHERE artifact_id=?',
                    ('\n'.join(line['text'] for line in details['lines']), artifact['artifact_id']))
            elif mutation == 'geometry':
                details['lines'][1]['box'][0][0] += 1
            elif mutation == 'block':
                details['local_pipeline']['question_blocks'][0]['bbox_xywh'][0] += 1
            elif mutation == 'candidate_region':
                c.execute("UPDATE mistake_capture_candidate SET region_json='{}' WHERE candidate_id=?", (candidate['candidate_id'],))
            elif mutation == 'other_child':
                c.execute("UPDATE capture_artifact SET child_id='child-junior-3' WHERE artifact_id=?", (artifact['artifact_id'],))
            elif mutation == 'local_only':
                details['restriction'] = {'local_only': True}
            elif mutation == 'private':
                details['restriction'] = {'license_status': 'private'}
            elif mutation == 'graph':
                details['restriction'] = {'source_ref': 'private-graph'}
            else:
                c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0 WHERE singleton_id=1')
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
                (json.dumps(details), artifact['artifact_id']))
            with pytest.raises(ValueError, match='PAPER_VARIANT_'):
                validate_job_authorization(c, job_id=jid)
            assert c.execute('SELECT binding_json FROM paper_variant_authorization_event WHERE event_id=?',
                (allowed['receipt']['event_id'],)).fetchone()[0] == raw
            c.rollback()
    assert PaperVariantAuthorizationService(db).read(CHILD, mistake_id=mid)['status'] == 'authorized'


def test_v1_contiguous_grant_never_silently_upgrades_to_line_mapping(captured_question):
    db, _, _, artifact, mid, candidate = captured_question
    ordered = deepcopy(artifact['details'])
    ordered['lines'].reverse()
    with db.connect() as c:
        c.execute('UPDATE capture_artifact SET extracted_text=?,details_json=? WHERE artifact_id=?',
            ('\n'.join(line['text'] for line in ordered['lines']), json.dumps(ordered), artifact['artifact_id']))
        c.commit()
    old = grant(db, mid, 'legacy-span-grant')
    with db.connect() as c:
        original = c.execute('SELECT * FROM paper_variant_authorization_event WHERE event_id=?', (old['receipt']['event_id'],)).fetchone()
        frozen = dict(original)
        binding = json.loads(original['binding_json'])
        assert 'question_projection' not in binding
        assert binding['question_start'] == 0 and binding['question_end'] == len(QUESTION)
        node = dict(c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id='node-p4-math-arithmetic'").fetchone())
        region_json = c.execute('SELECT region_json FROM mistake_capture_candidate WHERE candidate_id=?',
            (candidate['candidate_id'],)).fetchone()[0]
        assert binding['question_version'] == digest({'question': QUESTION, 'region': region_json, 'node': node})
        c.execute('UPDATE capture_artifact SET extracted_text=?,details_json=? WHERE artifact_id=?',
            (artifact['extracted_text'], json.dumps(artifact['details']), artifact['artifact_id']))
        c.commit()
    service = PaperVariantAuthorizationService(db)
    assert service.read(CHILD, mistake_id=mid)['status'] == 'invalidated'
    with pytest.raises(LocalSupplyError):
        VariantSupplyService(db).retry_from_parent(CHILD, old['generation_job']['job_id'], 'retry-legacy-span')
    replay = service.grant(CHILD, mistake_id=mid, question_version=binding['question_version'],
        source_version=binding['source_version'], license_basis=json.loads(frozen['license_json']),
        expected_revision=0, client_action_id='legacy-span-grant', actor_id='parent_ui')
    assert replay['current']['status'] == 'invalidated' and replay['receipt'] == old['receipt']
    current = grant(db, mid, 'new-explicit-line-grant')
    assert current['generation_job']['job_id'] != old['generation_job']['job_id']
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM paper_variant_authorization_event WHERE event_id=?',
            (old['receipt']['event_id'],)).fetchone()) == frozen
        assert c.execute('SELECT status FROM variant_generation_job WHERE job_id=?',
            (old['generation_job']['job_id'],)).fetchone()[0] == 'blocked'
        new = json.loads(c.execute('SELECT binding_json FROM paper_variant_authorization_event WHERE event_id=?',
            (current['receipt']['event_id'],)).fetchone()[0])
        assert new['question_projection']['version'] == 'paper_question_lines_v2'


@pytest.mark.parametrize('mutation', ['source', 'revoke'])
def test_actual_response_after_source_change_never_persists_candidates_or_restarts_old_job(captured_question, mutation):
    db, settings, _, artifact, mid, _ = captured_question
    posts = []

    def change_during_request():
        if mutation == 'revoke':
            PaperVariantAuthorizationService(db).revoke(CHILD, mistake_id=mid, expected_revision=1,
                client_action_id='revoke-during-response', actor_id='parent_ui')
            return
        with db.connect() as c:
            c.execute("UPDATE capture_artifact SET extracted_text=extracted_text || 'changed' WHERE artifact_id=?", (artifact['artifact_id'],))
            c.commit()

    with TestClient(create_app(settings, model_gateway=gateway_for(settings, posts, change_during_request))) as client:
        headers = parent(client)
        allowed = post(client, url(mid) + '/grant', body(client.get(url(mid), headers=headers).json()), headers=headers)
        response = client.post('/v1/parent/variant-jobs/process-next', headers=headers)
        if mutation == 'source':
            assert response.status_code == 422, response.text
            assert response.json()['code'] == 'LOCAL_SUPPLY_UNAVAILABLE'
        else:
            assert response.status_code == 200, response.text
            assert response.json()['job'] is None
        assert len(posts) == 1
        assert client.get(url(mid), headers=headers).json()['status'] == ('invalidated' if mutation == 'source' else 'revoked')
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, posts))) as client:
        headers = parent(client)
        assert post(client, '/v1/parent/variant-jobs/process-next', {}, headers=headers)['job'] is None
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM variant_candidate WHERE job_id=?',
            (allowed['generation_job']['job_id'],)).fetchone()[0] == 0
        assert c.execute('SELECT status FROM variant_generation_job WHERE job_id=?',
            (allowed['generation_job']['job_id'],)).fetchone()[0] == 'blocked'
    assert len(posts) == 1
