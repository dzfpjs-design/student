export type VoiceRuntimePolicy = Readonly<{ version: 1; capture: 'configured'|'bounded_segmented'|'blocked'; runtimeEpoch: number }>
/** Main-produced epoch is compared with the existing actual runtimeConfig. */
export function validVoiceRuntimePolicy(value: unknown, runtimeEpoch: unknown): value is VoiceRuntimePolicy {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const p = value as Record<string,unknown>
  return Object.keys(p).sort().join(',') === 'capture,runtimeEpoch,version' && p.version === 1
    && ['configured','bounded_segmented','blocked'].includes(p.capture as string)
    && Number.isSafeInteger(p.runtimeEpoch) && (p.runtimeEpoch as number) > 0 && p.runtimeEpoch === runtimeEpoch
}
