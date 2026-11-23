"""Closed export vocabulary snapshotted from reviewed local diagnostics sources.
No runtime source parsing or arbitrary field-name passthrough.
"""

LOGGER_SOURCE_SHA256 = 'eff763f4e2d26f587d32b86919eb8dd2bda73276e015263eb1cc1c2930234dc2'
ROUTES_SOURCE_SHA256 = 'f8205342306a4fbf62f1a9d54651ef8380e023c1a76f7d61159816747313ab14'
EVENTS = frozenset(('app_process_started',
 'app_quit_codec_disposed',
 'app_quit_diagnostics_flushed',
 'app_quit_final_requested',
 'app_quit_requested',
 'app_quit_service_closed',
 'app_quit_unload_prevented',
 'app_quit_will_quit',
 'data_key_unavailable',
 'device.connect.completed',
 'device.connect.failed',
 'diagnostic_folder_revealed',
 'document.capture.agent_dispatched',
 'document.capture.camera_ready',
 'document.capture.frame_blocked',
 'document.capture.frame_captured',
 'document.capture.frame_not_ready',
 'document.capture.ocr_completed',
 'document.capture.ocr_failed',
 'document.capture.ocr_started',
 'document.capture.open_failed',
 'document.capture.open_requested',
 'document.capture.retry_requested',
 'draft_codec_dispose_failed',
 'external_service_ready',
 'force_stop',
 'graceful_shutdown_failed',
 'local_api_request',
 'page_load_failed',
 'page_loaded',
 'pilot_email_claim_failed',
 'pilot_email_claim_invalid',
 'pilot_email_delivery_failed',
 'pilot_email_result_unknown',
 'pilot_email_sent',
 'primary_window_closed',
 'primary_window_created',
 'primary_window_ready_to_show',
 'process_closed',
 'process_exited',
 'process_gone',
 'process_spawned',
 'process_warning',
 'recovery_exhausted',
 'recovery_scheduled',
 'restart_requested',
 'runtime_missing',
 'service_ready',
 'start_requested',
 'startup_failed',
 'local_service.output_redaction_suppressed',
 'stderr',
 'stdout',
 'stop_requested',
 'structured_output_invalid',
 'trace_record_failed',
 'uncaught_exception',
 'unhandled_rejection',
 'voice.asr.completed',
 'voice.asr.echo_candidate_retained',
 'voice.asr.failed',
 'voice.asr.low_information_filtered',
 'voice.asr.non_speech_discarded',
 'voice.asr.playback_echo_discarded',
 'voice.asr.started',
 'voice.asr.transcript_dispatched',
 'voice.capture.playback_gate_ended',
 'voice.capture.playback_gate_started',
 'voice.capture.ready',
 'voice.capture.segment_queued',
 'voice.capture.signal_rejected',
 'voice.capture.utterance_detected',
 'voice.household.identity_selected',
 'voice.household.initialization_prompt_completed',
 'voice.household.initialization_prompt_started',
 'voice.household.microphone_resolution_failed',
 'voice.household.microphone_resolved',
 'voice.household.microphone_unresolved',
 'voice.household_listener.failed',
 'voice.onboarding.progress_saved',
 'voice.public_reply.failed',
 'voice.public_reply.usage',
 'voice.realtime.committed_input_dispatch',
 'voice.realtime.configured',
 'voice.realtime.duplex_capability',
 'voice.realtime.inactivity_standby',
 'voice.realtime.input_admitted',
 'voice.realtime.input_progress',
 'voice.realtime.input_rejected',
 'voice.realtime.input_source_checked',
 'voice.realtime.local_reply_boundary',
 'voice.realtime.microphone_fallback',
 'voice.realtime.reply_delivery',
 'voice.realtime.reply_first_audio',
 'voice.realtime.retry_exhausted',
 'voice.realtime.retry_scheduled',
 'voice.realtime.runtime_error',
 'voice.realtime.semantic_recovery',
 'voice.realtime.session_rotated',
 'voice.realtime.settings_read_failed',
 'voice.realtime.signaling_failed',
 'voice.realtime.signaling_succeeded',
 'voice.realtime.turn_dispatched',
 'voice.tts.arbitration',
 'voice.tts.delivery',
 'voice.wake_word.saved',
 'window_error',
 'window_responsive_after_stall',
 'window_unresponsive',
 'worker_line'))
ERROR_CODES = frozenset(('APP_RESTARTED',
 'ASR_NO_SPEECH',
 'ASR_TIMEOUT',
 'ASR_UNAVAILABLE',
 'ActivityVoiceError',
 'AgentExecutionConflict',
 'BOUND_DEVICE_NOT_FOUND',
 'CAMERA_BUSY',
 'CAMERA_NOT_FOUND',
 'CAMERA_PERMISSION_DENIED',
 'DEVICE_BINDING_UNAVAILABLE',
 'DEVICE_BUSY_OR_UNAVAILABLE',
 'DEVICE_COMMAND_FAILED',
 'DEVICE_COMMAND_TIMEOUT',
 'DEVICE_COMMAND_UNKNOWN',
 'DEVICE_NOT_BOUND',
 'DEVICE_NOT_FOUND',
 'DEVICE_NO_CONFIRMED_FRAME',
 'DEVICE_OPEN_FAILED',
 'DEVICE_PERMISSION_DENIED',
 'DEVICE_RECEIVER_UNAVAILABLE',
 'DEVICE_RESULT_UNCONFIRMED',
 'DRAFT_RECOVERY_IO',
 'EACCES',
 'EADDRINUSE',
 'ECONNREFUSED',
 'ECONNRESET',
 'ENOENT',
 'ETIMEDOUT',
 'INPUT_INVALID',
 'InputRevisionError',
 'KeyError',
 'LOCAL_SESSION_REQUIRED',
 'MEDIA_API_UNAVAILABLE',
 'MEDIA_PERMISSION_DENIED',
 'MICROPHONE_NOT_FOUND',
 'MICROPHONE_PERMISSION_DENIED',
 'MODEL_NETWORK_ERROR',
 'MODEL_NOT_CONFIGURED',
 'MODEL_RESPONSE_INVALID',
 'MODEL_TIMEOUT',
 'MODEL_UNAVAILABLE',
 'ModelResponseError',
 'OCR_FAILED',
 'OCR_TIMEOUT',
 'PARENT_AUTH_REQUIRED',
 'PARENT_CAPTURE_UNAVAILABLE',
 'PLAN_INPUT_REQUIRED',
 'RuntimeError',
 'SOURCE_CHANGED',
 'TRACE_RECORD_FAILED',
 'TaskStartBindingError',
 'UNSUPPORTED_DEVICE_MODE',
 'UnsupportedServerCapabilityError',
 'VERSION_CONFLICT',
 'ValueError'))
REASON_CODES = frozenset(('ASR_NO_HUMAN_SPEECH',
 'ASR_NO_MEANINGFUL_SPEECH',
 'ASR_NO_SPEECH',
 'QWEN_REALTIME_CONNECTION_FAILED',
 'QWEN_REALTIME_DATA_CHANNEL',
 'QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED',
 'QWEN_REALTIME_INPUT_IDENTITY_CAPACITY',
 'QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED',
 'QWEN_REALTIME_MEDIA_PLAYBACK_BLOCKED',
 'QWEN_REALTIME_MICROPHONE',
 'QWEN_REALTIME_PLAYBACK_COMPLETION_UNVERIFIED',
 'QWEN_REALTIME_PROVIDER_ERROR',
 'QWEN_REALTIME_RUNTIME_BYPASS',
 'QWEN_REALTIME_SESSION_EXPIRED',
 'QWEN_REALTIME_UNKNOWN'))
