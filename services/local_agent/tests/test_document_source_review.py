"""Ordinary original -> parent evidence mapping -> child's independent question."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from test_diagnostic_prompt_http import environment, CHILD, ITEM
import test_document_goal_http as original
from test_document_revisit import next_day, offer, say


@contextmanager
def unmatched(environment, monkeypatch):
    monkeypatch.setattr(original, 'BODY', '先计算括号内24与6的和，再把结果乘3。')
    generator = original.journey.__wrapped__(environment, monkeypatch)
    j = next(generator)
    try:
        next_day(j, monkeypatch)
        offer(j)
        say(j, monkeypatch, '先试试原题', 'start', 'original-start')
        result = say(j, monkeypatch, '换一道类似的新题考考我', 'independent_check', 'needs-mapping')
        assert not result['execution']['result'].get('diagnostic_prompt')
        j['cid'] = result['execution']['result']['commitment_id']
        with TestClient(j['client'].app, client=('127.0.0.1', 50500)) as parent:
            parent.headers['X-HSC-Parent'] = parent.post('/v1/parent/unlock', json={'pin':'8264'}).json()['parent_token']
            j['parent'] = parent
            j['path'] = f'/v1/parent/children/{CHILD}/original-checks/{j["cid"]}'
            yield j
    finally:
        generator.close()


def proposal(detail):
    item = next(row for row in detail['references'] if row['item_id'] == ITEM)
    return dict(action_id='review-original-source', version=detail['version'], decision='approve',
        item_id=ITEM, item_version=item['item_version'], original_quote='括号内24与6的和',
        reference_quote=item['target'], note='原题要求先算括号再乘法，关联这个运算顺序目标；尚未判定孩子是否掌握。')


def test_parent_mapping_is_saved_and_child_can_resume_without_a_mistake_label(environment, monkeypatch):
    with unmatched(environment, monkeypatch) as j:
        queue = j['parent'].get('/v1/parent/work-queue', params={'child_id':CHILD,'limit':50})
        assert queue.status_code == 200, queue.text
        assert any(row['object_id'] == j['cid'] and row['target']['panel'] == 'parent-work-original-check' for row in queue.json()['items'])
        response = j['parent'].get(j['path'])
        assert response.status_code == 200, response.text
        detail = response.json()
        assert detail['original_question'] == original.BODY
        payload = proposal(detail)
        saved = j['parent'].post(j['path'], json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()['current']['mapping']['decision'] == 'approved'
        assert saved.json()['current']['status'] == 'ready'
        receipt = j['parent'].get(j['path'] + '/receipts/' + payload['action_id'])
        assert receipt.status_code == 200 and receipt.json()['receipt'] == saved.json()['receipt']
        j['parent'].__exit__(None, None, None)
        j['client'].__exit__(None, None, None)
        with TestClient(create_app(j['settings'], model_gateway=j['gateway']), client=('127.0.0.1',50501)) as restarted:
            j['client'] = restarted
            restarted.headers['X-HSC-Parent'] = restarted.post('/v1/parent/unlock',json={'pin':'8264'}).json()['parent_token']
            assert restarted.get(j['path']).json()['mapping']['note'] == payload['note']
            del restarted.headers['X-HSC-Parent']
            result = say(j, monkeypatch, '现在用另一道类似题来考我吧', 'independent_check', 'mapped-new-check')
            prompt = result['execution']['result']['diagnostic_prompt']['prompt']
            selected = next(row for row in detail['references'] if row['item_id'] == payload['item_id'])
            assert prompt['item']['knowledge_node_id'] == selected['node_id']
            with j['db'].connect() as c:
                assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0
                assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 0
            assert original.BODY not in json.dumps(j['calls'][-1], ensure_ascii=False)


@pytest.mark.parametrize('change', ['original', 'reference', 'quote', 'child'])
def test_stale_or_ungrounded_approval_cannot_grant_source_mapping(environment, monkeypatch, change):
    with unmatched(environment, monkeypatch) as j:
        detail = j['parent'].get(j['path'])
        assert detail.status_code == 200, detail.text
        payload = proposal(detail.json())
        path = j['path']
        if change == 'quote':
            payload['original_quote'] = '并不存在的原题证据'
        elif change == 'child':
            path = path.replace(CHILD, 'other-child')
        else:
            with j['db'].connect() as c:
                if change == 'original':
                    c.execute("UPDATE capture_artifact SET extracted_text='changed' WHERE artifact_id=?",(j['source']['artifact_id'],))
                else:
                    c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?",(ITEM,))
                c.commit()
        response = j['parent'].post(path, json=payload)
        assert response.status_code in {404,409}, response.text
        with j['db'].connect() as c:
            payload = json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])
            assert not payload.get('independent_mapping')


def test_revocation_invalidates_pending_question_and_history_survives(environment, monkeypatch):
    with unmatched(environment, monkeypatch) as j:
        detail = j['parent'].get(j['path'])
        assert detail.status_code == 200, detail.text
        approved = j['parent'].post(j['path'], json=proposal(detail.json()))
        assert approved.status_code == 200, approved.text
        check = say(j,monkeypatch,'换道新题考我','independent_check','prepared-mapped-check')
        prompt = check['execution']['result']['diagnostic_prompt']['prompt']
        current = j['parent'].get(j['path']).json()
        revoked = j['parent'].post(j['path'],json=dict(action_id='revoke-source',version=current['version'],decision='revoke',
            item_id=None,item_version=None,original_quote='',reference_quote='',note='重新核对原题关联，先撤销这次目标映射。'))
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()['current']['mapping']['decision'] == 'revoked'
        assert j['client'].get(f'/v1/sessions/{j["sid"]}/diagnostic-prompt').json()['status'] == 'invalidated'
        assert j['parent'].get(j['path']+'/receipts/review-original-source').json()['receipt']['decision'] == 'approved'


def test_parent_auth_locality_replay_and_optimistic_version(environment, monkeypatch):
    with unmatched(environment, monkeypatch) as j:
        detail = j['parent'].get(j['path']).json()
        payload = proposal(detail)
        token = j['parent'].headers.pop('X-HSC-Parent')
        assert j['parent'].get(j['path']).status_code == 401
        assert j['parent'].post(j['path'], json=payload).status_code == 401
        j['parent'].headers['X-HSC-Parent'] = token
        with TestClient(j['client'].app, client=('192.0.2.1',50501)) as remote:
            remote.headers['X-HSC-Parent'] = token
            assert remote.get(j['path']).status_code == 403
        saved = j['parent'].post(j['path'],json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.headers['Cache-Control'] == 'no-store'
        replay = j['parent'].post(j['path'],json=payload)
        assert replay.status_code == 200 and replay.json()['replayed']
        assert replay.json()['receipt'] == saved.json()['receipt']
        assert j['parent'].post(j['path'],json={**payload,'note':'同一操作编号不允许更换这段审核依据。'}).status_code == 409
        assert j['parent'].post(j['path'],json={**payload,'action_id':'stale-version'}).status_code == 409
        with j['db'].connect() as c:
            current=json.loads(c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()[0])
            assert len(current['independent_mapping']['events']) == 1
            assert c.execute('SELECT count(*) FROM learning_evidence').fetchone()[0] == 0


def test_original_queue_remains_visible_if_unrelated_curriculum_source_unavailable(environment, monkeypatch):
    with unmatched(environment,monkeypatch) as j:
        from home_study_agent.parent_work_queue import ParentWorkQueueService
        original_read = ParentWorkQueueService._read

        def unavailable_curriculum(self, connection, child, kind):
            if kind == 'curriculum':
                raise sqlite3.OperationalError('simulated unrelated source outage')
            yield from original_read(self, connection, child, kind)

        monkeypatch.setattr(ParentWorkQueueService, '_read', unavailable_curriculum)
        result = j['parent'].get('/v1/parent/work-queue',params={'child_id':CHILD,'limit':50}).json()
        assert any(s['kind']=='curriculum' and s['status']=='unavailable' for s in result['sources'])
        assert any(s['kind']=='original_question' and s['status']=='ready' and s['total']==1 for s in result['sources'])
        assert result['total'] is None
        assert any(row['object_id']==j['cid'] for row in result['items'])


@pytest.mark.parametrize('scope',['learning','media','tutor'])
def test_mapping_history_exports_with_original_and_is_erased_with_its_scope(environment,monkeypatch,scope):
    with unmatched(environment,monkeypatch) as j:
        payload=proposal(j['parent'].get(j['path']).json())
        saved=j['parent'].post(j['path'],json=payload)
        assert saved.status_code==200,saved.text
        privacy=j['client'].app.state.privacy_lifecycle
        exported=privacy.export_child(CHILD)
        path=Path(exported['path'])
        assert path.is_relative_to(j['settings'].data_dir)
        data=json.loads(path.read_text())
        record=next(row for row in data['agent_commitments'] if row['commitment_id']==j['cid'])
        mapping=json.loads(record['payload_json'])['independent_mapping']
        assert mapping['events'][0]['receipt']==saved.json()['receipt']
        assert mapping['current']['note']==payload['note']
        privacy.delete_child_scope(CHILD,scope,'确认删除','erase-source-'+scope)
        with j['db'].connect() as c:
            row=c.execute('SELECT payload_json FROM agent_commitment WHERE commitment_id=?',(j['cid'],)).fetchone()
            if row:
                assert 'independent_mapping' not in json.loads(row[0])
                assert not json.loads(row[0]).get('practice',{}).get('independent_request')
        assert not path.exists()
        assert j['parent'].get(j['path']).status_code in {404,409}
