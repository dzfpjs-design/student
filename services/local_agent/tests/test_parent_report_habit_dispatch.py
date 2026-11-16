"""Real pending-report host with active habit inputs; loopback-free mock transport.

No clock freezing, synthetic reliability, or mocked authority. Builder spying
only records the returned values so an unstable source can be diagnosed exactly.
"""
import copy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import home_study_agent.app as app_module
import home_study_agent.reports as reports_module
from home_study_agent.behavior_model import BehaviorModelService, ObservableEvent
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.report_source_fingerprint import report_source_fingerprint
from test_report_delivery import _pending_report_job


CHILD = 'child-primary-4'
SID = 'pending-report-session'


def changed_paths(left, right, path=''):
    if isinstance(left, dict) and isinstance(right, dict):
        return [p for key in sorted(set(left) | set(right))
                for p in changed_paths(left.get(key), right.get(key), f'{path}.{key}')]
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            return [path + '.length']
        return [p for i, (a, b) in enumerate(zip(left, right)) for p in changed_paths(a, b, f'{path}[{i}]')]
    return [] if left == right else [path]


def active_habit(db):
    behavior = BehaviorModelService(db); behavior.initialize()
    now = datetime.now(timezone.utc)
    for index, delta in enumerate((timedelta(days=2), timedelta(days=2, minutes=-5), timedelta(days=1))):
        behavior.ingest(ObservableEvent(child_id=CHILD, session_id=SID, event_id=f'habit-seed-{index}',
            occurred_at=(now - delta).isoformat(), event_type='work.stall', source='local.sensor', confidence=.95))
    proposed = behavior.pending_habit_experiment(CHILD)
    assert proposed['status'] == 'proposed'
    current = behavior.respond_habit_experiment(proposed['experiment']['experiment_id'], CHILD, True,
        (now - timedelta(hours=1)).isoformat(), 'accept-habit')
    assert current['status'] == 'active'
    return behavior


@pytest.mark.parametrize('change', ['none', 'observation', 'retraction'])
def test_active_habit_report_retry_ignores_clock_churn_but_rejects_changed_observations(tmp_path, monkeypatch, change):
    db = Database(tmp_path / 'habit-report.db'); db.initialize(); _pending_report_job(db)
    behavior = active_habit(db)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        data_encryption_key=b'H' * 32, model_provider='deepseek', model_api_key='fixture-only',
        model_base_url='https://report.invalid')
    privacy = PrivacyLifecycleService(db, tmp_path); privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    builds, source_versions, posts, mutations = [], [], [], []
    original_builder = reports_module.build_daily_report

    def observe_builder(evidence):
        result = original_builder(evidence)
        if evidence.get('session', {}).get('session_id') == SID:
            builds.append(copy.deepcopy(result))
            with db.connect() as c:
                source_versions.append(report_source_fingerprint(c, SID))
        return result

    monkeypatch.setattr(app_module, 'build_daily_report', observe_builder)
    monkeypatch.setattr(reports_module, 'build_daily_report', observe_builder)

    async def transport(request):
        assert request.url.host == 'report.invalid'
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            if change == 'observation':
                behavior.ingest(ObservableEvent(child_id=CHILD, session_id=SID, event_id='new-observation',
                    occurred_at=datetime.now(timezone.utc).isoformat(), event_type='work.stall', source='local.sensor', confidence=.95))
                mutations.append('observation')
            elif change == 'retraction':
                behavior.retract_event('habit-seed-2', CHILD, 'retract-habit-source', '这条观察需要撤回核对。')
                mutations.append('retraction')
            return httpx.Response(503)
        content = {'scenario': '核对今天留下的一小步。', 'script': ['谢谢你记录下来。', '我们一起核对下一步。'],
            'action': {'what': '核对第一步', 'steps': ['孩子选择一处', '核对后停止']},
            'avoid': [{'do_not_say': '快一点。', 'say_instead': '准备好再开始。'}]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content, ensure_ascii=False)}}]})

    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    with TestClient(app_module.create_app(settings, model_gateway=gateway)):
        pass
    with db.connect() as c:
        sources = [r[0] for r in c.execute('SELECT source FROM daily_report')]
        job = dict(c.execute("SELECT status,attempts,last_error FROM report_job WHERE job_id='pending-report-job'").fetchone())
        calls = [dict(r) for r in c.execute("SELECT status,error_code FROM model_call WHERE purpose='parent_report_coaching'")]
        observation_count = c.execute('SELECT COUNT(*) FROM observable_event WHERE child_id=?', (CHILD,)).fetchone()[0]
        retraction_count = c.execute('SELECT COUNT(*) FROM behavior_retraction WHERE child_id=?', (CHILD,)).fetchone()[0]
        assert c.execute('SELECT COUNT(*) FROM report_delivery_outbox').fetchone()[0] == 0
    paths = changed_paths(builds[0], builds[1]) if len(builds) >= 2 else []
    diagnostic = {'change': change, 'post_count': len(posts), 'mutations': mutations, 'sources': sources, 'job': job, 'calls': calls,
        'build_count': len(builds), 'first_build_diff_paths': paths,
        'first_two_source_versions_equal': len(source_versions) >= 2 and source_versions[0] == source_versions[1],
        'observation_count': observation_count, 'retraction_count': retraction_count}
    (tmp_path / 'habit-dispatch-readout.json').write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2))
    assert calls, diagnostic
    if change == 'none':
        assert observation_count == 3 and retraction_count == 0, diagnostic
        assert len(posts) == 2 and sources == ['cloud_parent_coaching_v1'] and job['status'] == 'completed', diagnostic
    else:
        # Reaching the first POST is mandatory: a clock-churn failure before it
        # cannot masquerade as successful rejection of an observation change.
        assert mutations == [change] and len(posts) == 1, diagnostic
        assert not sources and job['status'] == 'pending', diagnostic
        assert calls[-1]['error_code'] == 'PARENT_REPORT_AUTHORITY_CHANGED', diagnostic
        assert observation_count == (4 if change == 'observation' else 3), diagnostic
        assert retraction_count == (1 if change == 'retraction' else 0), diagnostic