NUMBERS = frozenset(('agent_runtime_ms',
 'argumentCount',
 'attempt',
 'attempts',
 'audio_bytes',
 'capture_ms',
 'chars',
 'column',
 'confidence',
 'count',
 'delayMs',
 'durationMs',
 'duration_ms',
 'elapsedMs',
 'elapsed_ms',
 'elapsed_since_dispatch_ms',
 'errorCode',
 'exitCode',
 'first_audio_ms',
 'frame_count',
 'height',
 'image_bytes',
 'inactivity_seconds',
 'latency_ms',
 'line',
 'processId',
 'removed_chars',
 'sample_rate',
 'statusCode',
 'total_turn_ms',
 'transcript_chars',
 'webContentsId',
 'width'))
TOKEN_COUNTS = frozenset(('input_tokens', 'output_tokens', 'total_tokens'))
BOOLEANS = frozenset(('automation',
 'bound',
 'browser_aec',
 'cloud_transmission_allowed',
 'cloud_used',
 'delivered',
 'echo_candidate',
 'expected',
 'for_mistake',
 'full_duplex',
 'initialization_required',
 'isMainFrame',
 'local_only',
 'managed_voice_required',
 'parent_port',
 'raw_audio_or_video',
 'speech_detected',
 'woke'))
CONTAINERS = frozenset(('details', 'error', 'metrics', 'result'))
ENUMS = {
    'architecture': frozenset(('arm64', 'ia32', 'x64')),
    'client': frozenset(('configured_smtp',)),
    'delivery': frozenset(('browser', 'managed', 'system_female_offline', 'system_offline', 'text_only')),
    'device_type': frozenset(('camera', 'document_camera', 'microphone')),
    'errorType': frozenset(('CancelledError', 'HTTPException', 'KeyError', 'RuntimeError', 'TimeoutError', 'ValueError')),
    'exitSignal': frozenset(('SIGABRT', 'SIGBREAK', 'SIGHUP', 'SIGINT', 'SIGKILL', 'SIGQUIT', 'SIGTERM')),
    'input_stage': frozenset(('admission', 'callback_failed', 'callback_owner_checked', 'callback_returned', 'callback_started', 'dispatch', 'duplicate_discarded', 'empty_transcript', 'input_not_admitted', 'provider_completed')),
    'method': frozenset(('DELETE', 'GET', 'HEAD', 'OPTIONS', 'PATCH', 'POST', 'PUT')),
    'mode': frozenset(('conversation', 'household_wake', 'wake')),
    'name': frozenset(('AbortError', 'Error', 'NotAllowedError', 'NotFoundError', 'NotReadableError', 'OverconstrainedError', 'RangeError', 'SecurityError', 'TimeoutError', 'TypeError')),
    'observation_kind': frozenset(('local_pcm_scheduled', 'remote_pcm_with_active_player')),
    'platform': frozenset(('darwin', 'linux', 'win32')),
    'reason': frozenset(('abnormal-exit', 'clean-exit', 'crashed', 'integrity-failure', 'killed', 'launch-failed', 'occluded', 'oom')),
    'route': frozenset(('agent_runtime', 'committed_input', 'global', 'realtime_tool', 'reconnect_recovery', 'runtime_recovery', 'study')),
    'route_template': frozenset(('/v1/agent-turns/{turn_id}/execute', '/v1/agent-turns/{turn_id}/execution-status', '/v1/agent-turns/{turn_id}/outcome', '/v1/behavior/interventions/{intervention_id}/effect', '/v1/behavior/interventions/{intervention_id}/feedback', '/v1/bootstrap', '/v1/capture-artifacts/{artifact_id}/mistake-candidates', '/v1/capture-artifacts/{artifact_id}/mistake-candidates/prepare', '/v1/capture-artifacts/{artifact_id}/ocr-review-items', '/v1/children/{child_id}/behavior/baseline', '/v1/children/{child_id}/behavior/habit-experiments', '/v1/children/{child_id}/behavior/habit-experiments/{experiment_id}/history', '/v1/children/{child_id}/behavior/habit-experiments/{experiment_id}/stop', '/v1/children/{child_id}/behavior/interventions/decide', '/v1/children/{child_id}/behavior/personalization', '/v1/children/{child_id}/behavior/personalization/reset', '/v1/children/{child_id}/behavior/replay', '/v1/children/{child_id}/behavior/states/{state_id}/retract', '/v1/children/{child_id}/bounded-experiments', '/v1/children/{child_id}/daily-learning-summary', '/v1/children/{child_id}/knowledge-graph', '/v1/children/{child_id}/learning-state', '/v1/children/{child_id}/learning-state/{knowledge_node_id}/history', '/v1/children/{child_id}/mistake-capture-queues', '/v1/children/{child_id}/mistake-knowledge-candidates', '/v1/children/{child_id}/mistakes', '/v1/children/{child_id}/ocr-review-history', '/v1/children/{child_id}/ocr-review-queue', '/v1/children/{child_id}/ocr-review-truth', '/v1/children/{child_id}/period-reports', '/v1/children/{child_id}/period-reports/generate', '/v1/children/{child_id}/reports', '/v1/children/{child_id}/strategy-experiments', '/v1/children/{child_id}/strategy-experiments/candidate', '/v1/children/{child_id}/weakness-clusters', '/v1/children/{child_id}/weekly-goals', '/v1/device-bindings', '/v1/device-leases/{lease_id}/confirm-frame', '/v1/device-leases/{lease_id}/release', '/v1/device-leases/{lease_id}/renew', '/v1/exam-blueprints', '/v1/exam-simulations/{run_id}/answer', '/v1/exam-simulations/{run_id}/control', '/v1/health', '/v1/install-setup/status', '/v1/local-ai/status', '/v1/mistake-capture-candidates/{candidate_id}', '/v1/mistake-capture-candidates/{candidate_id}/capture', '/v1/mistake-capture-candidates/{candidate_id}/skip', '/v1/mistakes/capture', '/v1/mistakes/{mistake_id}/attempts', '/v1/mistakes/{mistake_id}/confirm', '/v1/mistakes/{mistake_id}/diagnose', '/v1/mistakes/{mistake_id}/diagnosis', '/v1/mistakes/{mistake_id}/diagnosis/answers', '/v1/mistakes/{mistake_id}/diagnosis/evidence', '/v1/mistakes/{mistake_id}/diagnosis/hypotheses', '/v1/mistakes/{mistake_id}/diagnosis/probes', '/v1/mistakes/{mistake_id}/diagnosis/retractions', '/v1/mistakes/{mistake_id}/next', '/v1/mistakes/{mistake_id}/remediation-steps', '/v1/ocr-review-items/batch', '/v1/ocr-review-items/{review_id}', '/v1/ocr-review-items/{review_id}/skip', '/v1/ocr-review-items/{review_id}/undo', '/v1/parent/backups', '/v1/parent/backups/portable', '/v1/parent/backups/portable/restore', '/v1/parent/backups/restore', '/v1/parent/capture-artifacts/{artifact_id}', '/v1/parent/capture-artifacts/{artifact_id}/raw-media', '/v1/parent/children/{child_id}/answer-attempt-ledger', '/v1/parent/children/{child_id}/capture-artifacts', '/v1/parent/children/{child_id}/companion-memory', '/v1/parent/children/{child_id}/companion-memory/{memory_id}', '/v1/parent/children/{child_id}/controlled-content/approvals', '/v1/parent/children/{child_id}/controlled-content/approvals/{row_id}', '/v1/parent/children/{child_id}/controlled-content/approved', '/v1/parent/children/{child_id}/controlled-content/candidates', '/v1/parent/children/{child_id}/controlled-content/rule', '/v1/parent/children/{child_id}/controlled-games/approvals', '/v1/parent/children/{child_id}/controlled-games/approvals/{approval_id}', '/v1/parent/children/{child_id}/controlled-games/rule', '/v1/parent/children/{child_id}/curriculum-capabilities', '/v1/parent/children/{child_id}/curriculum-topics', '/v1/parent/children/{child_id}/delete', '/v1/parent/children/{child_id}/deletion-preview', '/v1/parent/children/{child_id}/document-trial/frames/{artifact_id}/image', '/v1/parent/children/{child_id}/document-trial/frames/{artifact_id}/questions', '/v1/parent/children/{child_id}/document-trial/sessions', '/v1/parent/children/{child_id}/document-trial/sessions/{session_id}', '/v1/parent/children/{child_id}/exam-blueprint-readiness', '/v1/parent/children/{child_id}/exam-blueprint-readiness/prepare', '/v1/parent/children/{child_id}/execution-traces', '/v1/parent/children/{child_id}/execution-traces/{turn_id}', '/v1/parent/children/{child_id}/export', '/v1/parent/children/{child_id}/family-rule', '/v1/parent/children/{child_id}/goal-programs', '/v1/parent/children/{child_id}/goal-programs/{track}', '/v1/parent/children/{child_id}/goal-programs/{track}/roadmap', '/v1/parent/children/{child_id}/goal-programs/{track}/rule-review', '/v1/parent/children/{child_id}/knowledge-corpus/search', '/v1/parent/children/{child_id}/knowledge-sources', '/v1/parent/children/{child_id}/knowledge-sources/{source_id}/delete', '/v1/parent/children/{child_id}/longitudinal-model', '/v1/parent/children/{child_id}/longitudinal-model/corrections', '/v1/parent/children/{child_id}/longitudinal-model/history', '/v1/parent/children/{child_id}/mistakes/{mistake_id}/preserved-responses', '/v1/parent/children/{child_id}/model-calls', '/v1/parent/children/{child_id}/observation-calibrations', '/v1/parent/children/{child_id}/original-checks/{commitment_id}', '/v1/parent/children/{child_id}/original-checks/{commitment_id}/receipts/{action_id}', '/v1/parent/children/{child_id}/parent-actions/{snapshot_id}', '/v1/parent/children/{child_id}/parent-actions/{snapshot_id}/feedback', '/v1/parent/children/{child_id}/parent-actions/{snapshot_id}/history', '/v1/parent/children/{child_id}/period-evidence/retracted', '/v1/parent/children/{child_id}/period-evidence/{evidence_ref}/restore', '/v1/parent/children/{child_id}/period-evidence/{evidence_ref}/retract', '/v1/parent/children/{child_id}/period-reports/{period_report_id}/supply-followups', '/v1/parent/children/{child_id}/period-supply-followups/{snapshot_id}', '/v1/parent/children/{child_id}/pet-vocabulary', '/v1/parent/children/{child_id}/profile', '/v1/parent/children/{child_id}/report-delivery/channel-tests', '/v1/parent/children/{child_id}/report-delivery/channels/{channel}/test', '/v1/parent/children/{child_id}/report-delivery/channels/{channel}/test/keep', '/v1/parent/children/{child_id}/report-delivery/channels/{channel}/test/reissue', '/v1/parent/children/{child_id}/report-delivery/preferences', '/v1/parent/children/{child_id}/report-delivery/recipients', '/v1/parent/children/{child_id}/report-delivery/status', '/v1/parent/children/{child_id}/report-plan-adjustments/{adjustment_id}/review', '/v1/parent/children/{child_id}/report-plan-receipts/{receipt_id}', '/v1/parent/children/{child_id}/report-snapshots', '/v1/parent/children/{child_id}/report-snapshots/{snapshot_id}/feedback-history', '/v1/parent/children/{child_id}/report-snapshots/{snapshot_id}/feedback-retractions', '/v1/parent/children/{child_id}/report-snapshots/{snapshot_id}/plan-adjustments', '/v1/parent/children/{child_id}/report-source-issues', '/v1/parent/children/{child_id}/reports/retry', '/v1/parent/children/{child_id}/safety-events/{safety_event_id}', '/v1/parent/children/{child_id}/specialized-assessments', '/v1/parent/children/{child_id}/specialized-roadmaps', '/v1/parent/children/{child_id}/specialized-supply/candidates', '/v1/parent/children/{child_id}/specialized-supply/candidates/{candidate_id}/stage-review-context', '/v1/parent/children/{child_id}/specialized-supply/gaps', '/v1/parent/children/{child_id}/specialized-supply/prepare', '/v1/parent/children/{child_id}/specialized-supply/prepare-local', '/v1/parent/children/{child_id}/supply-followups/{root_id}', '/v1/parent/children/{child_id}/tutor-outcomes/{outcome_id}/review', '/v1/parent/children/{child_id}/variant-candidates', '/v1/parent/children/{child_id}/variant-jobs/{job_id}/retry', '/v1/parent/children/{child_id}/voice-calibration', '/v1/parent/children/{child_id}/work-supply-gaps/{job_id}', '/v1/parent/curriculum-capabilities/{capability_id}/review', '/v1/parent/curriculum-standards', '/v1/parent/curriculum-standards/import', '/v1/parent/curriculum-standards/search', '/v1/parent/dashboard', '/v1/parent/deletions/{deletion_id}/native-context', '/v1/parent/deletions/{deletion_id}/retry', '/v1/parent/device-bindings', '/v1/parent/device-qualification', '/v1/parent/device-qualification/runs', '/v1/parent/device-qualification/runs/{run_id}/finish', '/v1/parent/knowledge-corpora', '/v1/parent/knowledge-corpora/{corpus_id}', '/v1/parent/knowledge-corpora/{corpus_id}/coverage', '/v1/parent/knowledge-corpora/{corpus_id}/curriculum-standard-alignment', '/v1/parent/knowledge-corpora/{corpus_id}/internalization-jobs', '/v1/parent/knowledge-corpora/{corpus_id}/internalization-scopes', '/v1/parent/local-supply/refresh', '/v1/parent/longitudinal-model/corrections/{correction_id}/revoke', '/v1/parent/maintenance/run', '/v1/parent/model-pricing', '/v1/parent/model-runtime-policy', '/v1/parent/native-controlled-resources', '/v1/parent/native-controlled-resources/{kind}/{activity_id}', '/v1/parent/native-privacy-permits/fence', '/v1/parent/native-privacy-permits/read', '/v1/parent/observation-calibrations', '/v1/parent/observation-calibrations/{run_id}/cancel', '/v1/parent/observation-calibrations/{run_id}/frame', '/v1/parent/privacy', '/v1/parent/privacy-operations/generation', '/v1/parent/privacy-operations/{request_id}', '/v1/parent/privacy-operations/{request_id}/finalize', '/v1/parent/recovery-status', '/v1/parent/report-deliveries/process', '/v1/parent/report-deliveries/{delivery_id}/cancel', '/v1/parent/report-deliveries/{delivery_id}/retry', '/v1/parent/report-deliveries/{delivery_id}/unknown/keep', '/v1/parent/report-deliveries/{delivery_id}/unknown/reissue', '/v1/parent/report-delivery/channels', '/v1/parent/report-delivery/channels/{channel}', '/v1/parent/report-delivery/recipients', '/v1/parent/report-delivery/recipients/{recipient_id}', '/v1/parent/report-snapshots/{snapshot_id}', '/v1/parent/report-snapshots/{snapshot_id}/approve', '/v1/parent/report-snapshots/{snapshot_id}/carry-saved-edits', '/v1/parent/report-snapshots/{snapshot_id}/corrections', '/v1/parent/report-snapshots/{snapshot_id}/deliveries', '/v1/parent/report-snapshots/{snapshot_id}/feedback', '/v1/parent/report-snapshots/{snapshot_id}/revoke', '/v1/parent/reports/{report_id}/actions', '/v1/parent/reports/{report_id}/feedback', '/v1/parent/retention/run', '/v1/parent/safety-events/{safety_event_id}/acknowledge', '/v1/parent/sessions/{session_id}/sensor-release-verifications', '/v1/parent/setup', '/v1/parent/specialized-supply/candidates/{candidate_id}/review', '/v1/parent/specialized-supply/jobs/process-next', '/v1/parent/status', '/v1/parent/system-health', '/v1/parent/today', '/v1/parent/unlock', '/v1/parent/variant-candidates/{candidate_id}/review', '/v1/parent/variant-jobs/process-next', '/v1/parent/work-queue', '/v1/pilot-test-digest/due', '/v1/pilot-test-digest/{delivery_date}/result', '/v1/pilot-test-digest/{delivery_date}/submit-email', '/v1/plan-items/{plan_item_id}/checkpoint', '/v1/plan-items/{plan_item_id}/complete', '/v1/plan-items/{plan_item_id}/pause', '/v1/plan-items/{plan_item_id}/reschedule', '/v1/plan-items/{plan_item_id}/resume', '/v1/plan-items/{plan_item_id}/review-question', '/v1/plan-items/{plan_item_id}/start', '/v1/private/money-egress', '/v1/runtime/shutdown', '/v1/sessions', '/v1/sessions/{session_id}', '/v1/sessions/{session_id}/agent-capabilities', '/v1/sessions/{session_id}/agent-continuation', '/v1/sessions/{session_id}/agent-execution-recovery-index', '/v1/sessions/{session_id}/agent-request-receipts/{action_id}', '/v1/sessions/{session_id}/agent-runtime', '/v1/sessions/{session_id}/agent-turn', '/v1/sessions/{session_id}/agent-turns/{turn_id}/continuation-admission', '/v1/sessions/{session_id}/agent-turns/{turn_id}/continue', '/v1/sessions/{session_id}/agent-turns/{turn_id}/control-admission', '/v1/sessions/{session_id}/agent-turns/{turn_id}/document-source', '/v1/sessions/{session_id}/agent-turns/{turn_id}/original-client-admission', '/v1/sessions/{session_id}/agent/proactive-prompt', '/v1/sessions/{session_id}/agent/proactive-prompts/{prompt_id}/acknowledge', '/v1/sessions/{session_id}/agent/proactive-prompts/{prompt_id}/current', '/v1/sessions/{session_id}/asr', '/v1/sessions/{session_id}/asr/actions/{client_action_id}', '/v1/sessions/{session_id}/asr/actions/{client_action_id}/cancel', '/v1/sessions/{session_id}/behavior/habit-experiments/{experiment_id}/respond', '/v1/sessions/{session_id}/behavior/interventions/pending', '/v1/sessions/{session_id}/behavior/interventions/{intervention_id}/feedback', '/v1/sessions/{session_id}/behavior/personalization/reset', '/v1/sessions/{session_id}/break', '/v1/sessions/{session_id}/companion-activities/end', '/v1/sessions/{session_id}/companion-activities/start', '/v1/sessions/{session_id}/controlled-content/active', '/v1/sessions/{session_id}/controlled-content/end', '/v1/sessions/{session_id}/controlled-content/start', '/v1/sessions/{session_id}/controlled-games/active', '/v1/sessions/{session_id}/controlled-games/end', '/v1/sessions/{session_id}/controlled-games/start', '/v1/sessions/{session_id}/controlled-games/{game_session_id}/process', '/v1/sessions/{session_id}/controlled-launch-epoch', '/v1/sessions/{session_id}/controlled-launch-intents/{intent_id}', '/v1/sessions/{session_id}/controlled-launch-intents/{intent_id}/cancel-unstarted', '/v1/sessions/{session_id}/controlled-launch-intents/{intent_id}/native-not-dispatched', '/v1/sessions/{session_id}/deferred-tasks/{source_plan_item_id}/review', '/v1/sessions/{session_id}/device-incidents', '/v1/sessions/{session_id}/devices/register', '/v1/sessions/{session_id}/diagnostic-prompt', '/v1/sessions/{session_id}/diagnostic-prompt/answer', '/v1/sessions/{session_id}/diagnostic-prompt/cancel', '/v1/sessions/{session_id}/diagnostic-prompt/prepare', '/v1/sessions/{session_id}/diagnostic-prompt/resume', '/v1/sessions/{session_id}/diagnostics/answer', '/v1/sessions/{session_id}/diagnostics/next', '/v1/sessions/{session_id}/document-goals/{root_turn_id}/confirm', '/v1/sessions/{session_id}/document-goals/{root_turn_id}/review', '/v1/sessions/{session_id}/document-monitor', '/v1/sessions/{session_id}/document-monitor/events', '/v1/sessions/{session_id}/document-monitor/learning-context', '/v1/sessions/{session_id}/document-monitor/ocr', '/v1/sessions/{session_id}/document-monitor/pause', '/v1/sessions/{session_id}/document-monitor/receipts', '/v1/sessions/{session_id}/document-monitor/receipts/{operation}/{client_action_id}', '/v1/sessions/{session_id}/document-monitor/receipts/{operation}/{client_action_id}/acknowledge', '/v1/sessions/{session_id}/document-monitor/receipts/{operation}/{client_action_id}/prepare', '/v1/sessions/{session_id}/document-monitor/resume', '/v1/sessions/{session_id}/document-monitor/revisions', '/v1/sessions/{session_id}/document-monitor/start', '/v1/sessions/{session_id}/document-monitor/stop', '/v1/sessions/{session_id}/document-monitor/trial-frames', '/v1/sessions/{session_id}/drafts/{section}', '/v1/sessions/{session_id}/exam-simulations', '/v1/sessions/{session_id}/exam-simulations/active', '/v1/sessions/{session_id}/exam-simulations/answer-binding', '/v1/sessions/{session_id}/exam-simulations/bound-answer', '/v1/sessions/{session_id}/homework', '/v1/sessions/{session_id}/homework/none', '/v1/sessions/{session_id}/homework/{homework_id}', '/v1/sessions/{session_id}/learning-summary', '/v1/sessions/{session_id}/mistake-dialogue', '/v1/sessions/{session_id}/native-writing-original', '/v1/sessions/{session_id}/native-writing-receipt', '/v1/sessions/{session_id}/native-writing-resume', '/v1/sessions/{session_id}/observation-frame', '/v1/sessions/{session_id}/observation/correct', '/v1/sessions/{session_id}/observation/pause', '/v1/sessions/{session_id}/observation/qualification', '/v1/sessions/{session_id}/observation/qualification/start', '/v1/sessions/{session_id}/observation/qualification/unavailable', '/v1/sessions/{session_id}/observation/resume', '/v1/sessions/{session_id}/observation/stream/start', '/v1/sessions/{session_id}/observation/stream/stop', '/v1/sessions/{session_id}/ocr', '/v1/sessions/{session_id}/pet-vocabulary/answer', '/v1/sessions/{session_id}/pet-vocabulary/cancel', '/v1/sessions/{session_id}/pet-vocabulary/next', '/v1/sessions/{session_id}/pet-vocabulary/prompt/answer', '/v1/sessions/{session_id}/pet-vocabulary/prompt/cancel', '/v1/sessions/{session_id}/pet-vocabulary/prompt/{prompt_session_id}', '/v1/sessions/{session_id}/plan/generate', '/v1/sessions/{session_id}/plan/reorder', '/v1/sessions/{session_id}/planning-context', '/v1/sessions/{session_id}/recalls', '/v1/sessions/{session_id}/recalls/none', '/v1/sessions/{session_id}/recalls/{recall_id}/review', '/v1/sessions/{session_id}/reflection/complete', '/v1/sessions/{session_id}/report', '/v1/sessions/{session_id}/resume', '/v1/sessions/{session_id}/specialized-assessment', '/v1/sessions/{session_id}/switch-child', '/v1/sessions/{session_id}/task-start-choice', '/v1/sessions/{session_id}/transition', '/v1/sessions/{session_id}/tutor', '/v1/sessions/{session_id}/tutor-ocr-confirmations', '/v1/sessions/{session_id}/voice-calibration', '/v1/sessions/{session_id}/voice-input-source', '/v1/sessions/{session_id}/voice-interaction', '/v1/sessions/{session_id}/voice-interaction/recovery/handoff/invalidate', '/v1/sessions/{session_id}/voice-interaction/recovery/handoff/prepare', '/v1/sessions/{session_id}/voice-interaction/recovery/handoff/read', '/v1/sessions/{session_id}/voice-interaction/recovery/read', '/v1/sessions/{session_id}/voice-interaction/recovery/retry', '/v1/sessions/{session_id}/voice-output-authority', '/v1/sessions/{session_id}/voice-output-context/{turn_id}', '/v1/sessions/{session_id}/voice-playback', '/v1/sessions/{session_id}/voice/activity-focus', '/v1/sessions/{session_id}/voice/local-playback-control', '/v1/sessions/{session_id}/voice/realtime/actions/{action_id}', '/v1/sessions/{session_id}/voice/realtime/actions/{action_id}/resume', '/v1/sessions/{session_id}/voice/realtime/begin', '/v1/sessions/{session_id}/voice/realtime/complete', '/v1/sessions/{session_id}/voice/realtime/recovery-index', '/v1/sessions/{session_id}/voice/synthesize/stream', '/v1/sessions/{session_id}/writing-ocr', '/v1/sessions/{session_id}/writing-ocr/prepare', '/v1/sessions/{session_id}/writing-ocr/readback', '/v1/sessions/{session_id}/writing-ocr/retract', '/v1/sessions/{session_id}/writing-teaching', '/v1/sessions/{session_id}/writing-teaching/next-action', '/v1/standby/asr', '/v1/strategy-experiments/{experiment_id}/activate', '/v1/strategy-experiments/{experiment_id}/replay', '/v1/strategy-experiments/{experiment_id}/rollback', '/v1/strategy-experiments/{experiment_id}/shadow', '/v1/tutor-turns/{turn_id}/feedback')),
    'runtime': frozenset(('qwen_omni_realtime',)),
    'selection_method': frozenset(('spoken_grade', 'wake_word')),
    'session_state': frozenset(('check_in', 'classroom_recall', 'homework_capture', 'plan_review', 'reflection', 'study')),
    'source_status': frozenset(('current', 'missing', 'owner_changed', 'source_changed', 'unbound')),
    'status': frozenset(('cancelled', 'completed', 'connected', 'failed', 'pending', 'running', 'sent', 'unknown', 'waiting_child')),
    'step': frozenset(('completed', 'deferred', 'document_practice', 'finish_practice', 'study_practice', 'wake_practice')),
    'transport': frozenset(('smtp',)),
    'trigger': frozenset(('connect_failed', 'lease_renewal', 'status_error')),
}

