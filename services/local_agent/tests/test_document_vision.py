from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import runpy
from threading import Event

import cv2
from fastapi.testclient import TestClient
import httpx
import numpy as np
from PIL import Image
import pytest

from normal_provider_owner import normal_provider_owner

from home_study_agent.config import Settings, dashscope_vision_base_url
from home_study_agent.app import create_app
from home_study_agent.database import Database
from home_study_agent.document_vision import (
    DocumentVisionError, DocumentVisionService, INPUT_TOKEN_RESERVATION,
    MAX_FRAME_PIXELS, MAX_UPLOAD_PIXELS, PageOnlySafetyGate,
)


BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
KEY = "synthetic-dashscope-test-key"


def synthetic_frame() -> bytes:
    image = np.full((1000, 800, 3), (40, 75, 40), dtype=np.uint8)
    cv2.rectangle(image, (100, 80), (700, 920), (255, 255, 255), -1)
    for y, text in zip((220, 340, 460, 580), (
        "1. Calculate 12 + 8", "2. Solve x + 3 = 7",
        "3. Calculate 9 / 3", "4. Find 6 * 7",
    )):
        cv2.putText(image, text, (150, y), cv2.FONT_HERSHEY_SIMPLEX, .9, (0, 0, 0), 2, cv2.LINE_AA)
    return cv2.imencode(".png", image)[1].tobytes()


class RegionEngine:
    """Deterministic local line geometry, not a mocked privacy-approval flag."""
    def __init__(self, text="Calculate 12 + 8"):
        self.text = text

    def recognize(self, image, route):
        ink = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) < 160
        ys = np.flatnonzero(ink.any(axis=1))
        groups = np.split(ys, np.where(np.diff(ys) > 2)[0] + 1)
        result = []
        for group in groups:
            if not len(group):
                continue
            top, bottom = int(group[0]), int(group[-1])
            xs = np.flatnonzero(ink[top:bottom + 1].any(axis=0))
            left, right = int(xs[0]), int(xs[-1])
            result.append({"text": self.text, "confidence": .7,
                           "box": [[left, top], [right, top], [right, bottom], [left, bottom]]})
        return result


@pytest.fixture
def context(tmp_path):
    database = Database(tmp_path / "vision.db")
    database.initialize()
    with database.connect() as db:
        for child in ("child-one", "child-two"):
            db.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                       VALUES (?,?,'4','test','active',datetime('now'),datetime('now'))""", (child, child))
        db.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                   VALUES ('session-one','child-one','active','study',datetime('now'),datetime('now'),'session-start')""")
        db.execute("""INSERT INTO privacy_preference(singleton_id,cloud_model_text_allowed,cloud_audio_allowed,
                   raw_media_storage_allowed,model_improvement_allowed,model_call_retention_days,tutor_turn_retention_days,
                   report_retention_days,updated_at) VALUES (1,1,0,0,0,90,180,730,datetime('now'))""")
        db.commit()
    settings = Settings(data_dir=tmp_path, database_path=database.path, model_provider="deepseek",
                        model_api_key="synthetic-a060961cd780e4fce78d", cloud_asr_provider="dashscope",
                        cloud_asr_api_key=KEY, cloud_asr_base_url=BASE, document_vision_enabled=True,
                        document_vision_base_url=BASE)
    return settings, database


def response_body(**changes):
    page = {"blocks": [{"kind": "text", "text": "Calculate", "bbox": [.1, .1, .8, .2], "confidence": .99},
                       {"kind": "formula", "text": "12 + 8", "bbox": [.1, .3, .8, .4], "confidence": .99}]}
    body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(page)}}],
            "usage": {"total_tokens": 700}}
    body.update(changes)
    return body


def service(context, handler=None, **settings_changes):
    settings, database = context
    transport = httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=response_body())))
    return DocumentVisionService(replace(settings, **settings_changes), database,
                                 transport=transport, ocr_engine=RegionEngine())


def analyze(vision, frame=None, **kwargs):
    return vision.analyze_page(frame or synthetic_frame(), child_id="child-one", request_id="request-one",
                               local_ocr_insufficient=True, **kwargs)


def seed_artifact(database, *, confidence=.4, status="quality_warning", text="local OCR guess"):
    with database.connect() as db:
        db.execute("""INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,local_path,
                   extracted_text,confidence,details_json,status,created_at,client_action_id)
                   VALUES ('artifact-one','child-one','session-one','document_ocr',NULL,?,?,?, ?,datetime('now'),'capture-one')""",
                   (text, confidence, json.dumps({"engine": "local-only", "other_evidence": 7}), status))
        db.commit()
        row = dict(db.execute("SELECT * FROM capture_artifact WHERE artifact_id='artifact-one'").fetchone())
    row["details"] = json.loads(row.pop("details_json"))
    return row


