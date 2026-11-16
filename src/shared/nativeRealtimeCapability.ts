export const nativeRealtimeCapabilityDomain = 'native_realtime_capability_request_v1'
export type NativeRealtimeOperation = 'catalog' | 'context' | 'input' | 'proposal' | 'receipt' | 'dialogue' | 'client'
export type NativeRealtimeCapabilityProof = {
  version: 1
  operation: NativeRealtimeOperation
  session_id: string
  voice_input_id: string
  connection_id: string
  connection_generation: number
  body_sha256: string
  issued_at: string
}
export const nativeRealtimeCapabilityProofFields = [
  'version', 'operation', 'session_id', 'voice_input_id', 'connection_id', 'connection_generation', 'body_sha256', 'issued_at',
] as const
export type NativeRealtimeForward = {
  session_id: string
  host_connection_id: string
  operation: NativeRealtimeOperation
  body?: Record<string, unknown>
  /** Host-only, one completion of an already claimed dispatch after its UI retires. */
  completion_lease?: string
}