# Closed daily-log mail outcomes; arbitrary provider/error text remains excluded.
ERROR_CODES = ERROR_CODES.union(('PILOT_ATTACHMENT_HASH_MISMATCH', 'PILOT_ATTACHMENT_INVALID', 'PILOT_ATTACHMENT_WINDOW_INVALID', 'PILOT_DIGEST_CLAIM_CHANGED', 'PILOT_DIGEST_SUBMISSION_ALREADY_STARTED', 'PILOT_LEGACY_ATTACHMENT_SNAPSHOT_MISSING', 'PILOT_RECIPIENT_ALIAS_AMBIGUOUS', 'PILOT_RECIPIENT_NOT_AUTHORIZED', 'PILOT_RECIPIENT_REVOKED', 'PILOT_SUBMISSION_OWNERSHIP_CHANGED', 'PILOT_SUBMISSION_PURPOSE_INVALID', 'PILOT_VERIFICATION_ACTION_CHANGED', 'PILOT_VERIFICATION_ACTION_CONFLICT', 'PILOT_VERIFICATION_ACTION_INVALID', 'PILOT_VERIFICATION_ACTION_REPLAY', 'PILOT_VERIFICATION_DATE_INVALID', 'PILOT_VERIFICATION_WINDOW_EMPTY', 'SERVER_SUBMISSION_OUTCOME_NOT_PERSISTED', 'SMTP_ATTACHMENT_INVALID', 'SMTP_ATTACHMENT_TOO_LARGE'))

