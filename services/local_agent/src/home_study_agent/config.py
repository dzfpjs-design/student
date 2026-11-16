from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import base64
import os
import re
import ipaddress
from urllib.parse import urlsplit


def dashscope_vision_base_url(value: str) -> str | None:
    """Keep ASR credentials on the exact configured official origin and region."""
    normalized = value.strip().rstrip("/")
    host = (
        r"(?:dashscope\.aliyuncs\.com|dashscope-intl\.aliyuncs\.com|"
        r"[a-z0-9][a-z0-9-]{0,62}\.(?:cn-beijing|ap-southeast-1|ap-northeast-1|us-east-1)"
        r"\.maas\.aliyuncs\.com)"
    )
    return normalized if re.fullmatch(r"https://" + host + r"/compatible-mode/v1", normalized) else None


def validate_tutor_explanation_url(value: str | None) -> str | None:
    """Accept only a parent-authorized HTTPS API origin, not credentials or local targets.

    This performs syntactic validation; the desktop settings layer must restrict
    writes to a verified parent and pass this value from its durable store.
    DNS ownership and remote-server trust are outside this function's scope.
    """
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip() or not value.isascii():
        return None
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in value) or any(ch in value for ch in ('\\', '%', '?', '#')):
        return None
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if (parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None
        or parsed.netloc.endswith(':') or port == 0):
        return None
    if parsed.path.rstrip('/') not in ('', '/v1') or parsed.query or parsed.fragment:
        return None
    host = host.lower()
    labels = host.split('.')
    if (len(labels) < 2 or len(host) > 253 or not re.fullmatch(r'[a-z]{2,63}', labels[-1])
        or host.endswith(('.local', '.localhost'))
        or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None
    return f"https://{host}{f':{port}' if port not in (None, 443) else ''}/v1"


def validate_tutor_explanation_model(value: str | None) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r'deepseek-[A-Za-z0-9][A-Za-z0-9._-]{0,110}', value):
        return None
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    runtime_mode: str = 'formal'
    onboarding_profile_dir: Path | None = None
    atomic_graph_db_path: Path | None = None
    session_token: str | None = None
    native_launch_attestation_key: str | None = field(default=None,repr=False)
    model_provider: str = "qwen"
    model_api_key: str | None = None
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name: str = "qwen3.7-plus"
    tutor_explanation_api_key: str | None = field(default=None, repr=False)
    tutor_explanation_base_url: str | None = field(default=None, repr=False)
    tutor_explanation_model: str = "deepseek-v4.1-flash"
    voice_intent_model_name: str | None = "qwen3.8-flash"
    voice_intent_api_key: str | None = field(default=None, repr=False)
    cloud_asr_provider: str = "disabled"
    cloud_asr_api_key: str | None = None
    document_vision_api_key: str | None = field(default=None, repr=False)
    cloud_asr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    cloud_asr_model: str = "qwen3-asr-flash"
    cloud_asr_timeout_seconds: float = 15.0
    pet_audio_enabled: bool = False
    pet_audio_api_key: str | None = field(default=None, repr=False)
    pet_audio_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    pet_audio_model: str = ""
    pet_audio_tariff_version: str = ""
    pet_audio_catalog_id: str = ""
    pet_audio_configuration_id: str = ""
    pet_audio_catalog_file: str = ""
    pet_audio_catalog_sha256: str = ""
    pet_audio_control_file: str = ""
    pet_audio_control_state: str = ""
    pet_audio_input_second_micros: int = 0
    pet_audio_input_text_token_micros: int = 0
    pet_audio_output_token_micros: int = 0
    document_vision_enabled: bool = False
    document_vision_base_url: str | None = None
    document_vision_credential_source: str = "asr_same_origin"
    document_vision_model: str = "qwen3-vl-flash"
    document_vision_timeout_seconds: float = 60.0
    document_vision_max_output_tokens: int = 4096
    # Zero disables the local spending ceiling. Provider throttling, one active
    # request, stable-page gating and content deduplication remain enforced.
    document_vision_daily_request_limit: int = 0
    document_vision_daily_token_budget: int = 0
    document_vision_min_interval_seconds: float = 2.0
    document_trial_capture_enabled: bool = True
    document_trial_capture_interval_seconds: int = 30
    document_trial_capture_maximum_bytes: int = 10 * 1024 * 1024 * 1024
    fallback_model_provider: str | None = None
    fallback_model_api_key: str | None = None
    fallback_model_base_url: str | None = None
    fallback_model_name: str | None = None
    model_timeout_seconds: float = 15.0
    agent_runtime_enabled: bool = True
    local_model_dir: Path | None = None
    # Formula recognition remains opt-in because the audited MFR baseline is
    # below automatic-entry quality. Paths must be supplied explicitly; startup
    # and inference never fetch weights.
    local_formula_provider: str = "disabled"
    local_formula_model_dir: Path | None = None
    local_formula_model_manifest: Path | None = None
    data_encryption_key: bytes | None = None
    maintenance_enabled: bool = True
    maintenance_interval_seconds: float = 900.0
    # TTS is deliberately disabled unless a parent-approved deployment configures
    # a compliant relay. The renderer never receives this URL or its credential.
    tts_provider: str = "disabled"
    tts_relay_url: str | None = None
    tts_api_key: str | None = None
    tts_voice_id: str | None = None
    # Direct Model Studio connection is opt-in.  The key is injected only into
    # the local agent process from Electron's encrypted setting store.
    tts_model: str = "cosyvoice-v3-flash"
    tts_workspace_id: str | None = None
    tts_region: str = "cn-beijing"
    tts_timeout_seconds: float = 8.0

    @classmethod
    def from_environment(cls) -> "Settings":
        default_dir = Path.home() / ".home-study-companion"
        data_dir = Path(os.environ.get("HSC_DATA_DIR", default_dir)).expanduser().resolve()
        database_path = Path(
            os.environ.get("HSC_DATABASE_PATH", data_dir / "home-study.db")
        ).expanduser().resolve()
        runtime_mode = os.environ.get('HSC_RUNTIME_MODE', 'formal')
        if runtime_mode not in {'formal', 'trial'}:
            raise ValueError('RUNTIME_PROFILE_MODE_INVALID')
        if runtime_mode == 'trial':
            import json
            root_value = os.environ.get('HSC_TRIAL_USER_DATA_DIR')
            profile_id = os.environ.get('HSC_TRIAL_PROFILE_ID', '')
            if not root_value or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', profile_id):
                raise ValueError('TRIAL_PROFILE_ID_REQUIRED')
            root = Path(root_value).expanduser().resolve()
            if root not in data_dir.parents or data_dir not in database_path.parents:
                raise ValueError('TRIAL_PROFILE_PATH_ESCAPE')
            try:
                marker = json.loads((root / 'trial-profile.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise ValueError('TRIAL_PROFILE_MARKER_INVALID') from None
            if marker != {'schema': 1, 'mode': 'trial', 'profileId': profile_id}:
                raise ValueError('TRIAL_PROFILE_ID_CONFLICT')
        provider = os.environ.get("HSC_MODEL_PROVIDER", "qwen").strip().lower()
        default_base_url = (
            "https://api.deepseek.com"
            if provider == "deepseek"
            else "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        default_model = "deepseek-v4-pro" if provider == "deepseek" else "qwen3.7-plus"
        default_voice_intent_model = "deepseek-v4-pro" if provider == "deepseek" else "qwen3.8-flash"
        encoded_data_key = os.environ.get("HSC_DATA_ENCRYPTION_KEY")
        data_encryption_key = None
        if encoded_data_key:
            try:
                candidate = base64.b64decode(encoded_data_key, validate=True)
                if len(candidate) == 32:
                    data_encryption_key = candidate
            except ValueError:
                data_encryption_key = None
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            runtime_mode=runtime_mode,
            onboarding_profile_dir=(Path(os.environ['HSC_VOICE_ONBOARDING_PROFILE_DIR']).expanduser().absolute()
                if os.environ.get('HSC_VOICE_ONBOARDING_PROFILE_DIR') else None),
            atomic_graph_db_path=(
                Path(os.environ["HSC_ATOMIC_GRAPH_DB"]).expanduser().resolve()
                if os.environ.get("HSC_ATOMIC_GRAPH_DB")
                else None
            ),
            session_token=os.environ.get("HSC_SESSION_TOKEN"),
            native_launch_attestation_key=os.environ.get('HSC_NATIVE_LAUNCH_ATTESTATION_KEY'),
            model_provider=provider,
            model_api_key=os.environ.get("HSC_MODEL_API_KEY"),
            model_base_url=os.environ.get("HSC_MODEL_BASE_URL", default_base_url).rstrip("/"),
            model_name=os.environ.get("HSC_MODEL_NAME", default_model),
            tutor_explanation_api_key=os.environ.get("HSC_TUTOR_EXPLANATION_API_KEY") or None,
            tutor_explanation_base_url=validate_tutor_explanation_url(os.environ.get("HSC_TUTOR_EXPLANATION_BASE_URL")),
            tutor_explanation_model=os.environ.get("HSC_TUTOR_EXPLANATION_MODEL", "deepseek-v4.1-flash"),
            voice_intent_model_name=os.environ.get("HSC_VOICE_INTENT_MODEL_NAME") or default_voice_intent_model,
            voice_intent_api_key=os.environ.get("HSC_VOICE_INTENT_API_KEY") or None,
            cloud_asr_provider=os.environ.get("HSC_ASR_PROVIDER", "disabled").strip().lower(),
            cloud_asr_api_key=os.environ.get("HSC_ASR_API_KEY") or None,
            document_vision_api_key=os.environ.get("HSC_DOCUMENT_VISION_API_KEY") or None,
            cloud_asr_base_url=os.environ.get("HSC_ASR_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
            cloud_asr_model=os.environ.get("HSC_ASR_MODEL", "qwen3-asr-flash").strip(),
            cloud_asr_timeout_seconds=max(3.0, min(60.0, float(os.environ.get("HSC_ASR_TIMEOUT_SECONDS", "15")))),
            pet_audio_enabled=os.environ.get('HSC_PET_AUDIO_ENABLED', '').lower() == 'true',
            pet_audio_api_key=os.environ.get('HSC_PET_AUDIO_API_KEY') or None,
            pet_audio_base_url=os.environ.get('HSC_PET_AUDIO_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1').rstrip('/'),
            pet_audio_model=os.environ.get('HSC_PET_AUDIO_MODEL', ''),
            pet_audio_tariff_version=os.environ.get('HSC_PET_AUDIO_TARIFF_VERSION', ''),
            pet_audio_catalog_id=os.environ.get('HSC_PET_AUDIO_CATALOG_ID', ''),
            pet_audio_configuration_id=os.environ.get('HSC_PET_AUDIO_CONFIGURATION_ID', ''),
            pet_audio_catalog_file=os.environ.get('HSC_PET_AUDIO_CATALOG_FILE', ''),
            pet_audio_catalog_sha256=os.environ.get('HSC_PET_AUDIO_CATALOG_SHA256', ''),
            pet_audio_control_file=os.environ.get('HSC_PET_AUDIO_CONTROL_FILE', ''),
            pet_audio_control_state=os.environ.get('HSC_PET_AUDIO_CONTROL_STATE', ''),
            pet_audio_input_second_micros=int(os.environ.get('HSC_PET_AUDIO_INPUT_SECOND_MICROS', '0')),
            pet_audio_input_text_token_micros=int(os.environ.get('HSC_PET_AUDIO_INPUT_TEXT_TOKEN_MICROS', '0')),
            pet_audio_output_token_micros=int(os.environ.get('HSC_PET_AUDIO_OUTPUT_TOKEN_MICROS', '0')),
            document_vision_enabled=os.environ.get("HSC_DOCUMENT_VISION_ENABLED", "0").strip().lower() in {"1", "true", "yes"},
            document_vision_base_url=(os.environ.get("HSC_DOCUMENT_VISION_BASE_URL") or "").strip().rstrip("/") or None,
            document_vision_credential_source=os.environ.get("HSC_DOCUMENT_VISION_CREDENTIAL_SOURCE", "asr_same_origin"),
            document_vision_model=os.environ.get("HSC_DOCUMENT_VISION_MODEL", "qwen3-vl-flash").strip(),
            document_vision_timeout_seconds=float(os.environ.get("HSC_DOCUMENT_VISION_TIMEOUT_SECONDS", "60")),
            document_vision_max_output_tokens=int(os.environ.get("HSC_DOCUMENT_VISION_MAX_OUTPUT_TOKENS", "4096")),
            document_vision_daily_request_limit=int(os.environ.get("HSC_DOCUMENT_VISION_DAILY_REQUEST_LIMIT", "0")),
            document_vision_daily_token_budget=int(os.environ.get("HSC_DOCUMENT_VISION_DAILY_TOKEN_BUDGET", "0")),
            document_vision_min_interval_seconds=float(os.environ.get("HSC_DOCUMENT_VISION_MIN_INTERVAL_SECONDS", "2")),
            document_trial_capture_enabled=os.environ.get('HSC_DOCUMENT_TRIAL_CAPTURE_ENABLED', '1').lower() in {'1', 'true', 'yes'},
            document_trial_capture_interval_seconds=max(10, int(os.environ.get('HSC_DOCUMENT_TRIAL_CAPTURE_INTERVAL_SECONDS', '30'))),
            document_trial_capture_maximum_bytes=max(0, int(os.environ.get('HSC_DOCUMENT_TRIAL_CAPTURE_MAXIMUM_BYTES', str(10 * 1024 * 1024 * 1024)))),
            fallback_model_provider=os.environ.get("HSC_FALLBACK_MODEL_PROVIDER") or None,
            fallback_model_api_key=os.environ.get("HSC_FALLBACK_MODEL_API_KEY") or None,
            fallback_model_base_url=(os.environ.get("HSC_FALLBACK_MODEL_BASE_URL") or "").rstrip("/") or None,
            fallback_model_name=os.environ.get("HSC_FALLBACK_MODEL_NAME") or None,
            model_timeout_seconds=float(os.environ.get("HSC_MODEL_TIMEOUT_SECONDS", "15")),
            agent_runtime_enabled=os.environ.get("HSC_AGENT_RUNTIME_ENABLED", "1").strip().lower() not in {"0", "false", "no"},
            local_model_dir=Path(
                os.environ.get("HSC_LOCAL_MODEL_DIR", data_dir / "models")
            ).expanduser().resolve(),
            local_formula_provider=os.environ.get("HSC_LOCAL_FORMULA_PROVIDER", "disabled").strip().lower(),
            local_formula_model_dir=(Path(os.environ["HSC_LOCAL_FORMULA_MODEL_DIR"]).expanduser().resolve()
                                     if os.environ.get("HSC_LOCAL_FORMULA_MODEL_DIR") else None),
            local_formula_model_manifest=(Path(os.environ["HSC_LOCAL_FORMULA_MODEL_MANIFEST"]).expanduser().resolve()
                                          if os.environ.get("HSC_LOCAL_FORMULA_MODEL_MANIFEST") else None),
            data_encryption_key=data_encryption_key,
            maintenance_enabled=os.environ.get("HSC_MAINTENANCE_ENABLED", "1").strip().lower() not in {"0", "false", "no"},
            maintenance_interval_seconds=max(60.0, float(os.environ.get("HSC_MAINTENANCE_INTERVAL_SECONDS", "900"))),
            tts_provider=os.environ.get("HSC_TTS_PROVIDER", "disabled").strip().lower(),
            tts_relay_url=(os.environ.get("HSC_TTS_RELAY_URL") or "").strip() or None,
            tts_api_key=os.environ.get("HSC_TTS_API_KEY") or None,
            tts_voice_id=os.environ.get("HSC_TTS_VOICE_ID") or None,
            tts_model=os.environ.get("HSC_TTS_MODEL", "cosyvoice-v3-flash").strip(),
            tts_workspace_id=(os.environ.get("HSC_TTS_WORKSPACE_ID") or "").strip() or None,
            tts_region=os.environ.get("HSC_TTS_REGION", "cn-beijing").strip().lower(),
            tts_timeout_seconds=max(1.0, min(30.0, float(os.environ.get("HSC_TTS_TIMEOUT_SECONDS", "8")))),
        )
