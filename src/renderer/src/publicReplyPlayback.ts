import { type CompanionSpeechOptions, type CompanionSpeechResult, type TtsProvider } from './companionVoice'
import { queueManagedReply, cancelLocalSpeech } from './LocalInputs'
import { bindPublicReplyProviderMetadata } from './managedPlaybackSourceMetadata'
import type { PublicReplySpeechSource } from '../../shared/publicReplySpeech'

/** The renderer supplies only original identifiers. Main obtains the text from
 * Runtime and returns complete, transcript-verified samples from its provider. */
export async function playPublicReply(source: PublicReplySpeechSource, message: string, options: CompanionSpeechOptions,
  onResult: (result: CompanionSpeechResult | 'unconfirmed' | 'interrupted') => void, onStarted?: () => void, onNativeEnd?:()=>void): Promise<'handled' | 'unavailable'> {
  const bridge = window.desktop
  if (!bridge?.openPublicReplySpeech || !bridge.generatePublicReplySpeech || !bridge.settlePublicReplySpeech
    || !bridge.cancelPublicReplySpeech || !bridge.onPublicReplySpeechRetired) return 'unavailable'
  const { openPublicReplySpeech, generatePublicReplySpeech, settlePublicReplySpeech,
    cancelPublicReplySpeech, onPublicReplySpeechRetired } = bridge
  if (options.isCurrent?.() === false) return 'handled'
  let opened
  try { opened = await openPublicReplySpeech(source) }
  catch { onResult('unconfirmed'); options.onNotDelivered?.('unavailable'); return 'handled' }
  if (opened.status === 'unavailable') return 'unavailable'
  if (opened.status === 'unconfirmed') { onResult('unconfirmed'); options.onNotDelivered?.('unavailable'); return 'handled' }
  const leaseId = opened.leaseId, speechKey = `runtime-public-${source.agentTurnId}`
  let retired = false, settled = false, receiptTail = Promise.resolve()
  const unsubscribe = onPublicReplySpeechRetired(id => {
    if (id !== leaseId || settled) return
    retired = true; cancelLocalSpeech(speechKey)
  })
  const current = () => !retired && (options.isCurrent?.() ?? true)
  const cancel = () => { void cancelPublicReplySpeech(leaseId).catch(() => undefined) }
  const finish = (event: 'rendered' | 'interrupted' | 'unconfirmed') => {
    if (settled) return
    settled = true; unsubscribe()
    if (event !== 'rendered') retired = true
    receiptTail = receiptTail.then(() => settlePublicReplySpeech(leaseId, event))
    void receiptTail.then(() => {
      if (event === 'rendered' && current()) { onResult('managed'); options.onPlaybackEnd?.() }
      else if (event !== 'rendered') {
        onResult(event === 'interrupted' ? 'interrupted' : 'unconfirmed')
        options.onNotDelivered?.(event === 'interrupted' ? 'cancelled' : 'unavailable')
      }
    }, () => { onResult('unconfirmed'); options.onNotDelivered?.('unavailable'); cancel() })
  }
  const provider: TtsProvider = { id: 'runtime-public-qwen-reply', isProductionVoice: true,
    async *stream(request) {
      let generated = false
      // After samples arrive, the arbiter's cancellation receipt owns the
      // terminal event. Retiring main first would turn a known interruption
      // into an unknown attempt and race that receipt.
      const aborted = () => { retired = true; if (!generated) cancel() }
      request.signal.addEventListener('abort', aborted, { once: true })
      try {
        if (request.signal.aborted || !current()) throw new DOMException('Cancelled', 'AbortError')
        const audio = await generatePublicReplySpeech(leaseId)
        if (request.signal.aborted || !current()) throw new DOMException('Cancelled', 'AbortError')
        if (audio.leaseId !== leaseId || audio.sampleRate !== 24000 || audio.generationCompleted !== true
          || audio.renderedCompleted !== false || !audio.frameCount || !audio.sampleCount || audio.pcmBase64.length > 10666668) throw Error('PUBLIC_REPLY_SAMPLES_INVALID')
        const encoded = atob(audio.pcmBase64), bytes = Uint8Array.from(encoded, char => char.charCodeAt(0))
        if (bytes.byteLength !== audio.sampleCount * 2 || bytes.byteLength > 8000000 || !bytes.byteLength || bytes.byteLength % 2) throw Error('PUBLIC_REPLY_SAMPLES_INVALID')
        generated = true
        for (let offset = 0; offset < bytes.length; offset += 16000) {
          if (request.signal.aborted || !current()) throw new DOMException('Cancelled', 'AbortError')
          yield { pcm16: bytes.slice(offset, offset + 16000).buffer, sampleRate: audio.sampleRate, channels: 1 }
        }
      } finally { request.signal.removeEventListener('abort', aborted) }
    },
  }
  if (!current()) { unsubscribe(); cancel(); return 'handled' }
  bindPublicReplyProviderMetadata(provider, { mainLeaseId: leaseId, sessionId: source.sessionId, childId: source.childId, agentTurnId: source.agentTurnId, speechKey })
  const result = await queueManagedReply(provider, message, source.sessionId, () => {
    if (!current() || settled) return
    receiptTail = receiptTail.then(() => settlePublicReplySpeech(leaseId, 'started'))
    // A lost started acknowledgment prevents an ended acknowledgment; no replay.
    void receiptTail.catch(() => { retired = true; cancelLocalSpeech(speechKey); cancel(); onResult('unconfirmed') })
    onStarted?.()
  }, { ...options, localOnly: false, managedVoiceRequired: true, speechKey, isCurrent: current,
    onPlaybackEnd: () => {onNativeEnd?.();finish('rendered')},
    onNotDelivered: reason => finish(reason === 'cancelled' || reason === 'superseded' ? 'interrupted' : 'unconfirmed') })
  if (result !== 'managed' && !settled) { unsubscribe(); cancel(); finish('unconfirmed') }
  return 'handled'
}