# Finite ASR failure metadata; generated client names and raw text stay excluded.
REASON_CODES = REASON_CODES.union(('ASR_CLOUD_DIALOGUE_UNAVAILABLE', 'ASR_CLOUD_AUDIO_REVOKED', 'ASR_MODEL_NOT_READY', 'ASR_CLIENT_TIMEOUT', 'ASR_CLIENT_NETWORK_OR_PROTOCOL', 'ASR_CLIENT_UNKNOWN'))
REASON_CODES = REASON_CODES.union(('QWEN_PLAYBACK_ELEMENT_ERROR', 'QWEN_PLAYBACK_OBSERVER_CREATE_FAILED',
    'QWEN_PLAYBACK_OBSERVER_ACTIVITY_FAILED', 'QWEN_PLAYBACK_OBSERVER_QUIET_UNCONFIRMED',
    'QWEN_PLAYBACK_OBSERVER_MISSING', 'QWEN_PLAYBACK_OBSERVER_UNAVAILABLE', 'QWEN_PLAYBACK_PLAY_NOT_ALLOWED',
    'QWEN_PLAYBACK_PLAY_NOT_SUPPORTED', 'QWEN_PLAYBACK_PLAY_ABORTED', 'QWEN_PLAYBACK_PLAY_INVALID_STATE',
    'QWEN_PLAYBACK_PLAY_OTHER'))
