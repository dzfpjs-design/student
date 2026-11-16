import type { PcmChunk, TtsProvider, TtsRequest } from './companionVoice'

export type QwenSampleGenerationEvidence = {
  responseId: string; itemId: string; sampleCount: number; pcmBytes: number; frameCount: number
  generationCompleted: true; renderedCompleted: false
}
type EventObject = Record<string, unknown>
const object = (value: unknown): EventObject | null => value !== null && typeof value === 'object' && !Array.isArray(value)
  ? value as EventObject : null
const identity = (value: unknown): value is string => typeof value === 'string' && /^[a-zA-Z0-9_-]{8,128}$/.test(value)

/** Qualification lane only, not a production transport switch. Its owner binds
 * an authorized response.created ID and retires this route without closing the
 * shared conversation. Generation evidence is never a delivery receipt. */
export class QwenRealtimeSampleOutput implements TtsProvider {
  readonly id = 'qwen-realtime-known-sample-qualification'
  readonly isProductionVoice = true
  private queue: PcmChunk[] = []
  private queuedBytes = 0
  private bytes = 0
  private frames = 0
  private eventIds = new Set<string>()
  private itemId: string | null = null
  private audioDone = false
  private evidence: QwenSampleGenerationEvidence | null = null
  private failure: Error | null = null
  private retired = false
  private consumed = false
  private sourceClosed = false
  private wake: (() => void) | null = null

  constructor(private readonly owner: {
    responseId: string; voice: string; isCurrent: () => boolean; close: () => void
  }) {
    if (!identity(owner.responseId) || !/^[a-zA-Z0-9._:-]{2,180}$/.test(owner.voice)) throw new Error('QWEN_SAMPLE_OWNER_INVALID')
  }

  get generationEvidence(): QwenSampleGenerationEvidence | null { return this.evidence ? { ...this.evidence } : null }

  receive(value: unknown): 'accepted' | 'ignored' | 'retired' {
    if (this.retired || this.failure || this.evidence) return 'retired'
    if (!this.owner.isCurrent()) { this.retire(); return 'retired' }
    const event = object(value)
    if (!event) { this.fail('EVENT_INVALID'); return 'retired' }
    const type = event.type
    if (type === 'error') { this.fail('SOURCE_FAILED'); return 'retired' }
    if (typeof type === 'string' && type.startsWith('response.function_call')) { this.fail('TOOL_OUTPUT_UNAUTHORIZED'); return 'retired' }
    if (!['response.audio.delta', 'response.audio.done', 'response.done'].includes(String(type))) return 'ignored'
    if (!identity(event.event_id)) { this.fail('EVENT_INVALID'); return 'retired' }
    if (this.eventIds.has(event.event_id)) { this.fail('EVENT_REPLAYED'); return 'retired' }
    this.eventIds.add(event.event_id)
    if (type === 'response.done') {
      const response = object(event.response)
      if (!response || response.id !== this.owner.responseId || response.status !== 'completed'
        || response.voice !== this.owner.voice) { this.fail('RESPONSE_INVALID'); return 'retired' }
      const outputs = response.output
      const entry = Array.isArray(outputs) && outputs.length === 1 ? object(outputs[0]) : null
      const content = entry?.content
      const audio = Array.isArray(content) && content.length === 1 ? object(content[0]) : null
      if (!this.audioDone || !this.frames || entry?.id !== this.itemId || entry?.type !== 'message'
        || entry?.role !== 'assistant' || entry?.status !== 'completed' || audio?.type !== 'audio') {
        this.fail('COMPLETION_INVALID'); return 'retired'
      }
      this.evidence = { responseId: this.owner.responseId, itemId: this.itemId!, sampleCount: this.bytes / 2,
        pcmBytes: this.bytes, frameCount: this.frames, generationCompleted: true, renderedCompleted: false }
      this.wake?.()
      return 'accepted'
    }
    if (event.response_id !== this.owner.responseId || !identity(event.item_id)
      || event.output_index !== 0 || event.content_index !== 0 || (this.itemId && event.item_id !== this.itemId)) {
      this.fail('IDENTITY_INVALID'); return 'retired'
    }
    if (this.audioDone) { this.fail('AFTER_AUDIO_DONE'); return 'retired' }
    this.itemId = event.item_id
    if (type === 'response.audio.done') {
      if (!this.frames) { this.fail('EMPTY'); return 'retired' }
      this.audioDone = true
      return 'accepted'
    }
    const encoded = event.delta
    if (typeof encoded !== 'string' || !encoded.length || encoded.length > 699052
      || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
      this.fail('PCM_INVALID'); return 'retired'
    }
    let binary: string
    try { binary = atob(encoded) } catch { this.fail('PCM_INVALID'); return 'retired' }
    if (!binary.length || binary.length % 2 || binary.length > 512 * 1024 || btoa(binary) !== encoded) {
      this.fail('PCM_INVALID'); return 'retired'
    }
    this.bytes += binary.length; this.frames++
    if (this.bytes > 8000000 || this.frames > 4096) { this.fail('LIMIT'); return 'retired' }
    if (this.queue.length >= 512 || this.queuedBytes + binary.length > 8000000) { this.fail('QUEUE_LIMIT'); return 'retired' }
    const bytes = Uint8Array.from(binary, char => char.charCodeAt(0))
    this.queue.push({ pcm16: bytes.buffer, sampleRate: 24000, channels: 1 })
    this.queuedBytes += binary.length
    this.wake?.()
    return 'accepted'
  }

  transportEnded(): void { if (!this.evidence && !this.retired) this.fail('EOS_MISSING') }
  retire(): void { this.fail('RETIRED') }

  async *stream(request: TtsRequest): AsyncIterable<PcmChunk> {
    if (this.consumed) throw new Error('QWEN_SAMPLE_CONSUMER_REUSED')
    this.consumed = true
    const aborted = (): void => this.fail('CANCELLED', new DOMException('Playback cancelled', 'AbortError'))
    request.signal.addEventListener('abort', aborted, { once: true })
    if (request.signal.aborted) aborted()
    try {
      for (;;) {
        if (!this.owner.isCurrent() && !this.retired) this.retire()
        if (this.failure) throw this.failure
        const chunk = this.queue.shift()
        if (chunk) { this.queuedBytes -= chunk.pcm16.byteLength; yield chunk; continue }
        if (this.evidence) return
        await new Promise<void>(resolve => {
          const timer = window.setTimeout(() => this.fail('TIMEOUT'), 30000)
          this.wake = () => { window.clearTimeout(timer); this.wake = null; resolve() }
        })
      }
    } finally {
      request.signal.removeEventListener('abort', aborted)
      this.retired = true; this.queue = []; this.queuedBytes = 0
      this.closeSource()
      if (this.failure) throw this.failure
    }
  }

  private fail(code: string, error = new Error(`QWEN_SAMPLE_${code}`)): void {
    if (this.failure || this.retired) return
    this.failure = error; this.retired = true; this.queue = []; this.queuedBytes = 0
    this.wake?.()
    this.closeSource()
  }
  private closeSource(): void {
    if (this.sourceClosed) return
    this.sourceClosed = true
    try { this.owner.close() } catch { this.failure ??= new Error('QWEN_SAMPLE_SOURCE_CLOSE_FAILED') }
  }
}
