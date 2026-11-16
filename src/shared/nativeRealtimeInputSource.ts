export const nativeRealtimeInputSourceScopeFields = ['session_id', 'child_id', 'client_action_id',
  'voice_input_id', 'connection_id', 'storage_generation'] as const
export const nativeRealtimeInputSourceFields = [...nativeRealtimeInputSourceScopeFields, 'issued_at'] as const

export type NativeRealtimeInputSourceScope = Record<typeof nativeRealtimeInputSourceScopeFields[number], string>
export type NativeRealtimeInputSourceCommand = NativeRealtimeInputSourceScope & { issued_at: string }
export type NativeRealtimeInputSource = {
  scope: NativeRealtimeInputSourceScope
  status: 'completed'; transcript: string; source_hash: string
  source_kind: 'realtime_transcript'; onset_provenance: 'client_reported' | 'unknown'
  arrival_status: 'not_dispatched' | 'current' | 'superseded'
  local_only: true; cloud_transmission_allowed: false; replay_allowed: false; mastery_update_allowed: false
}