ENUMS['failure_kind'] = frozenset(('api', 'timeout', 'network_or_protocol', 'client'))

# Exact speech-arbiter outcomes/priorities; arbitrary names and text stay excluded.
ENUMS['disposition'] = frozenset(('finished', 'unavailable', 'cancelled', 'expired', 'superseded', 'full'))
ENUMS['priority'] = frozenset(('reminder', 'dialogue', 'lesson', 'urgent'))

# Fixed local attention stages; no names, transcripts or arbitrary event prefix.
EVENTS = EVENTS.union(('voice.wake.control_started', 'voice.wake.control_result', 'voice.wake.detected'))

# Native cloud transport adds only closed operation names and numeric timing.
EVENTS = EVENTS.union(('voice.realtime.native_operation',))
ERROR_CODES = ERROR_CODES.union(('LOCAL_ASR_BUSY',))
ENUMS['native_operation'] = frozenset(('catalog', 'context', 'input', 'proposal', 'receipt', 'dialogue', 'client'))
ENUMS['route_template'] = ENUMS['route_template'].union(('/v1/sessions/{session_id}/voice/realtime/capabilities', '/v1/sessions/{session_id}/voice/realtime/native-context', '/v1/sessions/{session_id}/voice/realtime/native-input', '/v1/sessions/{session_id}/voice/realtime/capability-proposals', '/v1/sessions/{session_id}/voice/realtime/capability-receipt', '/v1/sessions/{session_id}/voice/realtime/native-dialogue', '/v1/sessions/{session_id}/voice/realtime/native-client'))

