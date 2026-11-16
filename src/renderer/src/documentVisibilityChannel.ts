import { DocumentVisibilityEstimator, type DocumentVisibilitySample, type DocumentVisibilitySegment } from './documentVisibilityEstimator'

export type DocumentVisibilityPayload = { child_id: string; revision_id: string; lease_id: string; epoch: string;
  generation: number; seq: number; segment_id: string; started_at: string; ended_at: string; visible_ms: number }
type Context = { childId: string; leaseId: string; epoch: string; generation: number }
type Receipt = { segment_id: string; recorded: boolean }

/** No retries after unknown results: the monitor generation must be restarted. */
export class DocumentVisibilityChannel {
  private readonly estimator = new DocumentVisibilityEstimator(1500, 10_000)
  private readonly baseTick: number
  private readonly baseWall: number
  private seq = 0
  private busy = false
  private closed = false
  private failed = false
  private invalidation = 0
  constructor(private readonly context: Context, private readonly send: (body: DocumentVisibilityPayload) => Promise<Receipt>,
              private readonly nowTick = () => performance.now(), private readonly nowWall = () => Date.now(),
              private readonly makeId: () => string = () => crypto.randomUUID()) {
    this.baseTick = nowTick(); this.baseWall = nowWall()
  }
  sample(sample: DocumentVisibilitySample): void {
    if (this.closed || this.failed || this.busy) { this.estimator.stop(); return }
    if (sample.generation !== this.context.generation || Math.abs((this.nowWall() - this.baseWall) - (sample.tickMs - this.baseTick)) > 500) {
      this.estimator.stop(); this.failed = true; return
    }
    this.submit(this.estimator.observe(sample))
  }
  pause(): void {
    const result = this.estimator.stop()
    if (Math.abs((this.nowWall() - this.baseWall) - (this.nowTick() - this.baseTick)) > 500) this.failed = true
    if (!this.closed && !this.busy && !this.failed) this.submit(result)
  }
  discard(): void { this.invalidation++; this.estimator.stop() }
  stop(): void {
    if (this.closed) return
    this.estimator.stop()
    this.closed = true
    this.invalidation++
  }
  private submit(segment: DocumentVisibilitySegment | null): void {
    if (!segment) return
    const started = Math.floor(this.baseWall + segment.startTickMs - this.baseTick)
    const ended = Math.floor(this.baseWall + segment.endTickMs - this.baseTick)
    const visible = Math.floor(segment.estimatedMs)
    if (!Number.isSafeInteger(started) || !Number.isSafeInteger(ended) || visible <= 0 || ended <= started || visible > ended - started) { this.failed = true; return }
    // The service uses China-local calendar days. Drop a midnight-straddling
    // sample rather than assigning either side to the wrong daily report.
    const chinaDay = (time: number): number => Math.floor((time + 8 * 3600_000) / 86_400_000)
    if (chinaDay(started) !== chinaDay(ended)) return
    const payload: DocumentVisibilityPayload = { child_id: this.context.childId, revision_id: segment.pageRevisionId,
      lease_id: this.context.leaseId, epoch: this.context.epoch, generation: this.context.generation, seq: this.seq,
      segment_id: this.makeId(), started_at: new Date(started).toISOString(), ended_at: new Date(ended).toISOString(), visible_ms: visible }
    this.busy = true
    const ticket = this.invalidation
    void Promise.resolve().then(() => {
      if (this.closed || ticket !== this.invalidation) throw new Error('OBSERVATION_CANCELLED')
      return this.send(payload)
    }).then(receipt => {
      if (receipt.recorded !== true || receipt.segment_id !== payload.segment_id) this.failed = true
      else this.seq++
    }).catch(() => { this.failed = true }).finally(() => { this.busy = false })
  }
}
