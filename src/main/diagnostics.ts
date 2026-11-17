import { appendFile, mkdir, readdir, rename, rm, stat } from 'node:fs/promises'
import { existsSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { DIAGNOSTIC_ROUTE_TEMPLATES } from './diagnostic-route-templates'

export type DiagnosticLevel = 'debug' | 'info' | 'warn' | 'error'

export type DiagnosticEvent = {
  source: 'main' | 'renderer' | 'local_service' | 'device' | 'model' | 'ocr' | 'system'
  event: string
  level?: DiagnosticLevel
  message?: string
  correlationId?: string
  details?: unknown
}

export type DiagnosticSummary = {
  rootPath: string
  logPath: string
  crashDumpPath: string
  currentLogBytes: number
  rotatedLogCount: number
  maxFileBytes: number
  maxRotatedFiles: number
  rawMediaRecorded: false | null
  existingLogsVerified: boolean
  crashDumpsInspected: false
  uploadEnabled: false
}

const MAX_ARRAY_LENGTH = 50
const MAX_DEPTH = 5
const EVENTS = new Set(`app_process_started primary_window_created primary_window_closed primary_window_ready_to_show
window_unresponsive window_responsive_after_stall page_loaded process_gone page_load_failed diagnostic_folder_revealed
app_quit_requested restart_requested start_requested external_service_ready runtime_missing data_key_unavailable process_spawned local_service.output_redaction_suppressed
process_exited process_closed startup_failed stop_requested graceful_shutdown_failed force_stop service_ready recovery_exhausted recovery_scheduled
app_quit_service_closed app_quit_diagnostics_flushed app_quit_final_requested app_quit_unload_prevented app_quit_will_quit app_quit_codec_disposed draft_codec_dispose_failed
stdout stderr structured_output_invalid local_api_request trace_record_failed worker_line window_error unhandled_rejection uncaught_exception process_warning
voice.asr.started voice.asr.completed voice.asr.failed voice.asr.low_information_filtered voice.asr.echo_candidate_retained voice.asr.playback_echo_discarded
voice.asr.transcript_dispatched voice.asr.non_speech_discarded voice.capture.signal_rejected voice.capture.segment_queued
voice.capture.ready voice.capture.utterance_detected voice.household.microphone_resolved voice.household.microphone_unresolved
voice.capture.playback_gate_started voice.capture.playback_gate_ended
voice.household.microphone_resolution_failed voice.household_listener.failed voice.tts.delivery voice.tts.arbitration
voice.household.initialization_prompt_started voice.household.initialization_prompt_completed voice.household.identity_selected
voice.onboarding.progress_saved voice.wake_word.saved
voice.realtime.configured voice.realtime.signaling_succeeded voice.realtime.signaling_failed
voice.realtime.settings_read_failed voice.realtime.runtime_error voice.realtime.turn_dispatched voice.realtime.socket_open_failed voice.realtime.socket_message_rejected voice.realtime.socket_ended voice.realtime.native_connect_failed
voice.realtime.semantic_recovery voice.realtime.committed_input_dispatch voice.realtime.duplex_capability voice.realtime.local_reply_boundary
voice.realtime.reply_first_audio voice.realtime.reply_delivery voice.realtime.inactivity_standby voice.realtime.microphone_fallback
voice.realtime.session_rotated voice.realtime.keepalive_acknowledged voice.public_reply.usage voice.public_reply.failed
document.capture.open_requested document.capture.open_failed document.capture.camera_ready document.capture.frame_not_ready
document.capture.frame_captured document.capture.frame_blocked document.capture.ocr_started document.capture.retry_requested
document.capture.ocr_completed document.capture.ocr_failed document.capture.agent_dispatched
pilot_email_claim_failed pilot_email_claim_invalid pilot_email_sent pilot_email_delivery_failed pilot_email_result_unknown`.split(/\s+/))
for (const event of ['device.connect.completed', 'device.connect.failed',
  'voice.realtime.input_progress',
  'voice.realtime.input_source_checked', 'voice.realtime.input_admitted',
  'voice.realtime.input_rejected', 'voice.realtime.retry_exhausted', 'voice.realtime.retry_scheduled']) EVENTS.add(event)
EVENTS.add('voice.realtime.native_operation')
EVENTS.add('mistake.print.stage')
for(const event of ['voice.realtime.echo_cancellation_policy','voice.realtime.native_response'])EVENTS.add(event)
for (const event of ['voice.wake.control_started', 'voice.wake.control_result', 'voice.wake.detected']) EVENTS.add(event)
const ERROR_CODES = new Set(`LOCAL_ASR_BUSY DRAFT_RECOVERY_IO MODEL_TIMEOUT MODEL_UNAVAILABLE MODEL_RESPONSE_INVALID MODEL_NOT_CONFIGURED MODEL_NETWORK_ERROR
APP_RESTARTED DEVICE_NOT_BOUND BOUND_DEVICE_NOT_FOUND PARENT_CAPTURE_UNAVAILABLE DEVICE_COMMAND_UNKNOWN
DEVICE_COMMAND_FAILED DEVICE_COMMAND_TIMEOUT DEVICE_PERMISSION_DENIED CAMERA_NOT_FOUND CAMERA_BUSY CAMERA_PERMISSION_DENIED
MICROPHONE_NOT_FOUND MICROPHONE_PERMISSION_DENIED ASR_TIMEOUT ASR_NO_SPEECH ASR_UNAVAILABLE OCR_FAILED OCR_TIMEOUT
MEDIA_API_UNAVAILABLE MEDIA_PERMISSION_DENIED DEVICE_BINDING_UNAVAILABLE DEVICE_NOT_FOUND DEVICE_BUSY_OR_UNAVAILABLE UNSUPPORTED_DEVICE_MODE
DEVICE_OPEN_FAILED DEVICE_NO_CONFIRMED_FRAME DEVICE_RESULT_UNCONFIRMED DEVICE_RECEIVER_UNAVAILABLE
LOCAL_SESSION_REQUIRED PARENT_AUTH_REQUIRED INPUT_INVALID SOURCE_CHANGED VERSION_CONFLICT PLAN_INPUT_REQUIRED
TRACE_RECORD_FAILED ECONNREFUSED ECONNRESET ETIMEDOUT ENOENT EACCES EADDRINUSE AgentExecutionConflict UnsupportedServerCapabilityError
ActivityVoiceError TaskStartBindingError InputRevisionError ValueError KeyError RuntimeError ModelResponseError`.split(/\s+/))
for (const code of ['PILOT_DIGEST_INVALID','PILOT_EMAIL_UNKNOWN_ERROR','FOXMAIL_MAPI_TIMEOUT','FOXMAIL_MAPI_INVALID_RECEIPT',
  'FOXMAIL_MAPI_PROCESS_FAILED','FOXMAIL_MAPI_REJECTED','FOXMAIL_NOT_DEFAULT']) ERROR_CODES.add(code)
for (const code of ['SMTP_CONFIG_INVALID', 'SMTP_CREDENTIALS_INVALID', 'SMTP_DELIVERY_UNKNOWN',
  'SMTP_MESSAGE_INVALID', 'SMTP_RECIPIENT_INVALID', 'SMTP_RECIPIENT_REFUSED',
  'SMTP_SUBMISSION_REJECTED', 'SMTP_UNAVAILABLE']) ERROR_CODES.add(code)
for (const code of `PUBLIC_REPLY_ALREADY_ATTEMPTED PUBLIC_REPLY_AUTHORITY_REVOKED PUBLIC_REPLY_BIND_UNCONFIRMED
PUBLIC_REPLY_CANCELLED PUBLIC_REPLY_CAPACITY PUBLIC_REPLY_CONFIG_CHANGED PUBLIC_REPLY_DELIVERY_UNCONFIRMED
PUBLIC_REPLY_EOS_MISSING PUBLIC_REPLY_EVENT_INVALID PUBLIC_REPLY_EVENT_LIMIT PUBLIC_REPLY_FORMAT_UNCONFIRMED
PUBLIC_REPLY_GENERATION_UNCONFIRMED PUBLIC_REPLY_MODEL_CHANGED PUBLIC_REPLY_OUTPUT_INVALID PUBLIC_REPLY_OUTPUT_UNCONFIRMED
PUBLIC_REPLY_OWNER_RETIRED PUBLIC_REPLY_RECEIPT_INVALID PUBLIC_REPLY_SAMPLES_INVALID PUBLIC_REPLY_SOURCE_INVALID
PUBLIC_REPLY_SUPPLIER_FAILED PUBLIC_REPLY_TEXT_CHANGED PUBLIC_REPLY_TEXT_UNCONFIRMED PUBLIC_REPLY_TIMEOUT
PUBLIC_REPLY_TOOL_SOURCE_CHANGED PUBLIC_REPLY_TOOL_UNCONFIRMED PUBLIC_REPLY_TOOL_UNEXPECTED`.split(/\s+/)) ERROR_CODES.add(code)
// Closed outcomes from the durable daily-log mail protocol.
for (const code of ["PILOT_ATTACHMENT_HASH_MISMATCH", "PILOT_ATTACHMENT_INVALID", "PILOT_ATTACHMENT_WINDOW_INVALID", "PILOT_DIGEST_CLAIM_CHANGED", "PILOT_DIGEST_SUBMISSION_ALREADY_STARTED", "PILOT_LEGACY_ATTACHMENT_SNAPSHOT_MISSING", "PILOT_RECIPIENT_ALIAS_AMBIGUOUS", "PILOT_RECIPIENT_NOT_AUTHORIZED", "PILOT_RECIPIENT_REVOKED", "PILOT_SUBMISSION_OWNERSHIP_CHANGED", "PILOT_SUBMISSION_PURPOSE_INVALID", "PILOT_VERIFICATION_ACTION_CHANGED", "PILOT_VERIFICATION_ACTION_CONFLICT", "PILOT_VERIFICATION_ACTION_INVALID", "PILOT_VERIFICATION_ACTION_REPLAY", "PILOT_VERIFICATION_DATE_INVALID", "PILOT_VERIFICATION_WINDOW_EMPTY", "SERVER_SUBMISSION_OUTCOME_NOT_PERSISTED", "SMTP_ATTACHMENT_INVALID", "SMTP_ATTACHMENT_TOO_LARGE"]) ERROR_CODES.add(code)
const REASON_CODES = new Set(`QWEN_NATIVE_SOCKET_UNCONFIRMED QWEN_NATIVE_SOCKET_OWNER_CHANGED QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED QWEN_NATIVE_SOCKET_PROVIDER_ERROR QWEN_NATIVE_SOCKET_PAYLOAD_INVALID QWEN_NATIVE_SOCKET_BACKPRESSURE QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED QWEN_NATIVE_CREDENTIAL_REJECTED QWEN_NATIVE_ACCESS_DENIED QWEN_NATIVE_ACCOUNT_BALANCE
ASR_NO_HUMAN_SPEECH ASR_NO_MEANINGFUL_SPEECH ASR_NO_SPEECH
ASR_CLOUD_DIALOGUE_UNAVAILABLE ASR_CLOUD_AUDIO_REVOKED ASR_MODEL_NOT_READY ASR_CLIENT_TIMEOUT ASR_CLIENT_NETWORK_OR_PROTOCOL ASR_CLIENT_UNKNOWN
QWEN_REALTIME_MEDIA_PLAYBACK_BLOCKED QWEN_REALTIME_DATA_CHANNEL QWEN_REALTIME_CONNECTION_FAILED
QWEN_REALTIME_PROVIDER_ERROR QWEN_REALTIME_RUNTIME_BYPASS QWEN_REALTIME_MICROPHONE QWEN_REALTIME_UNKNOWN
QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED QWEN_REALTIME_INPUT_IDENTITY_CAPACITY QWEN_REALTIME_SESSION_EXPIRED QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED
QWEN_REALTIME_PLAYBACK_COMPLETION_UNVERIFIED`.split(/\s+/))
for(const code of ["QWEN_NATIVE_SESSION_CONFIG_TIMEOUT", "QWEN_NATIVE_SESSION_CONFIG_UNCONFIRMED", "QWEN_NATIVE_SESSION_IDENTITY_UNCONFIRMED", "QWEN_NATIVE_HOUSEHOLD_INPUT_UNCONFIRMED", "QWEN_NATIVE_RESTORED_INPUT_UNCONFIRMED", "QWEN_NATIVE_INPUT_ADMISSION_UNCONFIRMED", "QWEN_NATIVE_RESPONSE_IDENTITY_UNCONFIRMED", "QWEN_NATIVE_INPUT_SOURCE_RESTRICTED", "QWEN_NATIVE_INPUT_BACKPRESSURE", "QWEN_NATIVE_TOOL_HANDOFF_UNCONFIRMED", "QWEN_NATIVE_RUNTIME_CONTEXT_UNCONFIRMED", "QWEN_NATIVE_AUDIO_PERMISSION_UNCONFIRMED", "QWEN_NATIVE_AUDIO_OUTPUT_UNAVAILABLE", "QWEN_NATIVE_DIALOGUE_SAVE_UNCONFIRMED", "QWEN_NATIVE_PROTOCOL_CAPACITY"])REASON_CODES.add(code)
for (const code of `QWEN_PLAYBACK_ELEMENT_ERROR QWEN_PLAYBACK_OBSERVER_CREATE_FAILED QWEN_PLAYBACK_OBSERVER_ACTIVITY_FAILED
QWEN_PLAYBACK_OBSERVER_QUIET_UNCONFIRMED QWEN_PLAYBACK_OBSERVER_MISSING QWEN_PLAYBACK_OBSERVER_UNAVAILABLE
QWEN_PLAYBACK_PLAY_NOT_ALLOWED QWEN_PLAYBACK_PLAY_NOT_SUPPORTED QWEN_PLAYBACK_PLAY_ABORTED
QWEN_PLAYBACK_PLAY_INVALID_STATE QWEN_PLAYBACK_PLAY_OTHER QWEN_PLAYBACK_TAIL_UNCONFIRMED`.split(/\s+/)) REASON_CODES.add(code)
for (const code of ['microphone_closed', 'input_item_denied', 'input_item_invalid', 'anonymous_input_overflow']) REASON_CODES.add(code)
const NUMBERS = new Set(`elapsed_ms elapsedMs durationMs duration_ms latency_ms capture_ms audio_bytes image_bytes transcript_chars
width height confidence statusCode errorCode exitCode processId webContentsId argumentCount attempts attempt delayMs
line column sample_rate frame_count chars count removed_chars inactivity_seconds agent_runtime_ms first_audio_ms total_turn_ms elapsed_since_dispatch_ms`.split(/\s+/))
const TOKEN_COUNTS = new Set(['input_tokens', 'output_tokens', 'total_tokens'])
const BOOLEANS = new Set(`cloud_used expected isMainFrame automation parent_port for_mistake echo_candidate bound
speech_detected raw_audio_or_video local_only cloud_transmission_allowed woke managed_voice_required initialization_required delivered browser_aec full_duplex`.split(/\s+/))
const CONTAINERS = new Set(['details', 'error', 'metrics', 'result'])
const ENUMS: Record<string, Set<string>> = {
  print_action: new Set(['print', 'pdf']),
  print_stage: new Set(['admission', 'validation', 'save_dialog', 'layout_load', 'layout_readiness', 'queue_submit', 'pdf_render', 'pdf_save']),
  print_outcome: new Set(['started', 'completed', 'submitted', 'saved', 'cancelled', 'unconfirmed', 'failed']),
  print_failure: new Set(['owner', 'busy', 'invalid_document', 'load_error', 'readiness_error', 'unreadable_image', 'layout_timeout',
    'window_closed', 'renderer_gone', 'queue_timeout', 'printer_settings', 'printer_failed', 'printer_callback', 'native_exception']),
  native_connect_stage: new Set(['prerequisites','subscribe','playback_barrier','microphone','echo_policy','lease','fanout','downlink','socket_open','context']),
  native_connect_failure: new Set(['permission_denied','device_missing','device_busy','constraints','aborted','invalid_state','type_error','playback_tail_unconfirmed','other']),
  socket_open_stage: new Set(['policy', 'owner', 'configuration', 'preflight', 'dial', 'handshake', 'post_open_policy']),
  socket_open_failure: new Set(['timeout', 'transport_error', 'http_rejected', 'socket_closed', 'other']),
  socket_open_retirement: new Set(['none', 'provider_error', 'policy_unconfirmed', 'owner_changed', 'invalid_payload', 'unconfirmed', 'closed']),
  socket_end_kind: new Set(['provider_error', 'policy_unconfirmed', 'owner_changed', 'invalid_payload', 'unconfirmed', 'local_close', 'local_failure', 'remote_close', 'transport_error', 'heartbeat_timeout', 'heartbeat_send_failed', 'provider_error_event', 'provider_response_failed']),
  socket_close_category: new Set(['normal', 'going_away', 'no_status', 'abnormal', 'policy', 'server_error', 'service_restart', 'try_later', 'other']),
  socket_transport_category: new Set(['reset', 'timeout', 'unreachable', 'broken_pipe', 'protocol', 'unknown']),
  socket_payload_failure: new Set(['none', 'send_missing_socket', 'send_not_opened', 'send_not_ready', 'send_invalid', 'send_empty', 'send_oversize', 'message_conversion', 'message_binary', 'message_oversize', 'message_json', 'message_structure', 'message_other']),
  socket_message_top: new Set(['null', 'array', 'object', 'scalar']),
  socket_message_type: new Set(['missing', 'non_string', 'not_object']),
  socket_message_header: new Set(['missing', 'object', 'other', 'not_object']),
  socket_message_payload: new Set(['missing', 'object', 'other', 'not_object']),
  socket_message_error: new Set(['missing', 'object', 'other', 'not_object']),
  socket_message_code: new Set(['missing', 'object', 'other', 'not_object']),
  socket_message_message: new Set(['missing', 'object', 'other', 'not_object']),
  socket_message_event: new Set(['missing', 'object', 'other', 'not_object']),
  socket_message_code_type: new Set(['missing', 'not_object', 'null', 'string', 'number', 'boolean', 'array', 'object']),
  socket_message_message_type: new Set(['missing', 'not_object', 'null', 'string', 'number', 'boolean', 'array', 'object']),
  socket_provider_category: new Set(['credential', 'access', 'balance', 'rate_limit', 'model_not_found', 'resource_not_found', 'invalid_parameter', 'quota', 'provider_unavailable', 'unknown']),
  platform: new Set(['win32', 'darwin', 'linux']),
  architecture: new Set(['x64', 'arm64', 'ia32']),
  trigger: new Set(['status_error', 'lease_renewal', 'connect_failed']),
  source_status: new Set(['current', 'missing', 'unbound', 'owner_changed', 'source_changed']),
  input_stage: new Set(['admission', 'dispatch', 'provider_completed', 'duplicate_discarded', 'input_not_admitted',
    'callback_started', 'callback_returned', 'callback_failed', 'empty_transcript', 'callback_owner_checked']),
  exitSignal: new Set(['SIGKILL', 'SIGTERM', 'SIGINT', 'SIGBREAK', 'SIGHUP', 'SIGQUIT', 'SIGABRT']),
  method: new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD']),
  mode: new Set(['wake', 'conversation', 'household_wake']),
  failure_kind: new Set(['api', 'timeout', 'network_or_protocol', 'client']),
  delivery: new Set(['text_only', 'managed', 'browser', 'system_female_offline', 'system_offline']),
  disposition: new Set(['finished', 'unavailable', 'cancelled', 'expired', 'superseded', 'full']),
  priority: new Set(['reminder', 'dialogue', 'lesson', 'urgent']),
  route: new Set(['study', 'global', 'agent_runtime', 'realtime_tool', 'runtime_recovery', 'reconnect_recovery', 'committed_input']),
  runtime: new Set(['qwen_omni_realtime']),
  observation_kind: new Set(['remote_pcm_with_active_player', 'local_pcm_scheduled']),
  route_template: new Set(DIAGNOSTIC_ROUTE_TEMPLATES),
  session_state: new Set(['check_in', 'classroom_recall', 'homework_capture', 'plan_review', 'study', 'reflection']),
  status: new Set(['pending', 'running', 'waiting_child', 'completed', 'connected', 'failed', 'unknown', 'cancelled', 'sent']),
  native_operation: new Set(['catalog', 'context', 'input', 'proposal', 'receipt', 'dialogue', 'client']),
  native_phase: new Set(['claim','execute','complete','read']),
  native_capability: new Set(['behavior.feedback', 'behavior.reset_personalization', 'break.begin', 'break.resume', 'checkin.update_context', 'companion.activity_feedback', 'companion.end_activity', 'companion.start_activity', 'controlled_content.play', 'controlled_content.stop', 'controlled_game.play', 'controlled_game.stop', 'conversation.respond', 'device.connect', 'device.status', 'diagnostic.cancel', 'diagnostic.replay', 'diagnostic.start', 'diagnostic.submit', 'document.cancel_capture', 'document.capture_question', 'document.confirm_ocr', 'document.correct_ocr', 'document.monitor_control', 'document.retry_capture', 'document.set_occlusion_reminders', 'exam.cancel', 'exam.pause', 'exam.resume', 'exam.start', 'exam.submit', 'homework.add', 'homework.add_batch', 'homework.none', 'homework.review_carryover', 'homework.update_duration', 'learning.capture_question', 'learning.evaluate_response', 'learning.explain', 'learning.prepare_request', 'learning.request_response', 'learning.review_revisit', 'learning.review_schedule', 'learning_inputs.cancel_revision', 'learning_inputs.confirm_revision', 'learning_inputs.propose_revision', 'learning_inputs.review_reclaimed_scope', 'learning_inputs.review_unplaced', 'learning_inputs.select_target', 'mistake.activity_control', 'mistake.capture_control', 'mistake.complete_remediation', 'mistake.diagnose', 'mistake.dialogue', 'mistake.open', 'mistake.repair_question', 'mistake.review', 'mistake.submit_response', 'native_writing.submission_status', 'native_writing.submit_retained', 'observation.correct', 'observation.pause', 'observation.resume', 'onboarding.resume', 'oral.independent_check', 'oral.retry', 'pet.listening_control', 'pet_vocabulary.cancel', 'pet_vocabulary.start', 'pet_vocabulary.submit', 'planning.describe', 'planning.generate', 'planning.reorder', 'privacy.pause_all', 'profile.switch', 'recall.add', 'recall.add_batch', 'recall.none', 'recall.review_pending', 'reflection.complete', 'reflection.update', 'safety.find_parent', 'session.request_end', 'session.snooze', 'session.transition', 'speech.repeat', 'speech.stop', 'system.inspect_learning', 'task.complete', 'task.pause', 'task.reschedule', 'task.resume', 'task.start', 'tutor.capture_check_response', 'tutor.more_hint', 'tutor.playback_control', 'tutor.record_feedback', 'tutor.retry_check_coaching', 'voice.configure_wake_word', 'workspace.navigate', 'writing.confirm_original', 'writing.independent_check', 'writing.read_original', 'writing.revise_original']),
  transport: new Set(['smtp']),
  client: new Set(['configured_smtp']),
  device_type: new Set(['camera', 'document_camera', 'microphone']),
  reason: new Set(['occluded', 'clean-exit', 'abnormal-exit', 'killed', 'crashed', 'oom', 'launch-failed', 'integrity-failure']),
  selection_method: new Set(['wake_word', 'spoken_grade']),
  step: new Set(['wake_practice', 'study_practice', 'document_practice', 'finish_practice', 'completed', 'deferred']),
  name: new Set(['Error', 'TypeError', 'RangeError', 'AbortError', 'TimeoutError', 'NotAllowedError', 'NotFoundError', 'NotReadableError', 'OverconstrainedError', 'SecurityError']),
  errorType: new Set(['ValueError', 'KeyError', 'RuntimeError', 'TimeoutError', 'CancelledError', 'HTTPException'])
}
ENUMS.native_capability.add('mistake.learn')
// Formative learning exports only fixed operational metadata.
EVENTS.add("mistake.learning.model_result")
EVENTS.add("mistake.learning.action_result")
for (const code of ["MISTAKE_LEARNING_ACTION_CONFLICT", "MISTAKE_LEARNING_ACTION_NOT_FOUND", "MISTAKE_LEARNING_ARGUMENT_INVALID", "MISTAKE_LEARNING_CANCELLED", "MISTAKE_LEARNING_CHILD_NOT_FOUND", "MISTAKE_LEARNING_CLOUD_CONSENT_REQUIRED", "MISTAKE_LEARNING_DAILY_SHARE_COMPLETE", "MISTAKE_LEARNING_EXPLANATION_FIRST", "MISTAKE_LEARNING_FOCUS_CHANGED", "MISTAKE_LEARNING_MODEL_FAILED", "MISTAKE_LEARNING_MODEL_OUTPUT_INVALID", "MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED", "MISTAKE_LEARNING_NOTHING_DUE", "MISTAKE_LEARNING_OUTCOME_UNKNOWN", "MISTAKE_LEARNING_PRACTICE_CHANGED", "MISTAKE_LEARNING_PRACTICE_CHECK_FAILED", "MISTAKE_LEARNING_REVISION_CHANGED", "MISTAKE_LEARNING_REVISION_INVALID", "MISTAKE_LEARNING_SELECTION_REQUIRED", "MISTAKE_LEARNING_SESSION_CHANGED", "MISTAKE_LEARNING_SESSION_REQUIRED", "MISTAKE_LEARNING_SOURCE_CHANGED", "MISTAKE_LEARNING_SOURCE_NOT_FOUND", "MISTAKE_LEARNING_TEXT_INVALID", "MISTAKE_LEARNING_UNANSWERED_ITEM_REQUIRED", "MODEL_MISTAKE_REASONING_CONFIG_REQUIRED"]) ERROR_CODES.add(code)
ENUMS.request_type = new Set(["mistake_learning_analyze", "mistake_learning_practice", "mistake_learning_coach", "mistake_learning_answer", "mistake_learning_verify"])
ENUMS.mistake_status = new Set(["completed", "failed", "unknown", "cancelled"])
ENUMS.model_provider = new Set(["qwen", "deepseek"])
ENUMS.model_name = new Set(["qwen3.7-plus", "qwen3.8-flash", "deepseek-v4-pro"])

// Closed native-response/echo metadata; no source text or supplier identities.
ENUMS.echo_mode=new Set(['all','remote-only','browser-selected','disabled','unknown'])
ENUMS.echo_negotiation=new Set(['not_supported','accepted','rejected','timed_out','ended'])
ENUMS.response_status=new Set(['completed','cancelled','failed','incomplete'])
ENUMS.response_reason=new Set(['none','max_output_tokens','content_filter','client_cancelled','turn_detected','other'])
ENUMS.cancel_source=new Set(['speech_onset','confirmed_input','response_binding','other_local'])
ENUMS.response_text_kind=new Set(['empty','whitespace','punctuation','substantive'])
for(const key of ['response_elapsed_ms','response_text_chars','audio_chunk_count','pcm_chunk_count','first_pcm_ms'])NUMBERS.add(key)
for(const key of ['response_error_present','new_input_before_pcm','terminal_current','has_tool_call','pcm_scheduled'])BOOLEANS.add(key)

// Actual startup/recovery producers only. No dynamic event or field passthrough.
for(const event of ["runtime_awake_unconfirmed", "protected_startup_failed", "voice.realtime.pending_input_recovery", "voice.realtime.cancellation_journal_unconfirmed", "voice.agent.identity_index_restored", "voice.agent.identity_index_unconfirmed", "voice.realtime.identity_index_restored", "voice.realtime.identity_index_unconfirmed", "voice.realtime.cancellation_recovered", "voice.realtime.cancellation_recovery_unconfirmed", "voice.realtime.cancellation_recovery_invalid", "voice.recovery.receipt_check_incomplete", "voice.realtime.resume_receipt_retained", "voice.original_prompt.admission", "voice.capture.protected_tail_window_failed", "app_quit_backend_abnormal_acknowledged"])EVENTS.add(event)
EVENTS.add('restart_after_abnormal_shutdown')
// Reviewed fixed producers reached through diagnostic wrapper forwarding.
for(const event of ["structured_event", "document.capture.frame_deferred", "voice.capture.protected_tail_failed", "voice.capture.unknown_overlap_held", "voice.capture.echo_cancellation_policy", "voice.capture.original_prefix_admitted", "voice.capture.original_prefix_held", "voice.capture.original_prefix_discarded"])EVENTS.add(event)
for(const reason of ['quiet_not_verified','source_or_close_unknown'])REASON_CODES.add(reason)
ENUMS.reason.add('transient_occlusion')
for (const event of ['document.capture.frame_state', 'document.capture.ocr_pending']) EVENTS.add(event)
ENUMS.frame_state = new Set(['no_page', 'stabilizing', 'writing', 'occluded', 'page_changed', 'stable'])
ENUMS.frame_motion_band = new Set(['lt_2', '2_to_4_5', '4_5_to_7', '7_to_11', 'ge_11'])
ENUMS.frame_skin_band = new Set(['lt_025', '025_to_10', '10_to_22', 'ge_22'])
ENUMS.frame_content_motion = new Set(['changed', 'unchanged', 'unavailable'])
ENUMS.frame_paper_band = new Set(['lt_10', 'ge_10'])
ENUMS.frame_light_band = new Set(['dark', 'usable'])
ENUMS.protected_startup_reason = new Set(['storage','media_key_recovery','rotation_model_unclassified','tutor_recovery_marker'])
// Fixed structured stdout producers, still filtered through the same closed schema.
for(const event of ["privacy_database_delete_failed", "privacy_analysis_candidate_residual", "privacy_delete_dependency_blocked", "learning_adaptation_pending", "document_learning_worker_error", "speaking_candidate_worker_error", "pet_audio_worker_error"])EVENTS.add(event)
for(const code of ['RUNTIME_AWAKE_UNCONFIRMED','LOCAL_SERVICE_SHUTDOWN_PENDING','LOCAL_SERVICE_SHUTDOWN_ABNORMAL'])ERROR_CODES.add(code)
for(const key of ['has_more','allowed','capture_ready'])BOOLEANS.add(key)
ENUMS.scope=new Set(['canonical_voice_only'])
ENUMS.window=new Set(['initial','recovery'])
for(const value of ['deadline','read_failed'])ENUMS.reason.add(value)

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function knownError(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  if (/^FOXMAIL_MAPI_(?:LOGON|SEND)_(?:0X[0-9A-F]{8}|\d{1,10})$/.test(value)) return value
  if (/^(?:PILOT_DIGEST_(?:CLAIM|RESULT)|SMTP_SUBMIT)_HTTP_[1-5][0-9]{2}$/.test(value)) return value
  return ERROR_CODES.has(value) ? value : undefined
}

export function sanitizeDiagnosticValue(value: unknown, depth = 0, budget = { remaining: 200 }): unknown {
  if (--budget.remaining < 0) return '[TRUNCATED_METADATA]'
  if (depth > MAX_DEPTH) return '[TRUNCATED_DEPTH]'
  if (value === null || value === undefined || typeof value === 'boolean') return value
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined
  if (typeof value === 'string' || typeof value === 'bigint') return undefined
  if (value instanceof Error) {
    return {
      name: ENUMS.name.has(value.name) ? value.name : 'Error',
      ...(knownError((value as Error & { code?: string }).code) ? { code: knownError((value as Error & { code?: string }).code) } : {})
    }
  }
  if (Array.isArray(value)) return value.slice(0, MAX_ARRAY_LENGTH).map((item) => sanitizeDiagnosticValue(item, depth + 1, budget))
  if (typeof value === 'object') {
    const safe: Record<string, unknown> = {}
    for (const [key, item] of Object.entries(value as Record<string, unknown>).slice(0, 100)) {
      if (TOKEN_COUNTS.has(key) && typeof item === 'number' && Number.isSafeInteger(item) && item >= 0) safe[key] = item
      else if (key === 'connection_generation' && typeof item === 'number' && Number.isSafeInteger(item) && item >= 1) safe[key] = item
      else if (key === 'retry_delay_ms' && typeof item === 'number' && [1000, 3000, 8000].includes(item)) safe[key] = item
      else if (key === 'socket_application_idle_ms' && typeof item === 'number' && Number.isSafeInteger(item) && item >= 0 && item <= 86_400_000) safe[key] = item
      else if ((key === 'exitCode' || key === 'exitSignal') && item === null) safe[key] = null
      else if (['restored','confirmed'].includes(key) && typeof item === 'number' && Number.isSafeInteger(item) && item >= 0 && item <= 1000000) safe[key] = item
      else if (key === 'recovery_windows' && typeof item === 'number' && Number.isInteger(item) && item >= 0 && item <= 2) safe[key] = item
      else if (key === 'learning_tasks_replayed' && item === 0) safe[key] = 0
      else if (NUMBERS.has(key) && typeof item === 'number' && Number.isFinite(item)) safe[key] = item
      else if ((BOOLEANS.has(key) || key === 'packaged') && typeof item === 'boolean') safe[key] = item
      else if (key === 'version' && typeof item === 'string' && /^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(item)) safe[key] = item
      else if (['physical_audibility_verified', 'completion_verified', 'acoustic_quality_verified', 'contains_transcript'].includes(key) && item === false) safe[key] = false
      else if (['code', 'error_code'].includes(key) && knownError(item)) safe[key] = item
      else if (key === 'reason_code' && typeof item === 'string' && REASON_CODES.has(item)) safe[key] = item
      else if (Object.prototype.hasOwnProperty.call(ENUMS, key) && ENUMS[key].has(item as string)) safe[key] = item
      else if (CONTAINERS.has(key) && item && typeof item === 'object') safe[key] = sanitizeDiagnosticValue(item, depth + 1, budget)
    }
    return safe
  }
  return undefined
}

export class DiagnosticRecorder {
  readonly logDirectory: string
  readonly crashDumpPath: string
  readonly logPath: string
  private pending: Promise<void> = Promise.resolve()
  private readonly existingLogs: boolean

  constructor(
    readonly rootPath: string,
    readonly maxFileBytes = 10 * 1024 * 1024,
    readonly maxRotatedFiles = 32
  ) {
    this.logDirectory = join(rootPath, 'logs')
    this.crashDumpPath = join(rootPath, 'crashes')
    this.logPath = join(this.logDirectory, 'runtime.jsonl')
    this.existingLogs = existsSync(this.logDirectory)
    mkdirSync(this.logDirectory, { recursive: true })
    mkdirSync(this.crashDumpPath, { recursive: true })
  }

  record(input: DiagnosticEvent): Promise<void> {
    const event = EVENTS.has(input.event) ? input.event : 'unregistered_event'
    const details = sanitizeDiagnosticValue(input.details) as Record<string, unknown> | undefined
    // Raw error text is never persisted. Recognized codes remain actionable.
    const error = knownError(details?.error_code) ?? knownError(details?.code)
      ?? knownError((details?.error as Record<string, unknown> | undefined)?.code)
      ?? input.message?.slice(0, 4000).split(/[^A-Z0-9_]+/).find((code) => knownError(code))
    const deviceBlocked = error && ['DEVICE_NOT_BOUND','BOUND_DEVICE_NOT_FOUND','CAMERA_NOT_FOUND','MICROPHONE_NOT_FOUND'].includes(error)
    const summary = deviceBlocked ? '所需设备未就绪，请核对绑定与连接。'
      : error ? '操作未确认，请核对当前状态与错误类型。' : '已记录本地事件元数据；未保存原始内容。'
    const line = `${JSON.stringify({
      timestamp: new Date().toISOString(),
      privacy_policy_version: 'diagnostic_metadata_v2',
      level: ['debug','info','warn','error'].includes(input.level ?? '') ? input.level : 'info',
      source: ['main','renderer','local_service','device','model','ocr','system'].includes(input.source) ? input.source : 'system',
      event,
      summary,
      recovery_action: deviceBlocked ? 'check_device_binding' : error ? 'check_receipt_before_retry' : 'inspect_local_status',
      ...(error ? { error_code: error } : {}),
      ...(input.correlationId && UUID.test(input.correlationId) ? { correlation_id: input.correlationId } : {}),
      ...(details !== undefined ? { details } : {})
    })}\n`
    this.pending = this.pending
      .then(() => this.append(line))
      .catch(() => console.error('[diagnostics] LOCAL_DIAGNOSTIC_WRITE_FAILED'))
    return this.pending
  }

  flush(): Promise<void> {
    return this.pending
  }

  async summary(): Promise<DiagnosticSummary> {
    await this.flush()
    const currentLogBytes = await stat(this.logPath).then((value) => value.size).catch(() => 0)
    const rotatedLogCount = await readdir(this.logDirectory)
      .then((entries) => entries.filter((name) => /^runtime\.jsonl\.\d+$/.test(name)).length)
      .catch(() => 0)
    return {
      rootPath: this.rootPath,
      logPath: this.logPath,
      crashDumpPath: this.crashDumpPath,
      currentLogBytes,
      rotatedLogCount,
      maxFileBytes: this.maxFileBytes,
      maxRotatedFiles: this.maxRotatedFiles,
      rawMediaRecorded: this.existingLogs ? null : false,
      existingLogsVerified: !this.existingLogs,
      crashDumpsInspected: false,
      uploadEnabled: false
    }
  }

  private async append(line: string): Promise<void> {
    await mkdir(this.logDirectory, { recursive: true })
    const size = await stat(this.logPath).then((value) => value.size).catch(() => 0)
    if (size > 0 && size + Buffer.byteLength(line, 'utf8') > this.maxFileBytes) await this.rotate()
    await appendFile(this.logPath, line, { encoding: 'utf8', mode: 0o600 })
  }

  private async rotate(): Promise<void> {
    for (let index = this.maxRotatedFiles; index >= 1; index -= 1) {
      const source = index === 1 ? this.logPath : `${this.logPath}.${index - 1}`
      const target = `${this.logPath}.${index}`
      await rm(target, { force: true })
      await rename(source, target).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== 'ENOENT') throw error
      })
    }
  }
}

export function installProcessDiagnosticHandlers(recorder: DiagnosticRecorder): void {
  process.on('uncaughtExceptionMonitor', (error, origin) => {
    void recorder.record({ source: 'main', event: 'uncaught_exception', level: 'error', message: error.message, details: { origin, error } })
  })
  process.on('unhandledRejection', (reason) => {
    void recorder.record({ source: 'main', event: 'unhandled_rejection', level: 'error', details: { reason } })
  })
  process.on('warning', (warning) => {
    void recorder.record({ source: 'main', event: 'process_warning', level: 'warn', message: warning.message, details: { name: warning.name, stack: warning.stack } })
  })
}

for (const code of ['MODEL_OUTPUT_TRUNCATED', 'MODEL_OUTPUT_TERMINATED', 'MODEL_TIMEOUT', 'MODEL_NETWORK_ERROR', 'MODEL_INVALID_RESPONSE', 'MODEL_NOT_CONFIGURED', 'MODEL_REQUEST_FAILED', 'MODEL_ALL_ROUTES_UNAVAILABLE']) ERROR_CODES.add(code)

ERROR_CODES.add('MISTAKE_LEARNING_COACH_CHECK_INVALID')