REASON_CODES = REASON_CODES.union(('QWEN_NATIVE_SOCKET_UNCONFIRMED', 'QWEN_NATIVE_SOCKET_OWNER_CHANGED', 'QWEN_NATIVE_SOCKET_POLICY_UNCONFIRMED', 'QWEN_NATIVE_SOCKET_PROVIDER_ERROR', 'QWEN_NATIVE_SOCKET_PAYLOAD_INVALID', 'QWEN_NATIVE_SOCKET_BACKPRESSURE', 'QWEN_NATIVE_SOCKET_SEND_UNCONFIRMED'))
REASON_CODES = REASON_CODES.union(('QWEN_NATIVE_CREDENTIAL_REJECTED', 'QWEN_NATIVE_ACCESS_DENIED', 'QWEN_NATIVE_ACCOUNT_BALANCE'))

# Dedicated unassigned-home ASR metadata only; no transcript or receipt token.
ENUMS['route_template'] = ENUMS['route_template'].union(('/v1/household/voice-entry/asr',))

ENUMS['native_phase'] = frozenset(('claim','execute','complete','read'))
ENUMS['native_capability'] = frozenset(['behavior.feedback', 'behavior.reset_personalization', 'break.begin', 'break.resume', 'checkin.update_context', 'companion.activity_feedback', 'companion.end_activity', 'companion.start_activity', 'controlled_content.play', 'controlled_content.stop', 'controlled_game.play', 'controlled_game.stop', 'conversation.respond', 'device.connect', 'device.status', 'diagnostic.cancel', 'diagnostic.replay', 'diagnostic.start', 'diagnostic.submit', 'document.cancel_capture', 'document.capture_question', 'document.confirm_ocr', 'document.correct_ocr', 'document.monitor_control', 'document.retry_capture', 'document.set_occlusion_reminders', 'exam.cancel', 'exam.pause', 'exam.resume', 'exam.start', 'exam.submit', 'homework.add', 'homework.add_batch', 'homework.none', 'homework.review_carryover', 'homework.update_duration', 'learning.capture_question', 'learning.evaluate_response', 'learning.explain', 'learning.prepare_request', 'learning.request_response', 'learning.review_revisit', 'learning.review_schedule', 'learning_inputs.cancel_revision', 'learning_inputs.confirm_revision', 'learning_inputs.propose_revision', 'learning_inputs.review_reclaimed_scope', 'learning_inputs.review_unplaced', 'learning_inputs.select_target', 'mistake.activity_control', 'mistake.capture_control', 'mistake.complete_remediation', 'mistake.diagnose', 'mistake.dialogue', 'mistake.open', 'mistake.repair_question', 'mistake.review', 'mistake.submit_response', 'native_writing.submission_status', 'native_writing.submit_retained', 'observation.correct', 'observation.pause', 'observation.resume', 'onboarding.resume', 'oral.independent_check', 'oral.retry', 'pet.listening_control', 'pet_vocabulary.cancel', 'pet_vocabulary.start', 'pet_vocabulary.submit', 'planning.describe', 'planning.generate', 'planning.reorder', 'privacy.pause_all', 'profile.switch', 'recall.add', 'recall.add_batch', 'recall.none', 'recall.review_pending', 'reflection.complete', 'reflection.update', 'safety.find_parent', 'session.request_end', 'session.snooze', 'session.transition', 'speech.repeat', 'speech.stop', 'system.inspect_learning', 'task.complete', 'task.pause', 'task.reschedule', 'task.resume', 'task.start', 'tutor.capture_check_response', 'tutor.more_hint', 'tutor.playback_control', 'tutor.record_feedback', 'tutor.retry_check_coaching', 'voice.configure_wake_word', 'workspace.navigate', 'writing.confirm_original', 'writing.independent_check', 'writing.read_original', 'writing.revise_original'])

# Cloud formative mistake learning is a concrete registered capability.
ENUMS['native_capability'] = ENUMS['native_capability'].union(('mistake.learn',))

