"""Exact transaction-local candidate prefilter, never cached qualification."""
from datetime import datetime, timezone
import sqlite3
import json

import pytest

from home_study_agent import school_open_reviews as current
from test_school_open_reviews import school_case, answer_and_review, CHILD


@pytest.fixture
def prior():
    class Unindexed:
        # _anchor's unindexed branch retains the original full-history scan.
        # This keeps the differential fixture portable after integration.
        @staticmethod
        def reconcile_school_reviews(c, child, *, as_of):
            return current._reconcile(c, child, as_of=as_of, cancel_only=False, candidates=None,
                                      reauthorizing_assessment=None)
    return Unindexed


def compare(db, prior, child=CHILD):
    at = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        states = []
        for module in (prior, current):
            c.execute('SAVEPOINT differential')
            result = module.reconcile_school_reviews(c, child, as_of=at)
            rows = [tuple(r) for r in c.execute('SELECT * FROM review_task ORDER BY review_task_id')]
            states.append((result, rows))
            c.execute('ROLLBACK TO differential')
            c.execute('RELEASE differential')
        assert states[0] == states[1]
        c.rollback()
    return states[1][0]


@pytest.mark.parametrize('score', [0, 3])
def test_current_qualified_original_and_withdrawal_are_differentially_identical(school_case, prior, score):
    db, client, headers = school_case[:3]
    _, _, assessment, path, body = answer_and_review(school_case, score=score, submit=False)
    assert compare(db, prior)['created'] == []
    assert client.post(path, headers=headers, json=body).status_code == 200
    result = compare(db, prior)
    assert len(result['retained']) == 1
    assert compare(db, prior, 'child-primary-4')['created'] == []
    response = client.post(path, headers=headers, json={'operation':'retract','expected_revision':1,
        'client_action_id':'index-retract','reason':'Synthetic exact review withdrawal'})
    assert response.status_code == 200, response.text
    assert compare(db, prior)['created'] == []


def test_index_is_disposed_and_next_transaction_rechecks_latest_review(school_case):
    db, client, headers = school_case[:3]
    _, scored, _, path, _ = answer_and_review(school_case)
    evidence = scored['attempt']['evidence_id']
    with db.connect() as c:
        c.execute('BEGIN')
        with current._ReviewCandidates(c, CHILD) as index:
            assert len(index.for_evidence(c, CHILD, evidence)) == 1
            with pytest.raises(current.SchoolReviewError, match='READ_SCOPE_CHANGED'):
                index.for_evidence(c, 'child-primary-4', evidence)
            with db.connect() as other:
                with pytest.raises(current.SchoolReviewError, match='READ_SCOPE_CHANGED'):
                    index.for_evidence(other, CHILD, evidence)
        assert index.rows is None
        c.rollback()
        response = client.post(path, headers=headers, json={'operation':'retract','expected_revision':1,
            'client_action_id':'index-next-transaction-retract','reason':'Synthetic next transaction withdrawal'})
        assert response.status_code == 200, response.text
        c.execute('BEGIN')
        with current._ReviewCandidates(c, CHILD) as fresh:
            assert not fresh.for_evidence(c, CHILD, evidence)
        with pytest.raises(current.SchoolReviewError, match='READ_SCOPE_CHANGED'):
            index.for_evidence(c, CHILD, evidence)
        c.rollback()
        c.execute('BEGIN')
        with pytest.raises(RuntimeError, match='rollback scope'):
            with current._ReviewCandidates(c, CHILD) as failed:
                raise RuntimeError('rollback scope')
        assert failed.rows is None
        c.rollback()


def test_prefilter_never_swallows_db_errors():
    class Broken:
        in_transaction = True
        def execute(self, *args):
            raise sqlite3.OperationalError('synthetic IO fault')
    with pytest.raises(sqlite3.OperationalError, match='synthetic IO fault'):
        with current._ReviewCandidates(Broken(), CHILD):
            pytest.fail('must not enter')


