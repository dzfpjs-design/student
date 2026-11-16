import type { AgentTurn } from './types'

type Pending = {
  epoch: number
  spoken: boolean
  idle: () => boolean
  request: () => Promise<{ status: string; turn?: AgentTurn }>
  execute: (turn: AgentTurn, current: () => boolean) => Promise<void>
  failed: () => void
  prepared?: AgentTurn
  failurePending?: boolean
}

// Tool receipts can drive work, but never enter the child's speech queue.
export class AgentContinuationDriver {
  private epoch = 0
  private pending: Pending | null = null
  private running = false

  interrupt(): void { this.epoch += 1; this.pending = null }

  async readCurrent<T>(request: () => Promise<T>): Promise<T | null> {
    const epoch = this.epoch
    const result = await request()
    return epoch === this.epoch ? result : null
  }

  offer(work: Omit<Pending, 'epoch' | 'spoken'>): () => void {
    const pending = { ...work, epoch: this.epoch, spoken: false }
    this.pending = pending
    return () => {
      if (this.pending !== pending) return
      pending.spoken = true
      this.flush()
    }
  }

  flush(): void {
    const work = this.pending
    if (this.running || !work?.spoken || !work.idle()) return
    this.pending = null
    this.running = true
    const current = () => work.epoch === this.epoch
    const reportFailure = (): void => {
      if (!current()) return
      if (work.idle()) work.failed()
      else this.pending = { ...work, failurePending: true }
    }
    void (async () => {
      try {
        if (work.failurePending) { reportFailure(); return }
        const result = work.prepared ? { status: 'ready', turn: work.prepared } : await work.request()
        if (!current()) return
        if (result.status === 'ready' && result.turn) {
          if (!work.idle()) { this.pending = { ...work, prepared: result.turn }; return }
          // Execution owns its busy flag, so only the input epoch gates it there.
          await work.execute(result.turn, () => work.epoch === this.epoch)
        } else if (['blocked', 'planning_in_progress', 'awaiting_receipt', 'failed'].includes(result.status)) reportFailure()
      } catch { reportFailure() }
      finally { this.running = false; this.flush() }
    })()
  }
}