# Formative learning: closed metadata vocabulary, no learning content.
EVENTS = EVENTS.union(('mistake.learning.model_result', 'mistake.learning.action_result'))
ERROR_CODES = ERROR_CODES.union(('MISTAKE_LEARNING_ACTION_CONFLICT', 'MISTAKE_LEARNING_ACTION_NOT_FOUND', 'MISTAKE_LEARNING_ARGUMENT_INVALID', 'MISTAKE_LEARNING_CANCELLED', 'MISTAKE_LEARNING_CHILD_NOT_FOUND', 'MISTAKE_LEARNING_CLOUD_CONSENT_REQUIRED', 'MISTAKE_LEARNING_DAILY_SHARE_COMPLETE', 'MISTAKE_LEARNING_EXPLANATION_FIRST', 'MISTAKE_LEARNING_FOCUS_CHANGED', 'MISTAKE_LEARNING_MODEL_FAILED', 'MISTAKE_LEARNING_MODEL_OUTPUT_INVALID', 'MISTAKE_LEARNING_NATIVE_AUTHORITY_REQUIRED', 'MISTAKE_LEARNING_NOTHING_DUE', 'MISTAKE_LEARNING_OUTCOME_UNKNOWN', 'MISTAKE_LEARNING_PRACTICE_CHANGED', 'MISTAKE_LEARNING_PRACTICE_CHECK_FAILED', 'MISTAKE_LEARNING_REVISION_CHANGED', 'MISTAKE_LEARNING_REVISION_INVALID', 'MISTAKE_LEARNING_SELECTION_REQUIRED', 'MISTAKE_LEARNING_SESSION_CHANGED', 'MISTAKE_LEARNING_SESSION_REQUIRED', 'MISTAKE_LEARNING_SOURCE_CHANGED', 'MISTAKE_LEARNING_SOURCE_NOT_FOUND', 'MISTAKE_LEARNING_TEXT_INVALID', 'MISTAKE_LEARNING_UNANSWERED_ITEM_REQUIRED', 'MODEL_MISTAKE_REASONING_CONFIG_REQUIRED', 'MISTAKE_LEARNING_CLOUD_LABEL_RIGHT_REQUIRED'))
ENUMS['request_type'] = frozenset(('mistake_learning_analyze', 'mistake_learning_practice', 'mistake_learning_coach', 'mistake_learning_answer', 'mistake_learning_verify'))
ENUMS['mistake_status'] = frozenset(('completed', 'failed', 'unknown', 'cancelled'))
ENUMS['model_provider'] = frozenset(('qwen', 'deepseek'))
ENUMS['model_name'] = frozenset(('qwen3.7-plus', 'qwen3.8-flash', 'deepseek-v4-pro', 'deepseek-v4.1-flash'))
ENUMS['route_template'] = ENUMS['route_template'].union(('/v1/children/{child_id}/mistake-learning', '/v1/mistakes/{mistake_id}/learning', '/v1/mistakes/{mistake_id}/learning/actions/{client_action_id}', '/v1/mistakes/{mistake_id}/learning/{operation}', '/v1/children/{child_id}/mistake-learning/cards', '/v1/children/{child_id}/mistake-learning/focus/release'))
ENUMS['request_type'] = ENUMS['request_type'].union(('mistake_learning_topic',))
ENUMS['route_template'] = ENUMS['route_template'].union(('/v1/children/{child_id}/mistake-learning/topics/explain',))
ERROR_CODES = ERROR_CODES.union(('MISTAKE_TOPIC_SOURCE_CHANGED', 'MISTAKE_TOPIC_SOURCE_MISMATCH',
    'MISTAKE_TOPIC_REQUEST_INVALID', 'MISTAKE_TOPIC_SENSITIVE_SOURCE', 'MISTAKE_TOPIC_GENERATION_FAILED',
    'MISTAKE_TOPIC_MODEL_OUTPUT_INVALID'))

ERROR_CODES = ERROR_CODES.union(('MODEL_OUTPUT_TRUNCATED', 'MODEL_OUTPUT_TERMINATED', 'MODEL_TIMEOUT', 'MODEL_NETWORK_ERROR', 'MODEL_INVALID_RESPONSE', 'MODEL_NOT_CONFIGURED', 'MODEL_REQUEST_FAILED', 'MODEL_ALL_ROUTES_UNAVAILABLE'))

ERROR_CODES = ERROR_CODES.union(('MISTAKE_LEARNING_COACH_CHECK_INVALID',))

# Closed renderer-native response/echo summary. No content or supplier identities.
EVENTS = EVENTS.union(('voice.realtime.echo_cancellation_policy','voice.realtime.native_response'))
ENUMS['echo_mode'] = frozenset(('all','remote-only','browser-selected','disabled','unknown'))
ENUMS['echo_negotiation'] = frozenset(('not_supported','accepted','rejected','timed_out','ended'))
ENUMS['response_status'] = frozenset(('completed','cancelled','failed','incomplete'))
ENUMS['response_reason'] = frozenset(('none','max_output_tokens','content_filter','client_cancelled','turn_detected','other'))
ENUMS['cancel_source'] = frozenset(('speech_onset','confirmed_input','response_binding','other_local'))
ENUMS['response_text_kind'] = frozenset(('empty','whitespace','punctuation','substantive'))
NUMBERS = NUMBERS.union(('response_elapsed_ms','response_text_chars','audio_chunk_count','pcm_chunk_count','first_pcm_ms'))
BOOLEANS = BOOLEANS.union(('response_error_present','new_input_before_pcm','terminal_current','has_tool_call','pcm_scheduled'))

# Exact local native failure reasons; no provider text or identities.
REASON_CODES = REASON_CODES.union(('QWEN_NATIVE_SESSION_CONFIG_TIMEOUT', 'QWEN_NATIVE_SESSION_CONFIG_UNCONFIRMED', 'QWEN_NATIVE_SESSION_IDENTITY_UNCONFIRMED', 'QWEN_NATIVE_HOUSEHOLD_INPUT_UNCONFIRMED', 'QWEN_NATIVE_RESTORED_INPUT_UNCONFIRMED', 'QWEN_NATIVE_INPUT_ADMISSION_UNCONFIRMED', 'QWEN_NATIVE_RESPONSE_IDENTITY_UNCONFIRMED', 'QWEN_NATIVE_INPUT_SOURCE_RESTRICTED', 'QWEN_NATIVE_INPUT_BACKPRESSURE', 'QWEN_NATIVE_TOOL_HANDOFF_UNCONFIRMED', 'QWEN_NATIVE_RUNTIME_CONTEXT_UNCONFIRMED', 'QWEN_NATIVE_AUDIO_PERMISSION_UNCONFIRMED', 'QWEN_NATIVE_AUDIO_OUTPUT_UNAVAILABLE', 'QWEN_NATIVE_DIALOGUE_SAVE_UNCONFIRMED', 'QWEN_NATIVE_PROTOCOL_CAPACITY'))

# Reviewed fixed startup/recovery producer metadata. Never raw identities or originals.
EVENTS = EVENTS.union(('runtime_awake_unconfirmed', 'protected_startup_failed', 'voice.realtime.pending_input_recovery', 'voice.realtime.cancellation_journal_unconfirmed', 'voice.agent.identity_index_restored', 'voice.agent.identity_index_unconfirmed', 'voice.realtime.identity_index_restored', 'voice.realtime.identity_index_unconfirmed', 'voice.realtime.cancellation_recovered', 'voice.realtime.cancellation_recovery_unconfirmed', 'voice.realtime.cancellation_recovery_invalid', 'voice.recovery.receipt_check_incomplete', 'voice.realtime.resume_receipt_retained', 'voice.original_prompt.admission', 'voice.capture.protected_tail_window_failed', 'app_quit_backend_abnormal_acknowledged'))
EVENTS = EVENTS.union(('restart_after_abnormal_shutdown',))
# Fixed wrapper producers only; dynamic IPC/child event values stay closed.
EVENTS = EVENTS.union(('structured_event', 'document.capture.frame_deferred', 'voice.capture.protected_tail_failed', 'voice.capture.unknown_overlap_held', 'voice.capture.echo_cancellation_policy', 'voice.capture.original_prefix_admitted', 'voice.capture.original_prefix_held', 'voice.capture.original_prefix_discarded'))
REASON_CODES = REASON_CODES.union(('quiet_not_verified','source_or_close_unknown'))
ENUMS['reason'] = ENUMS['reason'].union(('transient_occlusion',))
EVENTS = EVENTS.union(('document.capture.frame_state', 'document.capture.ocr_pending'))
EVENTS = EVENTS.union(('document.monitor.frame_state', 'document.monitor.frame_stalled', 'document.monitor.ocr_started', 'document.monitor.ocr_pending',
    'document.monitor.ocr_result', 'document.monitor.ocr_discarded', 'document.monitor.ocr_retry_considered',
    'document.monitor.revision_saved', 'document.monitor.revision_inflight_interrupted', 'document.monitor.revision_historical',
    'document.monitor.ocr_failed', 'document.monitor.ocr_unconfirmed', 'document.monitor.revision_failed',
    'document.monitor.revision_unconfirmed', 'document.monitor.post_revision_failed'))
