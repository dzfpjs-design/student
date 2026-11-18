from __future__ import annotations

"""Cloud-first coaching enrichment with a local, evidence-bound fallback."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .cloud_privacy import POLICY_VERSION, sanitize_cloud_text, sensitive_categories
from .model_gateway import ModelNotConfiguredError, ModelRequest, ModelResponseError
from .repository import ReportNotReadyError
from .local_supply import digest


PROMPT_VERSION = "parent_report_coaching_v1"
POLICY_VERSION_REPORT = "parent_report_safety_v2_local_practice"
_BANNED = ("诊断", "人格", "天生", "智商", "别人都", "你就是", "懒惰", "没救")


def _report_comparison(summary, coaching):
    comparison = deepcopy([summary, coaching])
    personalization = comparison[1].get('personalization')
    if isinstance(personalization, dict):
        experiment = personalization.get('active_habit_experiment')
        outcome = experiment.get('outcome') if isinstance(experiment, dict) else None
        if isinstance(outcome, dict):
            # Active outcomes are recomputed on read. Their calculation clock
            # is not new evidence; every substantive result stays in the hash.
            outcome.pop('window_end', None)
    return digest(comparison)


class ParentReportAuthorityChangedError(ReportNotReadyError):
    code = 'PARENT_REPORT_AUTHORITY_CHANGED'

    def __init__(self):
        super().__init__('报告依据或云端授权已变化，已保留原学习记录和报告任务，请重新生成。')


class ParentReportGenerationAuthority:
    """Local, in-memory authority for this generation, never part of a prompt."""
    def __init__(self, repository, evidence, summary, baseline, current_evidence, source_version):
        self.repository = repository
        self.child_id = evidence['child']['child_id']
        self.session_id = evidence['session']['session_id']
        self.current_evidence = current_evidence
        self.expected = _report_comparison(summary, baseline)
        self.source_version = source_version
        with repository.database.connect() as c:
            c.execute('BEGIN')
            self.consent = self._consent(c)
            self.assert_source(c)

    @staticmethod
    def capture_source(repository, session_id):
        from .report_source_fingerprint import report_source_fingerprint
        with repository.database.connect() as c:
            c.execute('BEGIN')
            return report_source_fingerprint(c, session_id)

    @staticmethod
    def _consent(c):
        row = c.execute("""SELECT consent_id,allowed,policy_version,source,occurred_at FROM consent_record
            WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1""").fetchone()
        return tuple(row) if row else None

    @property
    def has_parent_consent(self):
        if not self.consent or self.consent[1:4] != (1, 'privacy_v1', 'parent_ui'):
            return False
        try:
            at = datetime.fromisoformat(self.consent[4])
            return at.tzinfo is not None and at <= datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return False

    def assert_source(self, c):
        from .report_source_fingerprint import report_source_fingerprint
        session = c.execute('''SELECT ds.child_id,ds.status,cp.status AS child_status
            FROM daily_session ds JOIN child_profile cp USING(child_id) WHERE session_id=?''', (self.session_id,)).fetchone()
        deleting = c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND status!='completed' LIMIT 1", (self.child_id,)).fetchone()
        if (not session or tuple(session)!=(self.child_id,'completed','active') or deleting
                or report_source_fingerprint(c,self.session_id)!=self.source_version):
            raise ParentReportAuthorityChangedError()

    def assert_local_authority(self, c):
        self.assert_source(c)
        privacy = c.execute('SELECT cloud_model_text_allowed FROM privacy_preference WHERE singleton_id=1').fetchone()
        if not self.has_parent_consent or not privacy or not privacy[0] or self._consent(c)!=self.consent:
            raise ParentReportAuthorityChangedError()

    def assert_for_persist(self, c, *, cloud):
        if cloud:
            self.assert_local_authority(c)
        else:
            self.assert_source(c)

    def __call__(self):
        from .reports import build_daily_report
        with self.repository.database.connect() as c:
            c.execute('BEGIN')
            self.assert_local_authority(c)
        try:
            current = self.current_evidence()
        except (ReportNotReadyError, KeyError):
            raise ParentReportAuthorityChangedError() from None
        if (current['child']['child_id']!=self.child_id or current['session']['session_id']!=self.session_id
                or _report_comparison(*build_daily_report(current))!=self.expected):
            raise ParentReportAuthorityChangedError()
        with self.repository.database.connect() as c:
            c.execute('BEGIN')
            self.assert_local_authority(c)


class ParentCoachingValidationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def enrich_parent_coaching(
    *, evidence: dict, summary: dict, baseline: dict, gateway: Any, repository: Any, settings: Any, cloud_text_allowed: bool,
    authority: ParentReportGenerationAuthority | None = None,
) -> tuple[dict, str]:
    """Return validated cloud coaching, otherwise the deterministic coaching unchanged."""
    if not cloud_text_allowed or not getattr(gateway, "configured", False):
        return baseline, "local_rules_v2"
    if authority is None:
        raise ParentReportAuthorityChangedError()
    if not authority.has_parent_consent:
        return baseline, "local_rules_v2"
    child = evidence.get("child") or {}
    from .document_revisit_memory import describe as describe_revisit
    local_only = {describe_revisit(item) for item in evidence.get('document_revisit_practice', [])}
    facts = [item for item in (baseline.get("evidence_basis") or []) if item not in local_only]
    facts.extend([item for item in (summary.get("facts") or []) if item not in local_only][:8])
    raw = "\n".join(str(item) for item in facts if item)
    if sensitive_categories(raw):
        call_id = repository.start_model_call(
            evidence["session"]["session_id"], child["child_id"], "parent_report_privacy_block",
            settings.model_provider, settings.model_name, len(raw), PROMPT_VERSION,
            {"privacy_transform": {"policy_version": POLICY_VERSION, "transmitted": False}, "raw_audio_or_video": False},
            "high", settings.model_provider, POLICY_VERSION_REPORT, "local_evidence_v1",
        )
        repository.finish_model_call(call_id, "failed", 0, 0, error_code="SENSITIVE_TEXT_LOCAL_ONLY", verification_status="rejected", safety_status="blocked")
        return baseline, "local_rules_v2"
    nickname = str(child.get("nickname") or "")
    sanitized = sanitize_cloud_text(raw[:3000], [nickname])
    prompt = (
        "以下是已确认的学习证据摘要。只可据此给家长写低压力、可执行的沟通建议；"
        "不得新增学习事实、诊断、人格标签、比较、羞辱、追问或威胁。"
        "只返回 JSON 对象，字段为 scenario, script, action, avoid。"
        "script 是 2-5 句；action 只含 what、steps(1-4句)，不改时长或停止条件；"
        "avoid 是 1-5 个含 do_not_say 与 say_instead 的对象。\n已确认事实：\n" + sanitized.text
    )
    context_manifest = {
        "object_types": ["confirmed_report_evidence"], "raw_audio_or_video": False,
        'document_revisit_included': False,
        "privacy_transform": {"policy_version": POLICY_VERSION, "transmitted": True,
                              "redaction_count": sanitized.redaction_count, "redaction_types": list(sanitized.redaction_types)},
    }
    call_id = repository.start_model_call(
        evidence["session"]["session_id"], child["child_id"], "parent_report_coaching", settings.model_provider,
        settings.model_name, len(prompt), PROMPT_VERSION, context_manifest, "medium", settings.model_provider,
        POLICY_VERSION_REPORT, "local_evidence_v1",
    )
    request = ModelRequest(
        request_id=call_id, child_id=child["child_id"], session_id=evidence["session"]["session_id"],
        request_type="parent_report", risk_level="medium", context_manifest=context_manifest,
        provider_preference=settings.model_provider, policy_version=POLICY_VERSION_REPORT,
        prompt_version=PROMPT_VERSION, knowledge_version="local_evidence_v1", output_type="report",
        pre_dispatch=authority,
        evidence_refs=(), messages=(
            {"role": "system", "content": "你是受证据约束的家长沟通助手，只输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ),
    )
    try:
        authority()
        result = await gateway.complete(request)
        authority()
        update = _validated_update(result.content)
    except ParentReportAuthorityChangedError as error:
        repository.finish_model_call(call_id, "failed", None, None, error_code=error.code,
                                     verification_status="rejected", safety_status="blocked")
        raise
    except (ModelNotConfiguredError, ModelResponseError, ValueError, json.JSONDecodeError) as error:
        code = getattr(error, "code", type(error).__name__)
        repository.finish_model_call(call_id, "failed", None, None, error_code=code, verification_status="rejected", safety_status="degraded")
        return baseline, "local_rules_v2"
    coaching = _merge_cloud_wording(baseline, update)
    repository.finish_model_call(
        call_id, "completed", result.latency_ms, len(result.content), result.prompt_tokens, result.completion_tokens,
        provider=result.provider, model=result.model, verification_status="partially_verified", safety_status="passed", fallback_used=result.fallback_used,
    )
    return coaching, "cloud_parent_coaching_v1"


def _validated_update(content: str) -> dict:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ParentCoachingValidationError("PARENT_REPORT_INVALID_JSON")
    scenario = value.get("scenario")
    script, action, avoid = value.get("script"), value.get("action"), value.get("avoid")
    if not isinstance(scenario, str) or not 1 <= len(scenario.strip()) <= 400:
        raise ParentCoachingValidationError("PARENT_REPORT_INVALID_TEXT")
    if not isinstance(script, list) or not 2 <= len(script) <= 5 or not all(isinstance(item, str) and 1 <= len(item.strip()) <= 300 for item in script):
        raise ParentCoachingValidationError("PARENT_REPORT_INVALID_SCRIPT")
    if not isinstance(action, dict) or not isinstance(action.get("what"), str):
        raise ParentCoachingValidationError("PARENT_REPORT_INVALID_ACTION")
    if not isinstance(action.get("steps"), list) or not 1 <= len(action["steps"]) <= 4:
        raise ParentCoachingValidationError("PARENT_REPORT_INVALID_ACTION")
    if not all(isinstance(step, str) and step.strip() for step in action["steps"]):
        raise ParentCoachingValidationError("PARENT_REPORT_INVALID_ACTION")
    if not isinstance(avoid, list) or not 1 <= len(avoid) <= 5 or not all(isinstance(item, dict) and isinstance(item.get("do_not_say"), str) and isinstance(item.get("say_instead"), str) for item in avoid):
        raise ParentCoachingValidationError("PARENT_REPORT_INVALID_AVOID")
    corpus = json.dumps(value, ensure_ascii=False)
    if any(marker in corpus for marker in _BANNED):
        raise ParentCoachingValidationError("PARENT_REPORT_SAFETY_REJECTED")
    return {"scenario": scenario.strip(), "script": [item.strip() for item in script], "action": {"what": action["what"].strip(), "steps": [item.strip() for item in action["steps"]]}, "avoid": [{"do_not_say": item["do_not_say"].strip(), "say_instead": item["say_instead"].strip()} for item in avoid]}


def _merge_cloud_wording(baseline: dict, update: dict) -> dict:
    """Apply only cloud wording that cannot alter evidence or action boundaries.

    The local builder is the system of record for evidence, feedback adaptation,
    duration and stop conditions.  A cloud response is untrusted prose even after
    schema validation, so this intentionally constructs a small allow-list rather
    than spreading its top-level keys into the saved snapshot.
    """
    baseline_action = baseline.get("action")
    local_duration = baseline_action.get("duration_minutes") if isinstance(baseline_action, dict) else None
    # When local evidence says not to act, the whole coaching record—not merely
    # its duration—is the safety boundary. Cloud wording could otherwise smuggle
    # pressure back in through a scenario, script, or "avoid" replacement.
    if (
        baseline.get("focus") == "insufficient_evidence"
        or not isinstance(baseline_action, dict)
        or not isinstance(local_duration, int)
        or isinstance(local_duration, bool)
        or local_duration <= 0
    ):
        return baseline
    coaching = {**baseline}
    coaching["scenario"] = update["scenario"]
    coaching["script"] = update["script"]
    coaching["avoid"] = update["avoid"]
    coaching["action"] = {
        **baseline_action,
        "what": update["action"]["what"],
        "steps": update["action"]["steps"],
    }
    return coaching
