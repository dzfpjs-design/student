from normal_provider_owner import normal_provider_owner
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway, ModelRequest, ModelResult, build_output_envelope


def request_for(
    messages: list[dict[str, str]],
    request_id: str = "request-test-001",
    request_type: str = "voice_interaction",
) -> ModelRequest:
    return ModelRequest(
        request_id=request_id,
        child_id="child-test",
        session_id="session-test",
        request_type=request_type,
        risk_level="medium",
        context_manifest={"object_types": ["active_plan_item"]},
        provider_preference="qwen",
        policy_version="tutor_safety_v2",
        prompt_version="tutor_layered_v2",
        knowledge_version="local_knowledge_catalog_v1",
        output_type="tutoring",
        evidence_refs=("plan-item-test",),
        messages=tuple(messages),
    )


@pytest.mark.parametrize('route', ['complete', 'complete_on_provider', 'complete_on_fallback'])
def test_local_handoff_recovery_cannot_reach_any_real_gateway_transport(tmp_path, route):
    from home_study_agent.agent_request_ownership import execution_scope, LocalHandoffDispatchDenied
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500, request=request)
    gateway = ModelGateway(Settings(data_dir=tmp_path, database_path=tmp_path/'test.db',
        model_api_key='synthetic-key'), transport=httpx.MockTransport(handler))
    request = request_for([{'role': 'user', 'content': 'synthetic'}])
    try:
        with execution_scope(False) as authority:
            with pytest.raises(LocalHandoffDispatchDenied):
                args = ('qwen', request) if route == 'complete_on_provider' else (request,)
                asyncio.run(getattr(gateway, route)(*args))
            assert not authority.external_started
        assert calls == []
    finally:
        asyncio.run(gateway.close())


def test_gateway_retries_one_transient_server_error(normal_provider_owner) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "先找出已知条件。"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            },
        )

    settings = Settings(
        data_dir=Path("."),
        database_path=Path("test.db"),
        model_api_key="test-key",
        model_timeout_seconds=15,
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(gateway.complete(request_for([{"role": "user", "content": "提示"}])))
    finally:
        asyncio.run(gateway.close())

    assert calls == 2
    assert result.content == "先找出已知条件。"
    assert result.prompt_tokens == 10


def test_dedicated_explanation_key_is_only_used_for_teaching(tmp_path, normal_provider_owner):
    calls = []

    def handler(request):
        calls.append((str(request.url), request.headers['authorization'], json.loads(request.content)))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}]})

    gateway = ModelGateway(Settings(
        data_dir=tmp_path, database_path=tmp_path/'test.db',
        model_provider='qwen', model_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1', model_name='qwen3.7-plus',
        model_api_key='synthetic-general-key', tutor_explanation_api_key='synthetic-teaching-key',
        tutor_explanation_base_url='https://tutor.example.test:7791/v1',
    ), transport=httpx.MockTransport(handler))
    teaching = ('tutor_hint_level_1', 'tutor_whole_explanation', 'tutor_whole_explanation_check', 'mistake_learning_analyze',
                'mistake_learning_practice', 'mistake_learning_coach', 'mistake_learning_verify', 'mistake_learning_topic',
                'mistake_variant_generation')
    general = ('voice_intent', 'paper_scoring_authority_draft', 'learning_outcome_evaluation', 'mistake_learning_answer')

    async def exercise():
        try:
            for purpose in (*teaching, *general):
                result = await gateway.complete(request_for([{'role': 'user', 'content': 'synthetic'}], request_type=purpose))
                assert result.provider == ('deepseek' if purpose in teaching else 'qwen')
                assert not result.fallback_used
        finally:
            await gateway.close()

    asyncio.run(exercise())
    for url, authorization, payload in calls[:len(teaching)]:
        assert url == 'https://tutor.example.test:7791/v1/chat/completions'
        assert authorization == 'Bearer synthetic-teaching-key'
        assert payload['model'] == 'deepseek-v4.1-flash'
        assert payload['thinking'] == {'type': 'enabled'}
        assert payload['response_format'] == {'type': 'json_object'}
    for url, authorization, _ in calls[len(teaching):]:
        assert url == 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
        assert authorization == 'Bearer synthetic-general-key'


