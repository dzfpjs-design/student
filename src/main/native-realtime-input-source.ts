import { createHmac } from 'node:crypto'
import { nativeRealtimeInputSourceFields, nativeRealtimeInputSourceScopeFields,
  type NativeRealtimeInputSourceCommand, type NativeRealtimeInputSource } from '../shared/nativeRealtimeInputSource'

/** Private main-to-Runtime read only. No source text or signature over IPC. */
export function signNativeRealtimeInputSource(key: string | undefined, command: NativeRealtimeInputSourceCommand): string {
  if (!key || !/^[a-f0-9]{64}$/.test(key)) throw Error('NATIVE_REALTIME_INPUT_UNAVAILABLE')
  return createHmac('sha256', Buffer.from(key, 'hex')).update(JSON.stringify([
    'native_realtime_input_source_v1', ...nativeRealtimeInputSourceFields.map(field => command[field]),
  ]), 'utf8').digest('hex')
}

export async function readNativeRealtimeInputSource(command: NativeRealtimeInputSourceCommand, runtime: {
  status: string; localApiUrl: string; sessionToken: string
}, sign: (command: NativeRealtimeInputSourceCommand) => string): Promise<NativeRealtimeInputSource> {
  if (runtime.status !== 'ready' || !runtime.sessionToken) throw Error('NATIVE_REALTIME_INPUT_UNAVAILABLE')
  const url = new URL(runtime.localApiUrl)
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
    || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('NATIVE_REALTIME_INPUT_LOCAL_ONLY')
  try {
    const response = await fetch(`${url.origin}/v1/sessions/${encodeURIComponent(command.session_id)}/voice-input-source`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(5000), headers: {
        'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken,
      }, body: JSON.stringify({ ...command, signature: sign(command) }),
    })
    if (!response.ok || !response.body) throw Error('unconfirmed')
    const reader = response.body.getReader(), chunks: Uint8Array[] = []
    let bytes = 0
    try {
      for (;;) {
        const step = await reader.read()
        if (step.done) break
        bytes += step.value.byteLength
        if (bytes > 131072) throw Error('oversize')
        chunks.push(step.value)
      }
    } finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    if (!validNativeRealtimeInputSource(value, command)) throw Error('source changed')
    return value
  } catch { throw Error('NATIVE_REALTIME_INPUT_UNCONFIRMED') }
}

export function validNativeRealtimeInputSource(value: unknown, command: NativeRealtimeInputSourceCommand): value is NativeRealtimeInputSource {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const source = value as NativeRealtimeInputSource
  const fields = ['scope', 'status', 'transcript', 'source_hash', 'source_kind', 'onset_provenance',
    'arrival_status', 'local_only', 'cloud_transmission_allowed', 'replay_allowed', 'mastery_update_allowed']
  return Object.keys(source).length === fields.length && fields.every(field => Object.hasOwn(source, field))
    && !!source.scope && typeof source.scope === 'object' && !Array.isArray(source.scope)
    && Object.keys(source.scope).length === nativeRealtimeInputSourceScopeFields.length
    && nativeRealtimeInputSourceScopeFields.every(field => source.scope[field] === command[field])
    && source.status === 'completed' && source.source_kind === 'realtime_transcript'
    && ['client_reported', 'unknown'].includes(source.onset_provenance)
    && ['not_dispatched', 'current', 'superseded'].includes(source.arrival_status)
    && typeof source.transcript === 'string' && !!source.transcript.trim() && source.transcript.length <= 16000
    && typeof source.source_hash === 'string' && /^[a-f0-9]{64}$/.test(source.source_hash)
    && source.local_only === true && source.cloud_transmission_allowed === false
    && source.replay_allowed === false && source.mastery_update_allowed === false
}
