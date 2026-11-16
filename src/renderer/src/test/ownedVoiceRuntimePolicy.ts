import { createVoiceRuntimePolicyHandler } from '../../../main/voice-runtime-policy'

/** Explicit synthetic host contract for existing Workspace tests.
 * This runs the original Main policy handler over one owned loopback runtime
 * and the original backend's canonical read-only response shape. It supplies
 * no renderer prop, source grant, paid slot, realtime admission or ASR result.
 * Real TCP hosts explicitly supply their native fetch and live runtime snapshot
 * so their policy request reaches the actual backend owner route.
 * Missing/invalid policy tests must construct their own incomplete host.
 */
export function ownedVoiceRuntimeConfig(localApiUrl = 'http://127.0.0.1:40117', sessionToken = 'synthetic-owned-runtime-token', runtimeEpoch = 1) {
  return Object.freeze({ status: 'ready' as const, localApiUrl,
    sessionToken, runtimeEpoch })
}
export function ownedVoiceRuntimePolicyDesktop<T extends object>(host: T = {} as T, runtimeSource: ReturnType<typeof ownedVoiceRuntimeConfig>|(() => ReturnType<typeof ownedVoiceRuntimeConfig>) = ownedVoiceRuntimeConfig(), policyFetch?: typeof fetch) {
  const currentRuntime = typeof runtimeSource === 'function' ? runtimeSource : () => runtimeSource
  const childOwner = Object.freeze({})
  const event = Object.freeze({ sender: Object.freeze({ id: 17 }) })
  const readPolicy = createVoiceRuntimePolicyHandler({
    current: () => ({ ...currentRuntime(), child: childOwner, revision: 0 }),
    allowedSender: value => value.sender.id === event.sender.id,
    fetch: policyFetch ?? (async (url, init) => {
      const runtime = currentRuntime()
      if (url !== runtime.localApiUrl + '/v1/private/money-egress' || init?.method !== 'GET'
        || init.redirect !== 'error' || init.cache !== 'no-store'
        || new Headers(init.headers).get('X-HSC-Session') !== runtime.sessionToken) {
        throw Error('OWNED_POLICY_FIXTURE_ORIGINAL_REQUEST_REQUIRED')
      }
      return new Response(JSON.stringify({ version: 1, capture: 'configured' }), {
        status: 200, headers: { 'Cache-Control': 'no-store' },
      })
    }),
  })
  return { getRuntimeConfig: async () => currentRuntime(), getVoiceRuntimePolicy: () => readPolicy(event), ...host }
}