def test_gateway_routes_voice_intent_to_its_low_latency_model(normal_provider_owner) -> None:
    requested_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_payloads.append(json.loads(request.read()))
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": '{"intent":"supportive_chat","confidence":0.91}'}}]},
        )

    settings = Settings(
        data_dir=Path("."),
        database_path=Path("test.db"),
        model_api_key="test-key",
        model_name="qwen-tutor",
        voice_intent_model_name="qwen-intent-fast",
        tutor_explanation_api_key='synthetic-teaching-key',
        tutor_explanation_base_url='https://tutor.example.test:7791/v1',
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        intent_result = asyncio.run(gateway.complete(request_for(
            [{"role": "user", "content": "开始吧"}],
            request_type="voice_agent_intent",
        )))
        tutor_result = asyncio.run(gateway.complete(request_for(
            [{"role": "user", "content": "提示"}],
            request_type="tutor_hint_level_1",
        )))
        companion_result = asyncio.run(gateway.complete(request_for(
            [{"role": "user", "content": "今天的云像一艘船"}],
            request_type="companion_dialogue",
        )))
    finally:
        asyncio.run(gateway.close())

    assert requested_payloads[0]["model"] == "qwen-intent-fast"
    assert requested_payloads[0]["max_tokens"] == 80
    assert requested_payloads[0]["enable_thinking"] is False
    assert requested_payloads[1]["model"] == "deepseek-v4.1-flash"
    assert requested_payloads[1]["max_tokens"] == 12288
    assert requested_payloads[1]["response_format"] == {"type": "json_object"}
    assert requested_payloads[1]["thinking"] == {"type": "enabled"}
    assert requested_payloads[2]["model"] == "qwen-tutor"
    assert requested_payloads[2]["max_tokens"] == 160
    assert requested_payloads[2]["enable_thinking"] is False
    assert intent_result.model == "qwen-intent-fast"
    assert tutor_result.model == "deepseek-v4.1-flash"
    assert companion_result.model == "qwen-tutor"


def test_gateway_uses_separate_intent_key_only_for_low_latency_requests(normal_provider_owner):
    requests = []
    def handler(request):
        requests.append((json.loads(request.read())['model'], request.headers.get('Authorization')))
        return httpx.Response(200, request=request, json={'choices':[{'message':{'content':'synthetic response'}}]})
    settings = Settings(data_dir=Path('.'), database_path=Path('test.db'),
        model_api_key='synthetic-primary-key', model_name='deepseek-test',
        voice_intent_model_name='qwen-intent-test', voice_intent_api_key='synthetic-intent-key',
        tutor_explanation_api_key='synthetic-teaching-key', tutor_explanation_base_url='https://tutor.example.test:7791/v1')
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        asyncio.run(gateway.complete(request_for([{'role':'user','content':'synthetic'}],request_type='voice_agent_intent')))
        asyncio.run(gateway.complete(request_for([{'role':'user','content':'synthetic'}],request_type='tutor_hint_level_1')))
    finally:
        asyncio.run(gateway.close())
    assert requests == [('qwen-intent-test','Bearer synthetic-intent-key'),('deepseek-v4.1-flash','Bearer synthetic-teaching-key')]


def test_gateway_requests_large_json_output_for_atomic_extraction(normal_provider_owner) -> None:
    requested_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_payloads.append(json.loads(request.read()))
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": '{"definitions":[],"relations":[]}'}}]},
        )

    settings = Settings(
        data_dir=Path("."), database_path=Path("test.db"), model_provider="deepseek",
        model_api_key="test-key", model_base_url="https://api.deepseek.com",
        model_name="deepseek-v4-pro",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        asyncio.run(gateway.complete(request_for(
            [{"role": "user", "content": "请只输出JSON"}],
            request_type="atomic_knowledge_extraction",
        )))
    finally:
        asyncio.run(gateway.close())

    assert requested_payloads[0]["max_tokens"] == 10000
    assert requested_payloads[0]["response_format"] == {"type": "json_object"}
    assert requested_payloads[0]["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in requested_payloads[0]


def test_whole_explanation_thinks_without_slowing_voice_intent_or_other_tutor_calls():
    from types import SimpleNamespace
    official=SimpleNamespace(provider='deepseek',base_url='https://api.deepseek.com',model='deepseek-flash')
    messages=[{'role':'user','content':'核对完整解释，只输出JSON。'}]
    for kind in ('tutor_whole_explanation','tutor_whole_explanation_check'):
        payload=ModelGateway.request_payload(official,messages,kind)
        assert payload['thinking']=={'type':'enabled'}
        assert payload['reasoning_effort']=='high'
        assert 'temperature' not in payload and 'enable_thinking' not in payload
        assert payload['max_tokens']==4096 and payload['response_format']=={'type':'json_object'}
    for kind in ('voice_agent_intent','voice_interaction','companion_dialogue','tutor_hint_level_4'):
        payload=ModelGateway.request_payload(official,messages,kind)
        assert payload['thinking']=={'type':'disabled'} and 'reasoning_effort' not in payload
    qwen=SimpleNamespace(provider='qwen',base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',model='qwen3.7-plus')
    payload=ModelGateway.request_payload(qwen,messages,'tutor_whole_explanation')
    assert payload['enable_thinking'] is False
    assert 'thinking' not in payload and 'reasoning_effort' not in payload
    assert messages==[{'role':'user','content':'核对完整解释，只输出JSON。'}]


def test_gateway_does_not_repeat_a_timed_out_request_on_the_same_endpoint(normal_provider_owner) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("too slow", request=request)

    settings = Settings(
        data_dir=Path("."), database_path=Path("test.db"), model_api_key="test-key"
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(Exception) as caught:
            asyncio.run(gateway.complete(request_for([{"role": "user", "content": "提示"}])))
    finally:
        asyncio.run(gateway.close())

    assert getattr(caught.value, "code", None) == "MODEL_TIMEOUT"
    assert calls == 1


@pytest.mark.parametrize('content', ['', '{"verdict":"supported"}'])
@pytest.mark.parametrize('kind', ['tutor_whole_explanation','tutor_whole_explanation_check','learning_outcome_evaluation'])
@pytest.mark.parametrize('reason,code', [('length','MODEL_OUTPUT_TRUNCATED'),('content_filter','MODEL_OUTPUT_TERMINATED')])
def test_whole_review_truncation_is_known_consumed_failure_without_retry(content, kind, reason, code, normal_provider_owner):
    from home_study_agent.model_gateway import ModelResponseError
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,request=request,json={
            'choices':[{'finish_reason':reason,'message':{'content':content}}],
            'usage':{'prompt_tokens':123,'completion_tokens':4096}})
    gateway=ModelGateway(Settings(data_dir=Path('.'),database_path=Path('test.db'),model_api_key='test-key',
        tutor_explanation_api_key='synthetic-teaching-key', tutor_explanation_base_url='https://tutor.example.test:7791/v1'),
        transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ModelResponseError) as caught:
            asyncio.run(gateway.complete(request_for([{'role':'user','content':'Return JSON'}],request_type=kind)))
    finally:
        asyncio.run(gateway.close())
    error=caught.value
    assert error.code==code and not error.fallback_eligible
    assert error.response_received is True
    assert error.prompt_tokens==123 and error.completion_tokens==4096
    assert isinstance(error.latency_ms,int) and len(calls)==1


def test_formative_structured_receipt_has_capacity_without_expanding_voice_intents():
    from types import SimpleNamespace
    endpoint=SimpleNamespace(provider='deepseek',base_url='https://api.deepseek.com',model='deepseek-flash')
    messages=[{'role':'user','content':'Return JSON.'}]
    result=ModelGateway.request_payload(endpoint,messages,'learning_outcome_evaluation')
    assert result['max_tokens']==2400
    assert result['thinking']=={'type':'disabled'}
    assert result['response_format']=={'type':'json_object'}
    assert ModelGateway.request_payload(endpoint,messages,'voice_agent_intent')['max_tokens']==80


def test_agent_plan_uses_deterministic_json_generation_parameters():
    from types import SimpleNamespace
    endpoint = SimpleNamespace(
        provider='deepseek', base_url='https://api.deepseek.com', model='deepseek-flash'
    )
    payload = ModelGateway.request_payload(
        endpoint, [{'role': 'user', 'content': 'Return one JSON plan.'}], 'agent_plan'
    )

    assert payload['temperature'] == 0
    assert payload['response_format'] == {'type': 'json_object'}
    assert payload['thinking'] == {'type': 'disabled'}


def test_known_failure_accounting_rejects_non_numeric_or_negative_usage():
    from home_study_agent.model_gateway import ModelResponseError
    error=ModelResponseError('MODEL_OUTPUT_TRUNCATED',response_received=True,
        prompt_tokens=True,completion_tokens=-1,latency_ms='private-text')
    assert error.prompt_tokens is None and error.completion_tokens is None and error.latency_ms is None
    assert str(error)=='MODEL_OUTPUT_TRUNCATED'


def test_gateway_falls_back_to_second_provider_after_primary_transient_failures(normal_provider_owner) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.example":
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "先画出已知条件。"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 9},
            },
        )

    settings = Settings(
        data_dir=Path("."),
        database_path=Path("test.db"),
        model_provider="qwen",
        model_api_key="synthetic-64a04c6ee432d0c9370920d0",
        model_base_url="https://primary.example/v1",
        model_name="qwen-test",
        fallback_model_provider="deepseek",
        fallback_model_api_key="synthetic-6823e438f494077cce88",
        fallback_model_base_url="https://fallback.example/v1",
        fallback_model_name="deepseek-test",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(gateway.complete(request_for([{"role": "user", "content": "提示"}])))
    finally:
        asyncio.run(gateway.close())

    assert calls == ["primary.example", "primary.example", "fallback.example"]
    assert result.provider == "deepseek"
    assert result.model == "deepseek-test"
    assert result.fallback_used is True
    assert result.content == "先画出已知条件。"


def test_gateway_does_not_hide_primary_authentication_failure_with_fallback(normal_provider_owner) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(401, request=request)

    settings = Settings(
        data_dir=Path("."),
        database_path=Path("test.db"),
        model_provider="qwen",
        model_api_key="synthetic-3efa6d392d55300df9bc",
        model_base_url="https://primary.example/v1",
        model_name="qwen-test",
        fallback_model_provider="deepseek",
        fallback_model_api_key="synthetic-6823e438f494077cce88",
        fallback_model_base_url="https://fallback.example/v1",
        fallback_model_name="deepseek-test",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        try:
            asyncio.run(gateway.complete(request_for([{"role": "user", "content": "提示"}])))
            assert False, "expected authentication failure"
        except Exception as error:
            assert getattr(error, "code", None) == "MODEL_HTTP_401"
    finally:
        asyncio.run(gateway.close())

    assert calls == ["primary.example"]


@pytest.mark.parametrize("status_code", [403, 404])
def test_gateway_uses_same_provider_paygo_endpoint_when_primary_model_is_unavailable(status_code: int, normal_provider_owner) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "token-plan.example":
            return httpx.Response(status_code, request=request)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "按量备用已响应。"}}]},
        )

    settings = Settings(
        data_dir=Path("."),
        database_path=Path("test.db"),
        model_provider="qwen",
        model_api_key="synthetic-f2b39e083ae45f348e1b",
        model_base_url="https://token-plan.example/v1",
        model_name="qwen3.7-plus",
        fallback_model_provider="qwen",
        fallback_model_api_key="synthetic-c9653e9cd9cef0517115070c",
        fallback_model_base_url="https://dashscope.example/v1",
        fallback_model_name="qwen3.7-plus",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(gateway.complete(request_for([{"role": "user", "content": "提示"}])))
    finally:
        asyncio.run(gateway.close())

    assert calls == ["token-plan.example", "dashscope.example"]
    assert result.provider == "qwen"
    assert result.model == "qwen3.7-plus"
    assert result.fallback_used is True


def test_atomic_invalid_json_uses_paygo_fallback_and_counts_discarded_tokens(normal_provider_owner) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "token-plan.example":
            return httpx.Response(
                200, request=request,
                json={"choices": [{"message": {"content": '{"definitions": ['}}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 20}},
            )
        return httpx.Response(
            200, request=request,
            json={"choices": [{"message": {"content": '{"definitions":[],"relations":[]}'}}],
                  "usage": {"prompt_tokens": 11, "completion_tokens": 4}},
        )

    settings = Settings(
        data_dir=Path("."), database_path=Path("test.db"),
        model_provider="qwen", model_api_key="synthetic-f2b39e083ae45f348e1b",
        model_base_url="https://token-plan.example/v1", model_name="qwen3.7-plus",
        fallback_model_provider="qwen", fallback_model_api_key="synthetic-c9653e9cd9cef0517115070c",
        fallback_model_base_url="https://dashscope.example/v1", fallback_model_name="qwen3.7-plus",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(gateway.complete(request_for(
            [{"role": "user", "content": "JSON"}], request_type="atomic_knowledge_extraction",
        )))
    finally:
        asyncio.run(gateway.close())

    assert calls == ["token-plan.example", "dashscope.example"]
    assert result.fallback_used is True
    assert result.prompt_tokens == 21 and result.completion_tokens == 24


def test_gateway_can_call_only_the_independent_provider_for_verification(normal_provider_owner) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "VERIFIED"}}]},
        )

    settings = Settings(
        data_dir=Path("."),
        database_path=Path("test.db"),
        model_provider="qwen",
        model_api_key="synthetic-64a04c6ee432d0c9370920d0",
        model_base_url="https://primary.example/v1",
        model_name="qwen-test",
        fallback_model_provider="deepseek",
        fallback_model_api_key="synthetic-6823e438f494077cce88",
        fallback_model_base_url="https://fallback.example/v1",
        fallback_model_name="deepseek-test",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        verifier = gateway.independent_provider("qwen")
        result = asyncio.run(
            gateway.complete_on_provider("deepseek", request_for([{"role": "user", "content": "核验"}]))
        )
    finally:
        asyncio.run(gateway.close())

    assert verifier == ("deepseek", "deepseek-test")
    assert gateway.independent_provider("deepseek") == ("qwen", "qwen-test")
    assert calls == ["fallback.example"]
    assert result.provider == "deepseek"
    assert result.content == "VERIFIED"


def test_gateway_can_call_configured_fallback_model_directly(normal_provider_owner) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["model"])
        return httpx.Response(
            200, request=request,
            json={"choices": [{"message": {"content": '{"definitions":[],"relations":[]}'}}]},
        )

    settings = Settings(
        data_dir=Path("."), database_path=Path("test.db"),
        model_provider="deepseek", model_api_key="synthetic-64a04c6ee432d0c9370920d0",
        model_base_url="https://api.deepseek.com", model_name="deepseek-v4-pro",
        fallback_model_provider="deepseek", fallback_model_api_key="synthetic-6823e438f494077cce88",
        fallback_model_base_url="https://api.deepseek.com", fallback_model_name="deepseek-v4-flash",
    )
    gateway = ModelGateway(settings, transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(gateway.complete_on_fallback(request_for(
            [{"role": "user", "content": "JSON"}], request_type="atomic_knowledge_extraction",
        )))
    finally:
        asyncio.run(gateway.close())

    assert calls == ["deepseek-v4-flash"]
    assert result.model == "deepseek-v4-flash" and result.fallback_used is True


def test_gateway_requires_the_full_request_contract() -> None:
    with pytest.raises(ValueError, match="invalid model output type"):
        ModelRequest(
            request_id="request-test-invalid",
            child_id="child-test",
            session_id="session-test",
            request_type="tutor",
            risk_level="medium",
            context_manifest={},
            provider_preference="qwen",
            policy_version="policy-v1",
            prompt_version="prompt-v1",
            knowledge_version=None,
            output_type="raw_text",
            evidence_refs=(),
            messages=({"role": "user", "content": "test"},),
        )


def test_output_envelope_has_exact_policy_statuses_and_no_hidden_raw_context() -> None:
    request = request_for([{"role": "user", "content": "脱敏后的问题"}])
    envelope = build_output_envelope(
        request,
        ModelResult("只给方法提示。", "qwen", "qwen-test", 12, fallback_used=True),
        result={"content": "只给方法提示。"},
        reason_summary="当前仅提供方法提示。",
        confidence=0.65,
        verification_status="partially_verified",
        safety_status="passed",
    )

    assert envelope == {
        "request_id": "request-test-001",
        "output_type": "tutoring",
        "result": {"content": "只给方法提示。"},
        "reason_summary": "当前仅提供方法提示。",
        "confidence": 0.65,
        "evidence_refs": ["plan-item-test"],
        "model_provider": "qwen",
        "model_version": "qwen-test",
        "prompt_version": "tutor_layered_v2",
        "knowledge_version": "local_knowledge_catalog_v1",
        "policy_version": "tutor_safety_v2",
        "verification_status": "partially_verified",
        "safety_status": "passed",
        "fallback_used": True,
    }
    assert "context_manifest" not in envelope and "messages" not in envelope

    with pytest.raises(ValueError, match="rejected model output"):
        build_output_envelope(
            request,
            ModelResult("unsafe", "qwen", "qwen-test", 12),
            result={"content": "unsafe"},
            reason_summary="拒绝输出。",
            confidence=0,
            verification_status="rejected",
            safety_status="blocked",
        )
