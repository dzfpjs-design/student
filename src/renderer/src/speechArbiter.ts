export type SpeechPriority = 'reminder' | 'dialogue' | 'lesson' | 'urgent'
export type SpeechDisposition = 'finished' | 'unavailable' | 'cancelled' | 'expired' | 'superseded' | 'full'
export type SpeechAdmission = {
  scope: string
  priority?: SpeechPriority
  expiresAt?: number
  key?: string
  playbackTimeoutMs?: number
  isCurrent?: () => boolean
  beforePlay?: (signal: AbortSignal) => Promise<boolean>
  play: (finish: () => void, unavailable: () => void) => void
  settled?: (result: SpeechDisposition) => void
}
type Entry = SpeechAdmission & { sequence: number; expiresAt: number }
const rank: Record<SpeechPriority, number> = { reminder: 0, dialogue: 1, lesson: 2, urgent: 3 }

/** Only actual playback completion advances the queue. Admission is not delivery. */
export class SpeechArbiter {
  private sequence = 0
  private scope: string | null = null
  private active: Entry | null = null
  private queue: Entry[] = []
  private pumping = false
  private timer: ReturnType<typeof setTimeout> | null = null
  private preparation: AbortController | null = null
  constructor(private readonly stop: () => void, private readonly now = Date.now, private readonly capacity = 8) {}

  submit(input: SpeechAdmission): void {
    const entry: Entry = { ...input, sequence: ++this.sequence,
      expiresAt: input.expiresAt ?? this.now() + (input.priority === 'reminder' ? 15_000 : 60_000) }
    if (!this.valid(entry)) { this.settle(entry, 'expired'); return }
    if (this.scope !== null && this.scope !== input.scope) this.cancel()
    this.scope = input.scope
    if (entry.key) {
      const obsolete = this.queue.filter(item => item.key === entry.key)
      this.queue = this.queue.filter(item => item.key !== entry.key)
      obsolete.forEach(item => this.settle(item, 'superseded'))
    }
    if (entry.priority === 'urgent' && this.queue.length >= this.capacity) {
      const expendable = [...this.queue].sort((a, b) =>
        rank[a.priority ?? 'dialogue'] - rank[b.priority ?? 'dialogue'] || b.sequence - a.sequence)[0]
      this.queue = this.queue.filter(item => item !== expendable)
      if (expendable) this.settle(expendable, 'superseded')
    }
    if (this.active && this.preparation && rank[entry.priority ?? 'dialogue'] > rank[this.active.priority ?? 'dialogue']) {
      // A source read has not started audio and must not hold up higher-priority speech.
      this.finish(this.active, 'superseded', false, false)
    } else if (entry.priority === 'urgent' && this.active) this.finish(this.active, 'superseded', true, false)
    if (this.queue.length >= this.capacity) { this.settle(entry, 'full'); return }
    this.queue.push(entry)
    this.pump()
  }

  cancel(key?: string): void {
    if (key !== undefined) {
      const active = this.active, stop = !this.preparation
      const pending = this.queue.filter(entry => entry.key === key)
      this.queue = this.queue.filter(entry => entry.key !== key)
      pending.forEach(entry => this.settle(entry, 'cancelled'))
      if (active?.key === key) this.finish(active, 'cancelled', stop)
      return
    }
    const pending = this.queue
    this.queue = []
    const active = this.active
    this.active = null
    this.scope = null
    this.preparation?.abort()
    this.preparation = null
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.stopPlayback()
    if (active) this.settle(active, 'cancelled')
    pending.forEach(item => this.settle(item, 'cancelled'))
  }

  private stopPlayback(): void {
    try { this.stop() } catch { /* A failed adapter cannot retain the queue lease. */ }
  }

  private settle(entry: Entry, result: SpeechDisposition): void {
    try { entry.settled?.(result) } catch { /* Consumer callbacks do not own arbitration. */ }
  }

  private valid(entry: Entry): boolean {
    try { return entry.expiresAt > this.now() && (entry.isCurrent?.() ?? true) }
    catch { return false }
  }

  private finish(entry: Entry, result: SpeechDisposition, stop = false, pump = true): void {
    if (this.active !== entry) return
    this.active = null
    this.preparation?.abort()
    this.preparation = null
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    if (stop) this.stopPlayback()
    this.settle(entry, result)
    if (pump) this.pump()
  }

  private pump(): void {
    if (this.active || this.pumping) return
    this.pumping = true
    try {
      this.queue.sort((a, b) => rank[b.priority ?? 'dialogue'] - rank[a.priority ?? 'dialogue'] || a.sequence - b.sequence)
      while (!this.active && this.queue.length) {
        const entry = this.queue.shift()!
        if (!this.valid(entry)) { this.settle(entry, 'expired'); continue }
        this.active = entry
        if (entry.beforePlay) {
          const controller = new AbortController()
          this.preparation = controller
          // Admission belongs to this exact queue lease, not a pre-enqueue read.
          this.timer = setTimeout(() => this.finish(entry, 'unavailable'), Math.max(1, Math.min(10_000, entry.expiresAt - this.now())))
          void Promise.resolve().then(() => {
            if (controller.signal.aborted || this.active !== entry || !this.valid(entry)) return false
            return entry.beforePlay!(controller.signal)
          }).then(allowed => {
            if (this.active !== entry || controller.signal.aborted) return
            if (allowed !== true || !this.valid(entry)) { this.finish(entry, 'superseded'); return }
            this.preparation = null
            if (this.timer) clearTimeout(this.timer)
            this.play(entry)
          }).catch(() => this.finish(entry, 'unavailable'))
        } else this.play(entry)
      }
    } finally { this.pumping = false }
  }

  private play(entry: Entry): void {
    const timeout = Number.isFinite(entry.playbackTimeoutMs)
      ? Math.max(1000, Math.min(615_000, entry.playbackTimeoutMs!)) : 180_000
    this.timer = setTimeout(() => this.finish(entry, 'unavailable', true), timeout)
    try { entry.play(() => this.finish(entry, 'finished'), () => this.finish(entry, 'unavailable', true)) }
    catch { this.finish(entry, 'unavailable', true) }
  }
}
