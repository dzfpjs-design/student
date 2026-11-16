from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import asyncio
from types import SimpleNamespace
from normal_provider_owner import normal_provider_scope
import httpx

import pytest
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.realtime_output_authority import RealtimeOutputAuthority, RealtimeOutputConflict, DOMAIN, FIELDS

KEY = 'ab' * 32  # Synthetic test key, never a deployment credential.


def seed_projection(db, scope, *, text='我们接着看下一步。', context=None, mutate=None, action_id='synthetic_output_action'):
    """Exercise the actual Gateway parser with a synthetic HTTP transport."""
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway, ModelRequest, ModelResponseError
    from home_study_agent.public_reply_projection import approved_routes, record_candidate, bind
    settings = Settings(data_dir=db.path.parent, database_path=db.path, model_api_key='synthetic-key')
    repo = StudyRepository(db)
    messages = ({'role': 'user', 'content': '合成公共问候'},)
    body = {'spoken_transition': text, 'next_step': {'arguments': {'response': text}}}
    content = json.dumps(body, ensure_ascii=False)
    payload = {'id': 'synthetic_provider_reply', 'model': settings.voice_intent_model_name,
               'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
    if mutate: mutate(payload)
    gateway = ModelGateway(settings, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
    call_id = repo.start_model_call(scope['session_id'], scope['child_id'], 'agent_plan', settings.model_provider,
        settings.voice_intent_model_name, 6, 'synthetic_prompt_v1', {}, 'medium', settings.model_provider, 'synthetic_policy_v1', None)
    request = ModelRequest(call_id, scope['child_id'], scope['session_id'], 'agent_plan', 'medium', {},
        settings.model_provider, 'synthetic_policy_v1', 'synthetic_prompt_v1', None, 'dialogue', (), messages)
    async def run():
        try: return await gateway.complete(request)
        finally: await gateway.close()
    try:
        with normal_provider_scope():
            result = asyncio.run(run())
    except ModelResponseError as error:
        repo.finish_model_call(call_id, 'failed', error.latency_ms, None,
            error.prompt_tokens, error.completion_tokens, error_code=error.code,
            verification_status='rejected', safety_status='degraded')
        raise
    repo.finish_model_call(call_id, 'completed', result.latency_ms, len(result.content),
        provider=result.provider, model=result.model, verification_status='verified', safety_status='passed')
    decision = SimpleNamespace(spoken_transition=text, step=SimpleNamespace(
        capability=SimpleNamespace(name='conversation.respond'), arguments={'response': text}))
    with db.connect() as c:
        reference = record_candidate(c, call_id=call_id, action_id=action_id, result=result, decision=decision,
            messages=messages, context=context or {}, known_child_names=[], routes=approved_routes(settings))
        if reference: bind(c, scope['agent_turn_id'], reference)
        c.commit()
    return approved_routes(settings), reference


def proof(scope, operation='reserve', response_id='', key=KEY, issued_at=None, **patch):
    command = dict(scope, operation=operation, response_id=response_id,
                   issued_at=issued_at or datetime.now(timezone.utc).isoformat(), **patch)
    signature = hmac.new(bytes.fromhex(key), json.dumps([DOMAIN] + [command[f] for f in FIELDS] +
                         ([command['generation_evidence']] if 'generation_evidence' in command else []),
                         ensure_ascii=False, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    return dict(command, signature=signature)


@pytest.fixture
def case(tmp_path):
    db = Database(tmp_path / 'synthetic-output.db'); db.initialize()
    repo = StudyRepository(db); repo.seed_family()
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    PrivacyLifecycleService(db, tmp_path).initialize()
    from home_study_agent.learning_input_revision import LearningInputRevisionService
    arrivals = LearningInputRevisionService(db); arrivals.initialize()
    sid = repo.start_session('child-primary-4', 'synthetic-start-output')['session_id']
    scope = dict(session_id=sid, child_id='child-primary-4', agent_turn_id='synthetic_turn_output',
                 connection_id='synthetic_connection', call_id='synthetic_call', voice='Tina', permit_id='synthetic_permit')
    with db.connect() as c:
        scope['storage_generation'] = c.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()[0]
        c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=1 WHERE singleton_id=1')
        c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
          orchestrator_version,tool_status,completed_at,created_at,client_action_id) VALUES(?,?,?,?,?,?,?,'synthetic','completed',?,?,?)''',
          (scope['agent_turn_id'], scope['child_id'], sid, '合成输入', 'conversation', '我们接着看下一步。', '{}',
           '2026-10-05T00:00:00Z', '2026-10-05T00:00:00Z', 'synthetic_output_action'))
        # Use the real durable arrival helper rather than copying its schema.
        arrivals.capture_arrival(sid, 'synthetic_output_action', '合成输入', connection=c)
        c.commit()
    routes, reference = seed_projection(db, scope)
    assert reference
    return db, scope, RealtimeOutputAuthority(db, KEY, approved_model_routes=routes)


def test_reserve_bind_and_restart_readback_do_not_repeat_cloud_dispatch(case):
    db, scope, service = case
    reserved = service.execute(proof(scope))
    assert reserved['response_text'] == '我们接着看下一步。'
    assert reserved['input_source'] == {'client_action_id': 'synthetic_output_action', 'voice_input_id': None}
    bound = service.execute(proof(scope, 'bind', 'resp_synthetic_output'))
    assert bound['dispatch_allowed'] is True and bound['rendered_completed'] is False
    with pytest.raises(RealtimeOutputConflict, match='ALREADY_BOUND'):
        service.execute(proof(scope, 'bind', 'resp_synthetic_output'))
    restarted = RealtimeOutputAuthority(Database(db.path), KEY, approved_model_routes=service.approved_model_routes)
    result = restarted.execute(proof(scope, 'inspect'))
    assert result['state'] == 'bound' and result['dispatch_allowed'] is False and 'response_text' not in result
    with pytest.raises(RealtimeOutputConflict, match='ALREADY_RESERVED'):
        restarted.execute(proof(dict(scope, call_id='another_call_id', permit_id='another_permit_id')))
    assert restarted.execute(proof(scope, 'retire'))['state'] == 'retired'
    assert restarted.execute(proof(scope, 'retire'))['state'] == 'retired'
    assert restarted.execute(proof(scope, 'inspect'))['dispatch_allowed'] is False
    with db.connect() as c:
        rows = c.execute("SELECT summary_json FROM audit_event WHERE actor=?", (DOMAIN,)).fetchall()
        assert len(rows) == 3
        assert all('合成输入' not in r[0] and '我们接着' not in r[0] and 'signature' not in r[0] for r in rows)


@pytest.mark.parametrize('mutation', ['source_missing', 'source_changed', 'memory', 'pending', 'wrong_child'])
def test_source_authority_is_not_a_renderer_permission_flag(case, mutation):
    db, scope, service = case
    with db.connect() as c:
        if mutation == 'source_missing': c.execute("DELETE FROM audit_event WHERE actor='public_reply_projection_v1'")
        if mutation == 'source_changed': c.execute("UPDATE agent_turn SET response_text='本机拼接内容'")
        if mutation == 'memory': c.execute("UPDATE agent_turn SET intent='memory_command'")
        if mutation == 'pending': c.execute("UPDATE agent_turn SET tool_status='pending'")
        c.commit()
    command = proof(dict(scope, child_id='child-junior-3') if mutation == 'wrong_child' else scope)
    with pytest.raises(RealtimeOutputConflict): service.execute(command)
    with db.connect() as c: assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['response', 'permission', 'session', 'epoch', 'expiry'])
def test_changed_source_or_authority_revokes_without_another_text_export(case, mutation):
    db, scope, service = case
    service.execute(proof(scope))
    with db.connect() as c:
        if mutation == 'response': c.execute("UPDATE agent_turn SET response_text='修改后的合成回答。'")
        if mutation == 'permission': c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        if mutation == 'session': c.execute("UPDATE daily_session SET status='cancelled'")
        c.commit()
    if mutation == 'epoch': service = RealtimeOutputAuthority(db, 'cd' * 32, approved_model_routes=service.approved_model_routes)
    if mutation == 'expiry': service.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=121)
    result = service.execute(proof(scope, 'inspect', key=service.key, issued_at=service.clock().isoformat()))
    assert result['state'] == 'revoked' and result['dispatch_allowed'] is False and 'response_text' not in result
    assert service.execute(proof(scope, 'bind', 'resp_synthetic_output', key=service.key,
                                issued_at=service.clock().isoformat()))['dispatch_allowed'] is False


def test_unsigned_changed_or_unexpected_body_cannot_reserve(case):
    _, scope, service = case
    for body in [proof(scope) | {'cloud_transmission_allowed': True}, proof(scope) | {'signature': 'x'},
                 proof(scope) | {'call_id': 'another_call_id'}, proof(scope, issued_at='2020-01-01T00:00:00Z')]:
        with pytest.raises(RealtimeOutputConflict): service.execute(body)


def test_public_projection_does_not_unprotect_original_turn(case):
    db, scope, service = case
    protected = json.dumps({'local_only': True, 'cloud_transmission_allowed': False})
    with db.connect() as c:
        c.execute('UPDATE agent_turn SET action_json=?', (protected,)); c.commit()
    reserved = service.execute(proof(scope))
    assert reserved['local_only'] is False and reserved['cloud_transmission_allowed'] is True
    assert reserved['generation_completed'] is reserved['rendered_completed'] is False
    with db.connect() as c:
        assert c.execute('SELECT action_json FROM agent_turn').fetchone()[0] == protected
        assert c.execute("SELECT count(*) FROM audit_event WHERE actor='public_reply_projection_v1'").fetchone()[0] == 2


def test_public_reply_export_preserves_exact_source_whitespace(case):
    db, scope, service = case
    text = ' 我们接着看下一步。 '
    with db.connect() as c:
        c.execute("DELETE FROM audit_event WHERE actor='public_reply_projection_v1'")
        c.execute('UPDATE agent_turn SET response_text=?', (text,)); c.commit()
    routes, reference = seed_projection(db, scope, text=text)
    assert reference
    assert service.execute(proof(scope))['response_text'] == text


@pytest.mark.parametrize('mutation', ['pcm_size', 'supplier_identity', 'text_hash', 'signature', 'changed_receipt'])
def test_generation_witness_rejects_changed_samples_origin_text_or_signature(case, mutation):
    db, scope, service = case
    service.execute(proof(scope)); service.execute(proof(scope, 'bind', 'resp_synthetic_output'))
    evidence = dict(sample_count=2400, pcm_bytes=4800, frame_count=1, pcm_sha256=hashlib.sha256(bytes(4800)).hexdigest(),
        text_sha256=hashlib.sha256('我们接着看下一步。'.encode()).hexdigest(), model='qwen3.8-omni-flash-realtime', voice='Tina',
        supplier_session_id='synthetic_supplier_session', supplier_tool_response_id='resp_synthetic_tool', supplier_tool_call_id='synthetic_supplier_call')
    if mutation == 'changed_receipt':
        service.execute(proof(scope, 'generated', 'resp_synthetic_output', generation_evidence=evidence))
        evidence['pcm_sha256'] = 'f' * 64
    if mutation == 'pcm_size': evidence['pcm_bytes'] = 4801
    if mutation == 'supplier_identity': evidence['supplier_tool_call_id'] = ''
    if mutation == 'text_hash': evidence['text_sha256'] = 'f' * 64
    command = proof(scope, 'generated', 'resp_synthetic_output', generation_evidence=evidence)
    if mutation == 'signature': command['generation_evidence']['pcm_sha256'] = 'f' * 64
    with pytest.raises(RealtimeOutputConflict): service.execute(command)
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM audit_event WHERE action='realtime_output_rendered'").fetchone()[0] == 0


@pytest.mark.parametrize('terminal', ['rendered', 'interrupted', 'unconfirmed'])
def test_sample_generation_and_render_delivery_are_durable_separate_facts(case, terminal):
    db, scope, service = case
    service.execute(proof(scope))
    service.execute(proof(scope, 'bind', 'resp_synthetic_output'))
    with pytest.raises(RealtimeOutputConflict, match='ORDER_INVALID'):
        service.execute(proof(scope, 'rendered', 'resp_synthetic_output'))
    evidence = dict(sample_count=2400, pcm_bytes=4800, frame_count=1, pcm_sha256=hashlib.sha256(bytes(4800)).hexdigest(),
        text_sha256=hashlib.sha256('我们接着看下一步。'.encode()).hexdigest(), model='qwen3.8-omni-flash-realtime', voice='Tina',
        supplier_session_id='synthetic_supplier_session', supplier_tool_response_id='resp_synthetic_tool', supplier_tool_call_id='synthetic_supplier_call')
    generated = service.execute(proof(scope, 'generated', 'resp_synthetic_output', generation_evidence=evidence))
    assert generated['generation_completed'] is True and generated['rendered_completed'] is False
    service.execute(proof(scope, 'started', 'resp_synthetic_output'))
    settled = service.execute(proof(scope, terminal, 'resp_synthetic_output'))
    assert settled['rendered_completed'] is (terminal == 'rendered')
    restarted = RealtimeOutputAuthority(Database(db.path), KEY, approved_model_routes=service.approved_model_routes)
    status = restarted.execute(proof(scope, 'inspect'))
    assert status['state'] == 'retired' and status['delivery_status'] == terminal
    assert status['rendered_completed'] is (terminal == 'rendered') and status['dispatch_allowed'] is False
    # A late ended event cannot promote an interrupted/unknown attempt.
    late = restarted.execute(proof(scope, 'rendered', 'resp_synthetic_output'))
    assert late['delivery_status'] == terminal
    with pytest.raises(RealtimeOutputConflict, match='ALREADY_RESERVED'):
        restarted.execute(proof(scope))
    with db.connect() as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert c.execute("SELECT count(*) FROM audit_event WHERE action=?", ('realtime_output_' + terminal,)).fetchone()[0] == 1


@pytest.mark.parametrize('mutation', ['truncated', 'unknown_finish', 'no_provider_identity', 'no_model_version'])
def test_incomplete_or_unidentified_transport_reply_has_no_export_projection(case, mutation):
    db, scope, service = case
    with db.connect() as c:
        c.execute("DELETE FROM audit_event WHERE actor='public_reply_projection_v1'"); c.commit()
    def mutate(payload):
        if mutation == 'truncated': payload['choices'][0]['finish_reason'] = 'length'
        if mutation == 'unknown_finish': payload['choices'][0].pop('finish_reason')
        if mutation == 'no_provider_identity': payload.pop('id')
        if mutation == 'no_model_version': payload.pop('model')
    if mutation == 'truncated':
        from home_study_agent.model_gateway import ModelResponseError
        with pytest.raises(ModelResponseError, match='^MODEL_OUTPUT_TRUNCATED$') as rejected:
            seed_projection(db, scope, mutate=mutate)
        assert rejected.value.response_received is True
        assert rejected.value.fallback_eligible is False
        with db.connect() as c:
            failed = c.execute('SELECT status,error_code,output_chars FROM model_call ORDER BY rowid DESC LIMIT 1').fetchone()
            assert dict(failed) == {'status': 'failed', 'error_code': 'MODEL_OUTPUT_TRUNCATED', 'output_chars': None}
    else:
        _, reference = seed_projection(db, scope, mutate=mutate)
        assert reference is None
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM audit_event WHERE actor='public_reply_projection_v1'").fetchone()[0] == 0
    with pytest.raises(RealtimeOutputConflict, match='PUBLIC_SOURCE_INVALID'): service.execute(proof(scope))


@pytest.mark.parametrize('mutation', ['turn_text', 'turn_input', 'call_owner', 'call_status', 'call_version', 'route', 'corrupt_projection'])
def test_export_rechecks_origin_version_owner_and_config(case, mutation):
    db, scope, service = case
    service.execute(proof(scope))
    with db.connect() as c:
        if mutation == 'turn_text': c.execute("UPDATE agent_turn SET response_text=response_text||'本地拼接'")
        if mutation == 'turn_input': c.execute("UPDATE agent_turn SET transcript='另一句原话'")
        if mutation == 'call_owner': c.execute("UPDATE model_call SET child_id='child-junior-3'")
        if mutation == 'call_status': c.execute("UPDATE model_call SET status='failed'")
        if mutation == 'call_version': c.execute("UPDATE model_call SET prompt_version='another_version'")
        if mutation == 'corrupt_projection': c.execute("UPDATE audit_event SET summary_json='{}' WHERE actor='public_reply_projection_v1'")
        c.commit()
    if mutation == 'route': service.approved_model_routes = frozenset()
    result = service.execute(proof(scope, 'bind', 'resp_synthetic_output'))
    assert result['state'] == 'revoked' and result['dispatch_allowed'] is False


def test_model_cannot_declare_local_memory_public(case):
    db, scope, service = case
    with db.connect() as c:
        c.execute("DELETE FROM audit_event WHERE actor='public_reply_projection_v1'"); c.commit()
    _, reference = seed_projection(db, scope, context={'local_only': True,
        'response_text': '我们接着看下一步。', 'public': True})
    assert reference is None
    with pytest.raises(RealtimeOutputConflict, match='PUBLIC_SOURCE_INVALID'): service.execute(proof(scope))


def test_equal_reply_from_another_input_cannot_be_rebound(case):
    db, scope, _ = case
    from home_study_agent.learning_input_revision import LearningInputRevisionService
    from home_study_agent.public_reply_projection import bind
    arrivals = LearningInputRevisionService(db)
    with db.connect() as c:
        arrivals.capture_arrival(scope['session_id'], 'another_synthetic_action', '另一句输入', connection=c)
        c.execute("DELETE FROM audit_event WHERE actor='public_reply_projection_v1'"); c.commit()
    _, reference = seed_projection(db, scope, action_id='another_synthetic_action')
    assert reference
    with db.connect() as c:
        assert bind(c, scope['agent_turn_id'], reference) is False
        assert c.execute("SELECT count(*) FROM audit_event WHERE audit_id=?",
                         ('public-reply-turn-' + scope['agent_turn_id'],)).fetchone()[0] == 0


def test_restored_receipt_cannot_revive_an_old_output_permission(case):
    db, scope, service = case
    service.execute(proof(scope))
    from home_study_agent.privacy_operation_receipt import preserve_live_identities
    with db.connect() as c:
        preserve_live_identities(c, c)
        c.commit()
    result = service.execute(proof(scope, 'bind', 'resp_synthetic_output'))
    assert result['state'] == 'revoked' and result['dispatch_allowed'] is False


def test_backup_before_reservation_cannot_reexport_the_original_turn(case, tmp_path):
    from home_study_agent.privacy_operation_receipt import preserve_live_identities
    db, scope, service = case
    backup = Database(tmp_path / 'synthetic-before-reservation.db')
    with db.connect() as live, backup.connect() as staged:
        live.backup(staged)
    service.execute(proof(scope))
    with db.connect() as live, backup.connect() as staged:
        preserve_live_identities(live, staged)
        staged.commit()
        assert staged.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    restored = RealtimeOutputAuthority(backup, KEY, approved_model_routes=service.approved_model_routes)
    with pytest.raises(RealtimeOutputConflict, match='STORAGE_CHANGED'):
        restored.execute(proof(scope))
    with backup.connect() as c:
        assert c.execute('SELECT count(*) FROM audit_event WHERE actor=?', (DOMAIN,)).fetchone()[0] == 0


def test_two_concurrent_reservations_get_exactly_one_original_opportunity(case):
    _, scope, service = case
    def reserve(_):
        try: return service.execute(proof(scope))['state']
        except RealtimeOutputConflict as error: return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(reserve, range(2)))
    assert sorted(results) == ['REALTIME_OUTPUT_ALREADY_RESERVED', 'reserved']


@pytest.mark.parametrize('mutation', ['local_flag_string', 'cloud_flag_null', 'malformed_receipt', 'response_mismatch'])
def test_uncertain_flags_or_corrupt_receipts_do_not_grant_authority(case, mutation):
    db, scope, service = case
    if mutation.endswith('receipt') or mutation == 'response_mismatch':
        service.execute(proof(scope))
        if mutation == 'response_mismatch':
            service.execute(proof(scope, 'bind', 'resp_synthetic_output'))
        else:
            with db.connect() as c:
                c.execute("UPDATE audit_event SET summary_json='[1]' WHERE actor=?", (DOMAIN,)); c.commit()
        with pytest.raises(RealtimeOutputConflict):
            service.execute(proof(scope, 'inspect', 'resp_another_output' if mutation == 'response_mismatch' else ''))
    else:
        with db.connect() as c:
            action = {'local_only': 'true'} if mutation == 'local_flag_string' else {'cloud_transmission_allowed': None}
            c.execute('UPDATE agent_turn SET action_json=?', (json.dumps(action),)); c.commit()
        with pytest.raises(RealtimeOutputConflict): service.execute(proof(scope))