def test_independent_vision_credential_keeps_asr_origin_and_asr_key(context, normal_provider_owner):
    requests = []
    def handler(request):
        requests.append((str(request.url), request.headers.get('Authorization')))
        return httpx.Response(200, request=request, json=response_body())
    vision = service(context, handler, cloud_asr_model='paraformer-test', document_vision_api_key='synthetic-vision-key')
    analyze(vision)
    assert requests == [(BASE + '/chat/completions', 'Bearer synthetic-vision-key')]
    assert vision.settings.cloud_asr_api_key == KEY
    assert vision.settings.cloud_asr_base_url == BASE


def test_single_safe_png_transmission_and_untrusted_schema(context, normal_provider_owner):
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == BASE + "/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        assert payload["model"] == "qwen3-vl-flash"
        assert payload["enable_thinking"] is False
        assert payload["stream"] is False
        assert payload["max_tokens"] == 4096
        assert payload["response_format"] == {"type": "json_object"}
        assert len(payload["messages"]) == 1
        content = payload["messages"][0]["content"]
        assert [item["type"] for item in content] == ["text", "image_url"]
        data_url = content[1]["image_url"]["url"]
        assert data_url.startswith("data:image/png;base64,")
        safe_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        assert safe_bytes != synthetic_frame()
        with Image.open(BytesIO(safe_bytes)) as safe:
            assert safe.mode == "L"
            assert safe.width < 800 and safe.height < 1000
            assert safe.width * safe.height <= MAX_UPLOAD_PIXELS
            assert set(np.unique(safe)) == {0, 255}
            assert not safe.info
        assert "child-one" not in request.content.decode()
        assert "synthetic-a060961cd780e4fce78d" not in request.content.decode()
        return httpx.Response(200, json=response_body())

    vision = service(context, handler)
    result = analyze(vision)
    assert result["status"] == "needs_review"
    assert result["can_judge_correctness"] is False
    assert result["can_update_mastery"] is False
    assert all(b["evidence_type"] == "ocr_guess" and b["review_required"] for b in result["blocks"])
    assert result["blocks"][1]["confidence"] == .35
    assert result["blocks"][0]["confidence"] == .69
    assert result["privacy"]["original_frame_uploaded"] is False
    reopened = service(context, lambda _: pytest.fail("replay must not upload"))
    assert analyze(reopened) == result
    with context[1].connect() as db:
        record = dict(db.execute("SELECT * FROM document_vision_request").fetchone())
        assert "base64" not in json.dumps(record)
        assert KEY not in json.dumps(record)
        assert record["charged_tokens"] == INPUT_TOKEN_RESERVATION + 4096
    assert len(requests) == 1
    assert list(context[0].data_dir.glob("*.png")) == []


