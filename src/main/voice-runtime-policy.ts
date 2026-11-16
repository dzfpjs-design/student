import type {VoiceRuntimePolicy} from '../shared/voiceRuntimePolicy'
type Snapshot = { status: string; localApiUrl: string; sessionToken: string; runtimeEpoch?: number; child: object|null; revision: number }
type Options = { current: () => Snapshot; allowedSender: (event: {sender:{id:number}}) => boolean; fetch?: typeof fetch }
const same = (a:Snapshot,b:Snapshot):boolean => a.status === 'ready' && b.status === 'ready' && a.child !== null && a.child === b.child
  && a.localApiUrl === b.localApiUrl && a.sessionToken === b.sessionToken && a.runtimeEpoch === b.runtimeEpoch && a.revision === b.revision
const valid = (a:Snapshot):boolean => a.status === 'ready' && !!a.child && !!a.sessionToken && Number.isSafeInteger(a.runtimeEpoch) && a.runtimeEpoch! > 0

/** One bounded read of the original backend owner; renderer sends no epoch/authority. */
export function createVoiceRuntimePolicyHandler(options:Options) {
  return async (event:{sender:{id:number}}):Promise<VoiceRuntimePolicy> => {
    if (!options.allowedSender(event)) throw Error('VOICE_RUNTIME_POLICY_UNAVAILABLE')
    const initial = options.current()
    if (!valid(initial)) throw Error('VOICE_RUNTIME_POLICY_UNAVAILABLE')
    const blocked = ():VoiceRuntimePolicy => {
      if (!options.allowedSender(event) || !same(initial,options.current())) throw Error('VOICE_RUNTIME_POLICY_UNAVAILABLE')
      return Object.freeze({version:1,capture:'blocked',runtimeEpoch:initial.runtimeEpoch!})
    }
    try {
      const url = new URL(initial.localApiUrl)
      if (url.protocol !== 'http:' || !['127.0.0.1','localhost','[::1]'].includes(url.hostname)
        || url.username || url.password || url.pathname !== '/' || url.search || url.hash) return blocked()
      const signal = AbortSignal.timeout(2000)
      const response = await (options.fetch ?? fetch)(`${url.origin}/v1/private/money-egress`, {method:'GET',redirect:'error',cache:'no-store',signal,
        headers:{'X-HSC-Session':initial.sessionToken}})
      if (!response.ok || !response.body) return blocked()
      const reader = response.body.getReader(); let raw = '', bytes = 0
      const decoder = new TextDecoder('utf-8',{fatal:true})
      try {
        for (;;) {
          const part = await reader.read(); if (part.done) break
          bytes += part.value.byteLength
          if (bytes > 128) return blocked()
          raw += decoder.decode(part.value,{stream:true})
        }
        raw += decoder.decode()
      } finally { await reader.cancel().catch(()=>undefined); reader.releaseLock() }
      if (signal.aborted || !options.allowedSender(event) || !same(initial,options.current())) return blocked()
      const value = JSON.parse(raw)
      // The original endpoint emits this canonical two-field body. Duplicate,
      // extra/old fields or unexpected encodings cannot overwrite policy.
      if (!value || Object.keys(value).sort().join(',') !== 'capture,version' || value.version !== 1
        || !['configured','bounded_segmented','blocked'].includes(value.capture) || JSON.stringify(value) !== raw) return blocked()
      return Object.freeze({version:1,capture:value.capture,runtimeEpoch:initial.runtimeEpoch!})
    } catch { return blocked() }
  }
}