ENUMS['protected_startup_reason'] = frozenset(('storage', 'media_key_recovery', 'rotation_model_unclassified', 'tutor_recovery_marker'))
ENUMS['frame_state'] = frozenset(('no_page', 'stabilizing', 'writing', 'occluded', 'page_changed', 'stable'))
ENUMS['frame_motion_band'] = frozenset(('lt_2', '2_to_4_5', '4_5_to_7', '7_to_11', 'ge_11'))
ENUMS['frame_skin_band'] = frozenset(('lt_025', '025_to_10', '10_to_22', 'ge_22'))
ENUMS['frame_content_motion'] = frozenset(('changed', 'unchanged', 'unavailable'))
ENUMS['frame_paper_band'] = frozenset(('lt_10', 'ge_10'))
ENUMS['frame_light_band'] = frozenset(('dark', 'usable'))
# Existing fixed structured stdout producers; no dynamic event passthrough.
EVENTS = EVENTS.union(('privacy_database_delete_failed', 'privacy_analysis_candidate_residual', 'privacy_delete_dependency_blocked', 'learning_adaptation_pending', 'document_learning_worker_error', 'speaking_candidate_worker_error', 'pet_audio_worker_error'))
ERROR_CODES = ERROR_CODES.union(('RUNTIME_AWAKE_UNCONFIRMED','LOCAL_SERVICE_SHUTDOWN_PENDING','LOCAL_SERVICE_SHUTDOWN_ABNORMAL'))
BOOLEANS = BOOLEANS.union(('has_more','allowed','capture_ready'))
ENUMS['scope'] = frozenset(('canonical_voice_only',))
ENUMS['window'] = frozenset(('initial','recovery'))
EVENTS = EVENTS.union(('voice.realtime.socket_open_failed', 'voice.realtime.socket_message_rejected'))
ENUMS['socket_open_stage'] = frozenset(('policy', 'owner', 'configuration', 'preflight', 'dial', 'handshake', 'post_open_policy'))
ENUMS['socket_open_failure'] = frozenset(('timeout', 'transport_error', 'http_rejected', 'socket_closed', 'other'))
ENUMS['socket_open_retirement'] = frozenset(('none', 'provider_error', 'policy_unconfirmed', 'owner_changed', 'invalid_payload', 'unconfirmed', 'closed'))
ENUMS['socket_payload_failure'] = frozenset(('none', 'send_missing_socket', 'send_not_opened', 'send_not_ready', 'send_invalid', 'send_empty', 'send_oversize', 'message_conversion', 'message_binary', 'message_oversize', 'message_json', 'message_structure', 'message_other'))
ENUMS['socket_message_top'] = frozenset(('null', 'array', 'object', 'scalar'))
ENUMS['socket_message_type'] = frozenset(('missing', 'non_string', 'not_object'))
for key in ('socket_message_header', 'socket_message_payload', 'socket_message_error', 'socket_message_code', 'socket_message_message', 'socket_message_event'):
    ENUMS[key] = frozenset(('missing', 'object', 'other', 'not_object'))
for key in ('socket_message_code_type', 'socket_message_message_type'):
    ENUMS[key] = frozenset(('missing', 'not_object', 'null', 'string', 'number', 'boolean', 'array', 'object'))
ENUMS['socket_provider_category'] = frozenset(('credential', 'access', 'balance', 'rate_limit', 'model_not_found', 'resource_not_found', 'invalid_parameter', 'quota', 'provider_unavailable', 'unknown'))
ENUMS['reason'] = ENUMS['reason'].union(('deadline','read_failed'))

# Dedicated tutoring route may be deliberately unconfigured during private migration.
ERROR_CODES = ERROR_CODES.union(("MODEL_TUTOR_EXPLANATION_CONFIG_REQUIRED",))

# The legacy original route logs only its template, never report/child identifiers.
ENUMS['route_template'] = ENUMS['route_template'].union(('/v1/parent/children/{child_id}/legacy-daily-reports/{report_id}/original', '/v1/parent/children/{child_id}/legacy-daily-reports/{report_id}/original/status'))
ENUMS['route_template'] = ENUMS['route_template'].union(('/v1/sessions/{session_id}/document-monitor/previews',))
ENUMS['route_template'] = ENUMS['route_template'].union(('/v1/sessions/{session_id}/document-monitor/previews/{action_id}/resolve',))
EVENTS = EVENTS.union(tuple('document.preview.' + event for event in (
    'started', 'saved', 'unconfirmed', 'resolve_saved', 'resolve_retired', 'resolve_source_blocked', 'refresh_due')))
ERROR_CODES = ERROR_CODES.union(('DOCUMENT_PREVIEW_RESULT_UNCONFIRMED', 'DOCUMENT_PREVIEW_SOURCE_CHANGED',
    'DOCUMENT_PREVIEW_ACTION_CONFLICT', 'DOCUMENT_PREVIEW_RETIRED', 'DOCUMENT_PREVIEW_OCR_FAILED',
    'DOCUMENT_PREVIEW_RESOLUTION_MISMATCH', 'DOCUMENT_OCR_BUSY'))

# Scene-boundary explanations carry only closed reasons and bounded aggregates.
EVENTS = EVENTS.union(('document.preview.page_boundary',))
EVENTS = EVENTS.union(('document.preview.scene_change',))
ENUMS['preview_scene_reason'] = frozenset(('broad_spatial_change', 'page_changed_signal',
    'blackout_or_page_missing', 'reference_returned', 'registered'))
ENUMS['preview_boundary_reason'] = frozenset((
    'initial', 'source_changed', 'counter_reset', 'blackout_or_page_missing',
    'page_changed_signal', 'broad_spatial_change', 'explicit_invalidation',
))
PREVIEW_BOUNDARY_NUMERIC_LIMITS = {
    'strong_cells': 48, 'quadrants': 4, 'mean_residual': 255,
    'exposure_offset_abs': 255, 'missing_elapsed_ms': 300_000, 'scene_elapsed_ms': 300_000,
}
PREVIEW_BOUNDARY_COUNT_KEYS = frozenset(('strong_cells', 'quadrants'))
NUMBERS = NUMBERS.union(PREVIEW_BOUNDARY_NUMERIC_LIMITS)