def test_two_http_reviewed_source_roots_rebuild_in_one_transaction(tmp_path, monkeypatch, prior):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.knowledge_base import KnowledgeBaseService
    from home_study_agent.repository import StudyRepository
    from test_open_response_supply_http import prepared
    from test_pet_reviewed_source_actual_http import NoCloud

    monkeypatch.setenv('HSC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('HSC_DATABASE_PATH', str(tmp_path / 'open-supply.db'))
    monkeypatch.setattr('test_pet_media_evidence.CHILD', CHILD)
    db, supply, spec, packet, _ = prepared(tmp_path, '数学')
    texts = ['分析 x²+6x+10=0 的实数根，并写出完整推理。',
             '分析 x²-8x+16=0 的实数根，并说明判断依据。',
             '分析 3x²+2x+4=0 的实数根，并说明判别过程。',
             '分析 2x²-12x+18=0 的实数根，并给出判断过程。']
    source = KnowledgeBaseService(db).import_source(CHILD, dict(
        source_type='school_material', title='Synthetic two independent source roots',
        subject='数学', grade_band='初三', version_label='isolated-open-2026',
        source_ref='isolated:two-roots', license_status='family_owned_index', parent_reviewed=True,
        content_text='\n'.join(json.dumps({**packet, 'prompt': t}, ensure_ascii=False) for t in texts)))
    with db.connect() as c:
        scope = json.loads(c.execute('SELECT scope_json FROM curriculum_supply_selection WHERE child_id=?', (CHILD,)).fetchone()[0])
        excerpts = [r[0] for r in c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (source['source_id'],))]
    supply.select_curriculum(CHILD, {**scope, 'textbook_source_id': source['source_id']},
        actor='parent', action_id='two-roots-scope', expected_version=1)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41820)) as client:
        headers = {'X-HSC-Parent': client.post('/v1/parent/setup', json={'pin': '8264'}).json()['parent_token']}
        base = f'/api/parent/children/{CHILD}/curriculum-supply'
        published = []
        for i, excerpt in enumerate(excerpts):
            stage = 'near_transfer' if i % 2 else 'parallel'
            request = {**spec, 'excerpt_id': excerpt, 'stage': stage,
                       'seed_item_id': f'diag-j3-discriminant-{1 + i // 2}'}
            response = client.post(base + '/generate', headers=headers, json={
                'action_id': f'two-{i}-draft', 'expected_version': 0, 'expected_scope_version': 2, 'source': request})
            assert response.status_code == 200, response.text
            cid = response.json()['candidate_id']
            checked = client.post(f'{base}/{cid}/validate', headers=headers,
                                 json={'action_id': f'two-{i}-validate', 'expected_version': 1})
            assert checked.status_code == 200, checked.text
            body = {'action_id': f'two-{i}-review', 'expected_version': 2,
                    'decision': 'approve', 'note': 'Synthetic original and stage reviewed'}
            if stage == 'near_transfer':
                context = checked.json()['stage_review_context']
                body['stage_review'] = {k: context[k] for k in ('schema', 'binding_version')}
                body['stage_review'].update(stage=stage, source_quote=context['source_prompt'],
                    candidate_quote=context['candidate_prompt'],
                    same_skill_rationale='两题都要求识别条件并用判别式给出可核对的推理。',
                    changed_context_rationale='迁移改变具体系数和结论，不复述原题答案。')
            reviewed = client.post(f'{base}/{cid}/review', headers=headers, json=body)
            assert reviewed.status_code == 200, reviewed.text
            response = client.post(f'{base}/{cid}/publish', headers=headers,
                                   json={'action_id': f'two-{i}-publish', 'expected_version': 3})
            assert response.status_code == 200, response.text
            published.append(response.json())
        sid = StudyRepository(db).start_session(CHILD, 'two-roots-original')['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        origins = []
        for i in (0, 2):
            case = (db, client, headers, sid, published[i])
            _, scored, _, _, _ = answer_and_review(case, key=f'two-root-{i}')
            origins.append(scored['attempt']['evidence_id'])
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            roots = [current._anchor(c, CHILD, e, datetime.now(timezone.utc))['source']['source_root_item_id'] for e in origins]
            assert len(set(roots)) == 2
            # Rebuild only derived schedules inside a rolled-back transaction;
            # original submissions and both actual HTTP approvals stay intact.
            c.execute('DELETE FROM review_task WHERE source_evidence_id IN (?,?)', origins)
            states = []
            for module in (prior, current):
                c.execute('SAVEPOINT rebuild')
                result = module.reconcile_school_reviews(c, CHILD, as_of=datetime.now(timezone.utc).isoformat())
                rows = [tuple(r) for r in c.execute('SELECT source_evidence_id,diagnostic_item_id,interval_label FROM review_task ORDER BY source_evidence_id')]
                assert len(result['created']) == 2
                assert dict((r[0], r[1]) for r in rows) == dict(zip(origins, [published[1]['diagnostic_item_id'], published[3]['diagnostic_item_id']]))
                states.append((result, rows))
                c.execute('ROLLBACK TO rebuild')
                c.execute('RELEASE rebuild')
            assert states[0] == states[1]
            c.rollback()
