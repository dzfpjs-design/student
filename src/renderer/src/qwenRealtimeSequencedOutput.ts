import type { PcmChunk, TtsProvider, TtsRequest } from './companionVoice'
import { QwenRealtimeSampleOutput } from './qwenRealtimeSampleOutput'
import { qwenOutputObject as object, validateQwenOutputScope, type QwenOutputScope } from '../../shared/qwenOutputBridge'

/** Independent receiver accounting catches lost/reordered relay frames. This is
 * a qualification lane, not a production supplier or authorization switch. */
export class QwenRealtimeSequencedOutput implements TtsProvider {
  readonly id = 'qwen-realtime-sequenced-output-qualification'
  readonly isProductionVoice = true
  private readonly scope: QwenOutputScope
  private readonly inner: QwenRealtimeSampleOutput
  private sequence = 0
  private frames = 0
  private bytes = 0
  private terminal = false
  private retired = false
  private failure: Error | null = null

  constructor(owner: { scope: QwenOutputScope; isCurrent: () => boolean; close: () => void }) {
    this.scope = validateQwenOutputScope(owner.scope)
    this.inner = new QwenRealtimeSampleOutput({ responseId: this.scope.responseId, voice: this.scope.voice,
      isCurrent: owner.isCurrent, close: owner.close })
  }

  get generationEvidence() { return this.inner.generationEvidence }

  receive(value: unknown): 'accepted' | 'ignored' | 'retired' {
    if (this.retired || this.terminal) return 'retired'
    try { return this.receiveEnvelope(value) } catch { return this.fail('ENVELOPE_INVALID') }
  }

  private receiveEnvelope(value: unknown): 'accepted' | 'ignored' | 'retired' {
    const envelope = object(value), route = object(envelope?.scope), event = object(envelope?.event)
    if (!envelope || envelope.type !== 'qwen_output_bridge_v1' || !route || !event) return this.fail('ENVELOPE_INVALID')
    if ((Object.keys(this.scope) as (keyof QwenOutputScope)[]).some(field => route[field] !== this.scope[field])) return this.fail('SCOPE_MISMATCH')
    if (envelope.sequence !== this.sequence + 1) return this.fail('SEQUENCE_MISMATCH')
    let frames = this.frames, bytes = this.bytes
    if (event.type === 'response.audio.delta') {
      if (typeof event.delta !== 'string' || !event.delta.length || event.delta.length > 699052) return this.fail('COUNTS_MISMATCH')
      try { bytes += atob(event.delta).length; frames++ } catch { return this.fail('COUNTS_MISMATCH') }
    } else if (event.type !== 'response.audio.done' && event.type !== 'response.done') return this.fail('EVENT_INVALID')
    if (envelope.frameCount !== frames || envelope.pcmBytes !== bytes || envelope.sampleCount !== bytes / 2
      || frames > 4096 || bytes > 8000000) return this.fail('COUNTS_MISMATCH')
    const accepted = this.inner.receive(event)
    if (accepted !== 'accepted') { this.retired = true; return accepted }
    this.sequence++; this.frames = frames; this.bytes = bytes
    this.terminal = event.type === 'response.done'
    return accepted
  }

  transportEnded(): void { if (!this.terminal && !this.retired) this.fail('EOS_MISSING') }
  retire(): void { this.retired = true; this.inner.retire() }

  async *stream(request: TtsRequest): AsyncIterable<PcmChunk> {
    try { yield* this.inner.stream(request) }
    catch (error) { throw this.failure ?? error }
    finally { this.retired = true }
  }

  private fail(code: string): 'retired' {
    this.failure = new Error(`QWEN_BRIDGE_${code}`)
    this.retire()
    return 'retired'
  }
}
