export type PetMedia = {
  version: string; media_id: string; child_id: string; session_id: string; prompt_id: string
  diagnostic_item_id: string; item_version: string; source_version: string
  kind: 'listening' | 'speaking'; revision: number; generation: number; status: string
  resume_required: boolean; frame_position: number; source_reviewed: boolean; receipt_grace_ms: number
  audio: { status: string; sha256: string | null; frames?: number; sample_rate?: number; channels?: number; sample_width?: number }
  receipt_retry_after_ms?: number
  pending_playback_receipt?: PetPlaybackReceipt | null
  qualification: { full_audio_control_reference_available?: boolean; listening_delivery_qualified: boolean; skill_qualified: boolean; blockers: string[];
    delivery_requirement?: { version: string };
    task_delivery_qualified?: boolean; required_play_count?: number | null; task_complete_play_count?: number }
  local_only: true; cloud_transmission_allowed: false
}
export type PetMediaEnvelope = { media: PetMedia; local_only: true; cloud_transmission_allowed: false }
export type PetActionReceipt = { child_id: string; session_id: string; media_id: string; client_action_id: string;
  replay_decision: 'not_recorded' | 'reuse_completed' | 'retry_pending_receipt' | 'await_recovery' | 'current_source_invalid';
  receipt: null | { generation: number; completion_accepted: boolean; request: PetPlaybackReceipt | PetPlaybackCommand };
  current: PetMedia; automatic_playback_allowed: false; local_only: true; cloud_transmission_allowed: false }
export type PetPromptMedia = { prompt_id: string; item_version: string; child_id: string; session_id: string;
  listening: PetMedia[]; speaking: PetMedia[]; selection_required: boolean; status: string;
  local_only: true; cloud_transmission_allowed: false }
export type PetPlaybackCommand = {
  media_id: string; generation: number; expected_revision: number; client_action_id: string
  command: 'pause' | 'resume' | 'restart' | 'cancel'
}
export type PetPlaybackReceipt = Omit<PetPlaybackCommand, 'command'> & {
  event: 'started' | 'ended'; audio_sha256: string; frame_position: number; monotonic_ms: number
}
