export const nativeRealtimeOutputFields = ['operation', 'session_id', 'child_id', 'agent_turn_id', 'connection_id',
  'call_id', 'voice', 'permit_id', 'storage_generation', 'response_id', 'issued_at'] as const
export const nativeRealtimeOutputScopeFields = [
  'session_id', 'child_id', 'agent_turn_id', 'connection_id', 'call_id', 'voice', 'permit_id', 'storage_generation',
] as const
export type NativeRealtimeOutputCommand = {
  operation: 'reserve' | 'bind' | 'inspect' | 'retire' | 'generated' | 'started' | 'rendered' | 'interrupted' | 'unconfirmed'; session_id: string; child_id: string; agent_turn_id: string
  connection_id: string; call_id: string; voice: string; permit_id: string; storage_generation: string; response_id: string; issued_at: string
  generation_evidence?: { sample_count: number; pcm_bytes: number; frame_count: number; pcm_sha256: string; text_sha256: string; model: string; voice: string;
    supplier_session_id: string; supplier_tool_response_id: string; supplier_tool_call_id: string }
}
