import { createHmac } from 'node:crypto'
import { nativeRealtimeOutputFields, nativeRealtimeOutputScopeFields, type NativeRealtimeOutputCommand } from '../shared/nativeRealtimeOutput'

/** Main/backend only; never expose this signer or its key over renderer IPC. */
export function signNativeRealtimeOutput(key: string | undefined, command: NativeRealtimeOutputCommand): string {
  if (!key || !/^[a-f0-9]{64}$/.test(key)) throw Error('NATIVE_REALTIME_AUTHORITY_UNAVAILABLE')
  return createHmac('sha256', Buffer.from(key, 'hex')).update(JSON.stringify([
    'native_realtime_output_v1', ...nativeRealtimeOutputFields.map(field => command[field]),
    ...('generation_evidence' in command ? [command.generation_evidence] : []),
  ]), 'utf8').digest('hex')
}

export async function executeNativeRealtimeOutput(command: NativeRealtimeOutputCommand, runtime: {
  status: string; localApiUrl: string; sessionToken: string
}, sign: (command: NativeRealtimeOutputCommand) => string): Promise<Record<string, unknown>> {
  if (runtime.status !== 'ready' || !runtime.sessionToken) throw Error('NATIVE_REALTIME_AUTHORITY_UNAVAILABLE')
  const url = new URL(runtime.localApiUrl)
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
    || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('NATIVE_REALTIME_LOCAL_ONLY')
  try {
    const response = await fetch(`${url.origin}/v1/sessions/${encodeURIComponent(command.session_id)}/voice-output-authority`, {
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
        if (bytes > 8192) throw Error('oversize')
        chunks.push(step.value)
      }
    } finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>
    const scope = value?.scope as Record<string, unknown> | undefined
    const fields = nativeRealtimeOutputScopeFields
    if (!value || !scope || fields.some(field => scope[field] !== command[field])) throw Error('scope changed')
    if (command.operation === 'reserve') {
      if (value.state !== 'reserved' || value.local_only !== false || value.cloud_transmission_allowed !== true
        || value.generation_completed !== false || value.rendered_completed !== false
        || typeof value.response_text !== 'string' || !value.response_text.trim() || value.response_text.length > 1000) throw Error('reserve invalid')
    } else if (command.operation === 'bind' && value.state === 'bound') {
      if (value.response_id !== command.response_id || value.dispatch_allowed !== true
        || value.generation_completed !== false || value.rendered_completed !== false) throw Error('bind invalid')
    } else if (!['reserved', 'bound', 'retired', 'revoked'].includes(String(value.state)) || value.dispatch_allowed !== false
      || 'response_text' in value) throw Error('readback invalid')
    return value
  } catch { throw Error('NATIVE_REALTIME_RECEIPT_UNCONFIRMED') }
}