@pytest.mark.parametrize("base", [BASE, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "https://workspace-12.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "https://123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"])
def test_official_same_origin_configuration(context, base):
    assert dashscope_vision_base_url(base + "/") == base
    assert service(context, cloud_asr_base_url=base, document_vision_base_url=base).configured


@pytest.mark.parametrize("override", [
    {"cloud_asr_base_url": "https://evil.test/compatible-mode/v1"},
    {"document_vision_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    {"document_vision_base_url": "https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1"},
    {"document_vision_base_url": BASE + "?token=secret"},
    {"document_vision_base_url": BASE.replace("https:", "http:")},
    {"document_vision_credential_source": "text_model"}, {"cloud_asr_api_key": None},
    {"document_vision_enabled": False}, {"document_vision_model": "deepseek-v4-pro"},
    {"document_vision_timeout_seconds": float("nan")}, {"document_vision_min_interval_seconds": -1},
])
def test_bad_configuration_never_sends_credentials(context, override):
    with pytest.raises(DocumentVisionError, match="NOT_CONFIGURED"):
        analyze(service(context, lambda _: pytest.fail("no upload"), **override))


def test_settings_environment_is_explicit_not_a_text_or_generic_key_fallback(monkeypatch, tmp_path):
    for name in ("HSC_DOCUMENT_VISION_ENABLED", "HSC_DOCUMENT_VISION_BASE_URL", "HSC_ASR_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HSC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "generic-secret")
    monkeypatch.setenv("HSC_MODEL_API_KEY", "text-secret")
    settings = Settings.from_environment()
    assert settings.document_vision_enabled is False
    assert settings.cloud_asr_api_key is None
    monkeypatch.setenv("HSC_DOCUMENT_VISION_ENABLED", "1")
    monkeypatch.setenv("HSC_DOCUMENT_VISION_BASE_URL", BASE)
    monkeypatch.setenv("HSC_ASR_API_KEY", KEY)
    monkeypatch.setenv("HSC_DOCUMENT_VISION_DAILY_REQUEST_LIMIT", "3")
    settings = Settings.from_environment()
    assert settings.document_vision_enabled
    assert settings.document_vision_base_url == BASE
    assert settings.document_vision_daily_request_limit == 3


def test_document_vision_environment_defaults_to_low_latency_flash(monkeypatch, tmp_path):
    monkeypatch.setenv("HSC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("HSC_DOCUMENT_VISION_MODEL", raising=False)
    monkeypatch.delenv("HSC_DOCUMENT_VISION_TIMEOUT_SECONDS", raising=False)

    settings = Settings.from_environment()

    assert settings.document_vision_model == "qwen3-vl-flash"
    assert settings.document_vision_timeout_seconds == 60
    assert settings.document_vision_max_output_tokens == 4096


def test_invalid_response_exposes_only_safe_protocol_stage(context, normal_provider_owner):
    body = response_body()
    body["choices"][0]["finish_reason"] = "length"
    with pytest.raises(DocumentVisionError, match="INVALID_RESPONSE") as caught:
        analyze(service(context, lambda _: httpx.Response(200, json=body)))
    assert caught.value.diagnostic_stage == "truncated"


def test_schema_failure_diagnostic_has_only_type_and_location(context, normal_provider_owner):
    content = json.dumps({"blocks": [{
        "kind": "text", "text": "private question text", "bbox": [.1, .2, .8, .4],
        "confidence": .9, "source": "model_invented_private_value",
    }]})
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    with pytest.raises(DocumentVisionError, match="INVALID_RESPONSE") as caught:
        analyze(service(context, lambda _: httpx.Response(200, json=body)))
    stage = caught.value.diagnostic_stage
    assert stage.startswith("block_schema:literal_error:blocks.0.source")
    assert "private" not in stage and "model_invented" not in stage


def test_health_reports_effective_document_vision_configuration(context):
    configured = service(
        context,
        document_vision_model="qwen3-vl-flash",
        document_vision_timeout_seconds=47,
        document_vision_max_output_tokens=1536,
    )
    application = create_app(settings=configured.settings)

    response = TestClient(application).get("/v1/health")

    assert response.status_code == 200
    assert response.json()["document_vision"] == {
        "configured": True,
        "model": "qwen3-vl-flash",
        "timeout_seconds": 47,
        "max_output_tokens": 1536,
    }


@pytest.mark.parametrize("kind", ["room", "video"])
def test_page_gate_refuses_unproven_content(context, kind):
    image = cv2.imdecode(np.frombuffer(synthetic_frame(), np.uint8), cv2.IMREAD_COLOR)
    if kind == "room":
        image[:] = (70, 100, 70)
    elif kind == "photo":
        image[640:840, 200:500] = np.random.default_rng(1).integers(60, 220, (200, 300, 3), dtype=np.uint8)
    elif kind == "occlusion":
        cv2.rectangle(image, (80, 400), (350, 700), (20, 50, 120), -1)
    if kind == "video":
        stream = BytesIO()
        Image.fromarray(image).save(stream, format="GIF", save_all=True,
                                   append_images=[Image.fromarray(image // 2)], duration=100)
        frame = stream.getvalue()
    else:
        frame = cv2.imencode(".png", image)[1].tobytes()
    with pytest.raises(DocumentVisionError):
        analyze(service(context, lambda _: pytest.fail("unsafe upload")), frame)
    with context[1].connect() as db:
        assert db.execute("SELECT COUNT(*) FROM document_vision_request").fetchone()[0] == 0


def test_edge_filled_text_page_uploads_only_locally_proven_binary_ink(context, normal_provider_owner):
    requests = []
    image = cv2.imdecode(np.frombuffer(synthetic_frame(), np.uint8), cv2.IMREAD_COLOR)[80:920, 100:700]
    frame = cv2.imencode(".png", image)[1].tobytes()

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        data_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        safe_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        assert safe_bytes != frame
        with Image.open(BytesIO(safe_bytes)) as safe:
            assert safe.mode == "L"
            assert set(np.unique(safe)) == {0, 255}
            assert safe.width * safe.height <= MAX_UPLOAD_PIXELS
        return httpx.Response(200, json=response_body())

    vision = DocumentVisionService(
        context[0], context[1], transport=httpx.MockTransport(handler), ocr_engine=RegionEngine()
    )
    result = vision.analyze_page(
        frame, child_id="child-one", session_id="session-one", request_id="edge-filled",
        local_ocr_insufficient=True, allowed=True,
    )
    assert result["status"] == "needs_review"
    assert result["privacy"]["original_frame_uploaded"] is False
    assert len(requests) == 1


def test_rectified_binary_page_preserves_visible_mark_outside_local_ocr_polygon():
    image = np.full((900, 700, 3), 255, dtype=np.uint8)
    cv2.putText(image, "1. Calculate 12 + 8", (70, 180), cv2.FONT_HERSHEY_SIMPLEX,
                1.0, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(image, "A. 20", (85, 275), cv2.FONT_HERSHEY_SIMPLEX,
                .9, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.circle(image, (560, 680), 28, (35, 35, 35), 4, cv2.LINE_AA)

    class PrintedLineOnly:
        def recognize(self, _image, _route):
            return [
                {"text": "1. Calculate 12 + 8", "confidence": .9,
                 "box": [[60, 130], [430, 130], [430, 195], [60, 195]]},
                {"text": "A. 20", "confidence": .9,
                 "box": [[80, 240], [220, 240], [220, 285], [80, 285]]},
            ]

    page = PageOnlySafetyGate(PrintedLineOnly()).prepare(cv2.imencode(".png", image)[1].tobytes())
    safe = cv2.imdecode(np.frombuffer(page.png, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert set(np.unique(safe)) == {0, 255}
    assert (safe[int(safe.shape[0] * .65):, int(safe.shape[1] * .65):] == 0).sum() > 25


def test_orientation_is_server_derived_and_applied_before_safe_page_rebuild(context):
    image = cv2.imdecode(np.frombuffer(synthetic_frame(), np.uint8), cv2.IMREAD_COLOR)
    sideways = cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    frame = cv2.imencode(".png", sideways)[1].tobytes()
    page = PageOnlySafetyGate(RegionEngine()).prepare(frame, (), 90)
    assert page.width > 0 and page.height > 0
    mirrored_page = PageOnlySafetyGate(RegionEngine()).prepare(
        cv2.imencode(".png", cv2.flip(sideways, 1))[1].tobytes(), (), 90, True,
    )
    assert mirrored_page.width > 0 and mirrored_page.height > 0
    with pytest.raises(DocumentVisionError, match="INVALID_ORIENTATION"):
        PageOnlySafetyGate(RegionEngine()).prepare(frame, (), 45)


def test_native_document_camera_frame_pixel_budget_is_shared_with_page_gate(monkeypatch):
    accepted = object()
    gate = PageOnlySafetyGate(RegionEngine())
    monkeypatch.setattr(gate, "_prepare", lambda image, names, orientation, mirror: accepted)

    native = BytesIO()
    Image.new("L", (4160, 3120), 255).save(native, format="PNG")
    assert MAX_FRAME_PIXELS == 4160 * 3120
    assert gate.prepare(native.getvalue()) is accepted

    above_native = BytesIO()
    Image.new("L", (4161, 3120), 255).save(above_native, format="PNG")
    with pytest.raises(DocumentVisionError, match="DOCUMENT_VISION_INVALID_IMAGE"):
        gate.prepare(above_native.getvalue())


@pytest.mark.parametrize("text", ["姓名：张同学", "Name: Alice", "test@example.com", "19900000001", "child-two"])
def test_local_pii_rejects_image_not_just_ocr_text(context, text):
    settings, database = context
    vision = DocumentVisionService(settings, database, transport=httpx.MockTransport(lambda _: pytest.fail("PII upload")),
                                   ocr_engine=RegionEngine(text))
    with pytest.raises(DocumentVisionError, match="PII_DETECTED"):
        analyze(vision)


@pytest.mark.parametrize("missing", [True, False])
def test_face_detection_or_missing_detector_is_fail_closed(context, monkeypatch, missing):
    class Detector:
        def empty(self): return missing
        def detectMultiScale(self, *args, **kwargs): return [(20, 20, 80, 80)]
    monkeypatch.setattr(cv2, "CascadeClassifier", lambda _: Detector())
    with pytest.raises(DocumentVisionError, match="PRIVACY_CHECK_UNAVAILABLE" if missing else "FACE_DETECTED"):
        analyze(service(context, lambda _: pytest.fail("unsafe upload")))


def test_missing_local_regions_does_not_use_cloud_to_prove_privacy(context):
    vision = service(context, lambda _: pytest.fail("unsafe upload"))
    vision._gate.ocr_engine.recognize = lambda *_: []
    with pytest.raises(DocumentVisionError, match="LOCAL_REGIONS_UNPROVEN"):
        analyze(vision)


@pytest.mark.parametrize("status,code", [(429, "RATE_LIMITED"), (401, "HTTP_401"), (500, "HTTP_500"), (307, "REDIRECT_REFUSED")])
def test_http_errors_do_not_leak_payload_or_follow_redirect(context, status, code, normal_provider_owner):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={"Retry-After": "120", "Location": "https://evil.test/steal"},
                              text=KEY + " secret provider diagnostic")
    vision = service(context, handler)
    with pytest.raises(DocumentVisionError, match=code) as exc:
        analyze(vision)
    assert KEY not in repr(exc.value)
    assert "secret provider diagnostic" not in repr(exc.value)
    assert calls == [BASE + "/chat/completions"]
    receipt = vision.request_status(child_id="child-one", request_id="request-one")
    assert receipt["status"] == "failed"
    if status == 429:
        assert exc.value.retry_after_seconds == 120
        with pytest.raises(DocumentVisionError, match="COOLDOWN"):
            vision.analyze_page(synthetic_frame(), child_id="child-one", request_id="retry", local_ocr_insufficient=True)
        assert len(calls) == 1


def test_timeout_has_durable_no_refund_and_no_automatic_retry(context, normal_provider_owner):
    def timeout(request):
        raise httpx.ReadTimeout(KEY, request=request)
    vision = service(context, timeout)
    with pytest.raises(DocumentVisionError, match="TIMEOUT") as exc:
        analyze(vision)
    assert KEY not in str(exc.value)
    reopened = service(context, lambda _: pytest.fail("unknown-billing replay"))
    with pytest.raises(DocumentVisionError, match="TIMEOUT"):
        analyze(reopened)
    with context[1].connect() as db:
        assert db.execute("SELECT charged_tokens FROM document_vision_request").fetchone()[0] > 0


@pytest.mark.parametrize("change", [{"document_vision_daily_request_limit": 1}, {"document_vision_daily_token_budget": 100}])
def test_budget_reserves_before_network(context, change, normal_provider_owner):
    if "document_vision_daily_request_limit" in change:
        analyze(service(context, **change))
    with pytest.raises(DocumentVisionError, match="BUDGET_EXCEEDED"):
        service(context, lambda _: pytest.fail("budget bypass"), **change).analyze_page(
            synthetic_frame(), child_id="child-one", request_id="second",
            local_ocr_insufficient=True,
        )


def test_zero_daily_limits_disable_local_budget_without_disabling_provider_backoff(context, normal_provider_owner):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_body())
    vision = service(context, handler, document_vision_daily_request_limit=0,
                     document_vision_daily_token_budget=0, document_vision_min_interval_seconds=0)
    analyze(vision)
    vision.analyze_page(synthetic_frame(), child_id="child-one", request_id="second",
                        local_ocr_insufficient=True)
    assert calls == 2


def test_budget_survives_restart_and_pending_reservation(context, normal_provider_owner):
    vision = service(context, document_vision_daily_request_limit=1)
    analyze(vision)
    reopened = service(context, lambda _: pytest.fail("budget reset"), document_vision_daily_request_limit=1)
    with pytest.raises(DocumentVisionError, match="BUDGET_EXCEEDED"):
        reopened.analyze_page(synthetic_frame(), child_id="child-one", request_id="second", local_ocr_insufficient=True)
    with context[1].connect() as db:
        db.execute("UPDATE document_vision_request SET status='reserved', result_json=NULL")
        db.commit()
    with pytest.raises(DocumentVisionError, match="OUTCOME_UNKNOWN"):
        analyze(reopened)


@pytest.mark.parametrize("content", ["not-json", '{"blocks":[]}', '{"mastery":1,"blocks":[]}',
    '{"blocks":[{"kind":"formula","text":"x=1","bbox":[0,0,2,1],"confidence":0.9}]}',
    '{"blocks":[{"kind":"text","text":"x=1","bbox":[0,0,1,1],"confidence":NaN}]}'])
def test_untrusted_or_malformed_response_is_not_an_outcome(context, content, normal_provider_owner):
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    with pytest.raises(DocumentVisionError, match="INVALID_RESPONSE"):
        analyze(service(context, lambda _: httpx.Response(200, json=body)))


def test_provider_cannot_exceed_twelve_protocol_blocks(context, normal_provider_owner):
    blocks = [
        {
            "kind": "text",
            "text": f"{index}. visible question",
            "bbox": [.1, .1, .8, .2],
            "confidence": .8,
            "source": "printed",
        }
        for index in range(1, 14)
    ]
    body = response_body(choices=[{
        "finish_reason": "stop",
        "message": {"content": json.dumps({"blocks": blocks})},
    }])
    with pytest.raises(DocumentVisionError, match="INVALID_RESPONSE") as caught:
        analyze(service(context, lambda _: httpx.Response(200, json=body)))
    assert str(caught.value.diagnostic_stage).startswith("block_schema")


def test_truncated_response_is_rejected(context, normal_provider_owner):
    body = response_body()
    body["choices"][0]["finish_reason"] = "length"
    with pytest.raises(DocumentVisionError, match="INVALID_RESPONSE"):
        analyze(service(context, lambda _: httpx.Response(200, json=body)))


def test_invalid_provider_payload_records_safe_page_transmission_and_response(context, normal_provider_owner):
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": "not-json"}}])
    artifact = seed_artifact(context[1])
    enriched = asyncio.run(service(
        context, lambda _: httpx.Response(200, json=body),
    ).analyze_if_needed("session-one", artifact, synthetic_frame(), allowed=True))
    privacy = enriched["details"]["cloud_vision_fallback"]["privacy"]
    assert privacy["transmission_attempted"] is True
    assert privacy["provider_response_received"] is True
    assert privacy["uploaded"] == "page_region_binary_ink"


def test_async_enrichment_preserves_local_ocr_and_persists_evidence(context, normal_provider_owner):
    artifact = seed_artifact(context[1])
    enriched = asyncio.run(service(context).analyze_if_needed("session-one", artifact, synthetic_frame(), allowed=True))
    for name in ("status", "confidence", "extracted_text"):
        assert enriched[name] == artifact[name]
    cloud = enriched["details"]["cloud_vision_fallback"]
    assert cloud["status"] == "needs_review"
    assert cloud["needs_confirmation"] is True
    assert cloud["text"] == "Calculate\n12 + 8"
    assert enriched["details"]["other_evidence"] == 7
    assert "cloud_vision_fallback" not in artifact["details"]
    with Database(context[1].path).connect() as db:
        row = db.execute("SELECT extracted_text, details_json FROM capture_artifact").fetchone()
        assert row["extracted_text"] == artifact["extracted_text"]
        assert json.loads(row["details_json"])["cloud_vision_fallback"] == cloud
        receipt = db.execute("SELECT result_json FROM document_vision_request WHERE status='succeeded'").fetchone()
        assert json.loads(receipt[0]) == cloud


@pytest.mark.parametrize("blocked", ["explicit", "privacy", "inactive", "not_study"])
def test_async_enrichment_exposes_permission_block_without_upload(context, blocked):
    artifact = seed_artifact(context[1])
    with context[1].connect() as db:
        if blocked == "privacy": db.execute("UPDATE privacy_preference SET cloud_model_text_allowed=0")
        if blocked == "inactive": db.execute("UPDATE daily_session SET status='completed'")
        if blocked == "not_study": db.execute("UPDATE daily_session SET state='break'")
        db.commit()
    vision = service(context, lambda _: pytest.fail("forbidden upload"))
    enriched = asyncio.run(vision.analyze_if_needed("session-one", artifact, synthetic_frame(), allowed=blocked != "explicit"))
    assert enriched["details"]["cloud_vision_fallback"]["status"] == "blocked"
    assert enriched["details"]["cloud_vision_fallback"]["text"] == ""
    privacy = enriched["details"]["cloud_vision_fallback"]["privacy"]
    assert privacy["transmission_attempted"] is False
    assert privacy["provider_response_received"] is False
    assert enriched["extracted_text"] == artifact["extracted_text"]


def test_sufficient_local_ocr_does_not_upload(context):
    artifact = seed_artifact(context[1], confidence=.95, status="completed")
    enriched = asyncio.run(service(context, lambda _: pytest.fail("not a fallback")).analyze_if_needed(
        "session-one", artifact, synthetic_frame()))
    assert enriched["details"]["cloud_vision_fallback"]["status"] == "not_needed"


def test_permission_is_rechecked_after_local_image_work(context, monkeypatch):
    vision = service(context, lambda _: pytest.fail("revoked permission"))
    prepare = vision._gate.prepare
    def revoke(*args):
        page = prepare(*args)
        with context[1].connect() as db:
            db.execute("UPDATE privacy_preference SET cloud_model_text_allowed=0")
            db.commit()
        return page
    monkeypatch.setattr(vision._gate, "prepare", revoke)
    with pytest.raises(DocumentVisionError, match="CLOUD_NOT_ALLOWED"):
        analyze(vision)


def test_real_local_ocr_regions_on_non_family_synthetic_page():
    page = PageOnlySafetyGate().prepare(synthetic_frame())
    assert page.width < 800 and page.height < 1000
    assert page.png.startswith(b"\x89PNG")


@pytest.mark.parametrize("deny_at", [1, 2, 3, 4])
def test_generation_callback_cancels_at_each_pre_upload_checkpoint(context, deny_at):
    calls = 0
    def allowed():
        nonlocal calls
        calls += 1
        return calls < deny_at
    vision = service(context, lambda _: pytest.fail("cancelled request uploaded"))
    with pytest.raises(DocumentVisionError, match="DOCUMENT_VISION_CANCELLED"):
        analyze(vision, still_allowed=allowed)
    assert calls == deny_at
    if deny_at == 4:
        assert vision.request_status(child_id="child-one", request_id="request-one")["error_code"] == "DOCUMENT_VISION_CANCELLED"
    else:
        assert vision.request_status(child_id="child-one", request_id="request-one") is None


def test_generation_callback_error_is_fail_closed_and_sanitized(context):
    def broken():
        raise RuntimeError(KEY)
    with pytest.raises(DocumentVisionError, match="CANCELLED") as exc:
        analyze(service(context, lambda _: pytest.fail("callback failure upload")), still_allowed=broken)
    assert KEY not in str(exc.value)


def test_child_pausing_during_gate_prevents_cloud_upload(context, monkeypatch):
    vision = service(context, lambda _: pytest.fail("pause race upload"))
    live = Event()
    live.set()
    original = vision._gate.prepare
    def pause_during_ocr(*args):
        page = original(*args)
        live.clear()
        return page
    monkeypatch.setattr(vision._gate, "prepare", pause_during_ocr)
    with pytest.raises(DocumentVisionError, match="CANCELLED"):
        analyze(vision, still_allowed=live.is_set)
    assert vision.request_status(child_id="child-one", request_id="request-one") is None


def test_larger_budget_defaults_are_bounded_per_child(context, normal_provider_owner):
    settings, database = context
    assert settings.document_vision_daily_request_limit == 0
    assert settings.document_vision_daily_token_budget == 0
    assert settings.document_vision_min_interval_seconds == 2
    vision = service(context, document_vision_daily_request_limit=1)
    first = analyze(vision)
    with database.connect() as db:
        db.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-one'")
        db.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
                   VALUES ('session-two','child-two','active','study',datetime('now'),datetime('now'),'session-two')""")
        db.commit()
    second = vision.analyze_page(synthetic_frame(), child_id="child-two", request_id="request-one", local_ocr_insufficient=True)
    assert first["request_id"] == second["request_id"]
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM document_vision_request").fetchone()[0] == 2
    assert vision.request_status(child_id="child-unknown", request_id="request-one") is None


def test_model_prompt_includes_validator_schema_and_a_valid_json_example(context, normal_provider_owner):
    from home_study_agent.document_vision import PROMPT, _ProviderPage
    example = next(line for line in PROMPT.splitlines() if line.startswith('{"blocks":'))
    assert _ProviderPage.model_validate_json(example).blocks[0].kind == "formula"
    def handler(request):
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        schema = json.loads(prompt.split("JSON Schema:\n", 1)[1])
        assert schema == _ProviderPage.model_json_schema()
        bounds = schema["$defs"]["_ProviderBlock"]["properties"]["bbox"]["items"]
        assert bounds["minimum"] == 0 and bounds["maximum"] == 1
        assert 'Do NOT return pixel coordinates' in prompt
        assert 'Do not return untouched questions' in prompt
        assert 'exactly ONE printed block' in prompt
        assert 'Maximum 12 blocks' in prompt
        assert 'Never assume a question is single-choice' in prompt
        return httpx.Response(200, json=response_body())
    assert analyze(service(context, handler))["status"] == "needs_review"


@pytest.fixture
def schema_summary():
    script = Path(__file__).resolve().parents[1] / "scripts" / "probe_document_vision.py"
    return runpy.run_path(str(script))["summarize_response_schema"]


@pytest.mark.parametrize("box,normalized,ordered", [
    ([.1, .2, .8, .4], True, True), ([100, 200, 800, 400], False, True),
    ([.8, .2, .1, .4], True, False), ([0, 0, 1, 1], True, True),
])
def test_synthetic_diagnostic_identifies_coordinate_protocol_without_changing_it(
    context, schema_summary, box, normalized, ordered, normal_provider_owner,
):
    content = json.dumps({"blocks": [{"kind": "formula", "text": "12 + 8", "bbox": box, "confidence": .3}]})
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    summary = schema_summary(body)
    assert summary["blocks"][0]["bbox_in_0_1"] is normalized
    assert summary["blocks"][0]["bbox_ordered"] is ordered
    assert summary["validation"] == ("passed" if normalized and ordered else "failed")
    if box == [100, 200, 800, 400]:
        result = analyze(service(context, lambda _: httpx.Response(200, json=body)))
        assert result["blocks"][0]["bbox"] == [.1, .2, .8, .4]
        assert result["provider_normalizations"] == ["bbox_0_1000_to_0_1"]
    elif not (normalized and ordered):
        with pytest.raises(DocumentVisionError, match="INVALID_RESPONSE"):
            analyze(service(context, lambda _: httpx.Response(200, json=body)))


def test_known_qwen_json_fence_and_percent_confidence_are_normalized(context, normal_provider_owner):
    content = "```json\n" + json.dumps({"blocks": [{
        "kind": "text", "text": "1. 速度", "bbox": [100, 200, 800, 400],
        "confidence": 97, "source": "printed",
    }]}, ensure_ascii=False) + "\n```"
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    result = analyze(service(context, lambda _: httpx.Response(200, json=body)))
    assert result["blocks"][0]["source"] == "printed"
    assert result["blocks"][0]["bbox"] == [.1, .2, .8, .4]
    assert result["blocks"][0]["confidence"] == .69
    assert result["provider_normalizations"] == [
        "bbox_0_1000_to_0_1", "confidence_percent_to_0_1",
    ]


@pytest.mark.parametrize(("bbox", "expected", "normalization"), [
    ([100, 200, 800, 1001], [.1, .2, .8, 1.0], "bbox_rounding_clamped_to_0_1"),
    ([100, 200, 800, 1008], [.1, .2, .8, 1.0], "bbox_rounding_clamped_to_0_1"),
    ([.1, -.001, .8, 1.001], [.1, 0.0, .8, 1.0], "bbox_normalized_rounding_clamped_to_0_1"),
    ([.1, -.008, .8, 1.008], [.1, 0.0, .8, 1.0], "bbox_normalized_rounding_clamped_to_0_1"),
    ([.1, -.08, .8, 1.08], [.1, 0.0, .8, 1.0], "bbox_normalized_rounding_clamped_to_0_1"),
])
def test_known_provider_bbox_rounding_overflow_is_narrowly_clamped(
    context, bbox, expected, normalization, normal_provider_owner,
):
    content = json.dumps({"blocks": [{
        "kind": "text", "text": "2. 电热计算", "bbox": bbox,
        "confidence": .8, "source": "printed",
    }]}, ensure_ascii=False)
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    result = analyze(service(context, lambda _: httpx.Response(200, json=body)))
    assert result["blocks"][0]["bbox"] == expected
    assert normalization in result["provider_normalizations"]


@pytest.mark.parametrize("bbox", [
    [0, 0, 2, 1],
    [100, 200, 800, 1200],
    [.1, .2, .8, 1.2],
    [1001, 200, 1000, 900],
])
def test_bbox_rounding_tolerance_still_rejects_real_overflow_or_invalid_order(context, bbox, normal_provider_owner):
    content = json.dumps({"blocks": [{
        "kind": "text", "text": "2. 电热计算", "bbox": bbox,
        "confidence": .8, "source": "printed",
    }]}, ensure_ascii=False)
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    with pytest.raises(DocumentVisionError, match="INVALID_RESPONSE"):
        analyze(service(context, lambda _: httpx.Response(200, json=body)))


def test_one_degenerate_provider_block_does_not_discard_other_auditable_blocks(context, normal_provider_owner):
    content = json.dumps({"blocks": [
        {"kind": "text", "text": "4. 电热计算", "bbox": [.1, .2, .8, .3],
         "confidence": .8, "source": "printed"},
        {"kind": "text", "text": "page edge artifact", "bbox": [1001, 900, 1008, 1008],
         "confidence": .2, "source": "uncertain"},
    ]}, ensure_ascii=False)
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    result = analyze(service(context, lambda _: httpx.Response(200, json=body)))
    assert [block["text"] for block in result["blocks"]] == ["4. 电热计算"]
    assert "dropped_degenerate_bbox_block" in result["provider_normalizations"]


def test_known_provider_role_and_formula_aliases_are_canonicalized(context, normal_provider_owner):
    content = json.dumps({"blocks": [
        {"kind": "printed_text", "text": "2. 电热计算", "bbox": [.1, .2, .8, .3],
         "confidence": .8, "source": "print"},
        {"kind": "grading_mark", "text": "圈选B", "bbox": [.2, .31, .3, .35],
         "confidence": .7},
        {"kind": "equation", "text": "Q=I^2Rt", "bbox": [.2, .36, .6, .4],
         "confidence": .6, "source": "handwriting"},
    ]}, ensure_ascii=False)
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": content}}])
    result = analyze(service(context, lambda _: httpx.Response(200, json=body)))
    assert [(block["kind"], block["source"]) for block in result["blocks"]] == [
        ("text", "printed"), ("text", "grading_mark"), ("formula", "handwritten"),
    ]
    assert {"kind_role_alias_to_text_and_source", "kind_formula_alias", "source_role_alias"}.issubset(
        result["provider_normalizations"]
    )


def test_synthetic_diagnostic_never_exposes_text_keys_values_or_error_inputs(schema_summary):
    secret = "synthetic-key-never-serialize-this"
    page = {secret: secret, "blocks": [
        {"kind": "formula", "text": secret, "bbox": [100, 200, 800, 400], "confidence": 97, secret: secret},
        {"kind": secret, "text": {secret: secret}, "bbox": secret, "confidence": secret},
    ]}
    body = response_body(choices=[{"finish_reason": secret, "message": {"content": json.dumps(page)}}])
    body["secret"] = secret
    result = schema_summary(body)
    encoded = json.dumps(result)
    assert secret not in encoded
    assert '"input"' not in encoded and '"ctx"' not in encoded and '"msg"' not in encoded
    assert result["finish_reason"] == "missing_or_unknown"
    assert result["root_extra_field_count"] == 1
    assert result["blocks"][0]["confidence_in_0_1"] is False
    assert result["validation"] == "failed"
    assert any(issue["type"] == "less_than_equal" for issue in result["issues"])


@pytest.mark.parametrize("content", ["```json\nSECRET-RESPONSE\n```", '{"text":"bad\\latex"}', None, ["SECRET-RESPONSE"]])
def test_synthetic_diagnostic_handles_invalid_json_and_non_string_content(schema_summary, content):
    body = response_body(choices=[{"finish_reason": "length", "message": {"content": content}}])
    result = schema_summary(body)
    assert result["finish_reason"] == "length"
    assert "SECRET-RESPONSE" not in json.dumps(result)
    if isinstance(content, str):
        assert result["json_valid"] is False


def test_synthetic_diagnostic_bounds_its_output(schema_summary):
    page = {"blocks": [{"kind": "bad", "text": "private", "bbox": [100, 200, 300, 400], "confidence": 99}] * 100}
    body = response_body(choices=[{"finish_reason": "stop", "message": {"content": json.dumps(page)}}])
    result = schema_summary(body)
    assert len(result["blocks"]) == 8
    assert len(result["issues"]) <= 12
    assert len(json.dumps(result)) < 6000
