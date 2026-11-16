export type QwenOutputScope = {
  sessionId: string; childId: string; agentTurnId: string; callId: string; responseId: string; voice: string
}
export type QwenOutputEnvelope = {
  type: 'qwen_output_bridge_v1'; scope: QwenOutputScope; sequence: number
  frameCount: number; pcmBytes: number; sampleCount: number; event: Record<string, unknown>
}
export function validateQwenOutputScope(scope: QwenOutputScope): QwenOutputScope {
  const fields = ['sessionId', 'childId', 'agentTurnId', 'callId', 'responseId'] as const
  if (!scope || fields.some(field => typeof scope[field] !== 'string' || !/^[a-zA-Z0-9_-]{8,128}$/.test(scope[field]))
    || typeof scope.voice !== 'string' || !/^[a-zA-Z0-9._:-]{2,180}$/.test(scope.voice)) throw new Error('QWEN_OUTPUT_SCOPE_INVALID')
  return Object.fromEntries([...fields, 'voice'].map(field => [field, scope[field as keyof QwenOutputScope]])) as QwenOutputScope
}
export function qwenOutputObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
