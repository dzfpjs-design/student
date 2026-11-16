import { qwenOutputObject as object, validateQwenOutputScope, type QwenOutputEnvelope, type QwenOutputScope } from '../shared/qwenOutputBridge'

const identity = (value: unknown): value is string => typeof value === 'string' && /^[a-zA-Z0-9_-]{8,128}$/.test(value)

/** Qualification relay. The future Runtime transport must supply a trusted grant;
 * renderer-provided scope and permission flags are not an authorization endpoint. */
export class QwenOutputRelay {
  private readonly scope: QwenOutputScope
  private sequence = 0
  private frames = 0
  private bytes = 0
  private itemId: string | null = null
  private audioDone = false
  private ended = false
  private delivering = false
  private eventIds = new Set<string>()

  constructor(private readonly owner: {
    scope: QwenOutputScope; localOnly: boolean; cloudTransmissionAllowed: boolean
    isCurrent: () => boolean; emit: (event: QwenOutputEnvelope) => void; onFailure: (code: string) => void
  }) {
    if (owner.localOnly !== false || owner.cloudTransmissionAllowed !== true) throw Error('QWEN_OUTPUT_LOCAL_ONLY')
    this.scope = validateQwenOutputScope(owner.scope)
  }

  receive(value: unknown): 'accepted' | 'ignored' | 'retired' {
    if (this.ended) return 'retired'
    if (this.delivering) return this.fail('REENTRANT')
    try {
      if (this.owner.localOnly !== false || this.owner.cloudTransmissionAllowed !== true) return this.fail('LOCAL_ONLY')
      if (!this.owner.isCurrent()) return this.fail('OWNER_RETIRED')
      const event = object(value)
      if (!event || typeof event.type !== 'string') return this.fail('EVENT_INVALID')
      if (event.type === 'error') return this.fail('SOURCE_FAILED')
      if (event.type.startsWith('response.function_call')) return this.fail('TOOL_UNAUTHORIZED')
      if (!['response.audio.delta', 'response.audio.done', 'response.done'].includes(event.type)) return 'ignored'
      if (!identity(event.event_id) || this.eventIds.has(event.event_id)) return this.fail('EVENT_REPLAYED')
      this.eventIds.add(event.event_id)
      if (this.eventIds.size > 4098) return this.fail('LIMIT')
      if (event.type === 'response.done') {
        const response = object(event.response)
        const outputs = response?.output
        const entry = Array.isArray(outputs) && outputs.length === 1 ? object(outputs[0]) : null
        const contents = entry?.content
        const audio = Array.isArray(contents) && contents.length === 1 ? object(contents[0]) : null
        if (!this.audioDone || !this.frames || response?.id !== this.scope.responseId || response?.status !== 'completed'
          || response?.voice !== this.scope.voice || entry?.id !== this.itemId || entry?.type !== 'message'
          || entry?.role !== 'assistant' || entry?.status !== 'completed' || audio?.type !== 'audio') return this.fail('COMPLETION_INVALID')
      } else {
        if (event.response_id !== this.scope.responseId || !identity(event.item_id) || event.output_index !== 0
          || event.content_index !== 0 || (this.itemId && event.item_id !== this.itemId)) return this.fail('IDENTITY_INVALID')
        if (this.audioDone) return this.fail('AFTER_AUDIO_DONE')
        this.itemId = event.item_id
        if (event.type === 'response.audio.done') {
          if (!this.frames) return this.fail('EMPTY')
          this.audioDone = true
        } else {
          const encoded = event.delta
          if (typeof encoded !== 'string' || !encoded.length || encoded.length > 699052
            || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) return this.fail('PCM_INVALID')
          const pcm = Buffer.from(encoded, 'base64')
          if (!pcm.length || pcm.length % 2 || pcm.length > 512 * 1024 || pcm.toString('base64') !== encoded) return this.fail('PCM_INVALID')
          this.frames++; this.bytes += pcm.length
          if (this.frames > 4096 || this.bytes > 8000000) return this.fail('LIMIT')
        }
      }
      const serialized = JSON.stringify(event)
      if (serialized.length > 750000) return this.fail('EVENT_LIMIT')
      const envelope: QwenOutputEnvelope = { type: 'qwen_output_bridge_v1', scope: { ...this.scope }, sequence: ++this.sequence,
        frameCount: this.frames, pcmBytes: this.bytes, sampleCount: this.bytes / 2, event: JSON.parse(serialized) }
      this.delivering = true
      try { this.owner.emit(envelope) } finally { this.delivering = false }
      if (this.ended) return 'retired'
      if (event.type === 'response.done') this.ended = true
      return 'accepted'
    } catch { return this.fail('CONSUMER_FAILED') }
  }

  retire(): void { this.fail('RETIRED') }
  transportEnded(): void { this.fail('EOS_MISSING') }
  private fail(code: string): 'retired' {
    if (!this.ended) {
      this.ended = true
      try { this.owner.onFailure(`QWEN_OUTPUT_${code}`) } catch { /* Source ownership is already retired. */ }
    }
    return 'retired'
  }
}
