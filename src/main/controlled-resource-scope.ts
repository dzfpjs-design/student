export type ControlledCloseReceipt = {
  closed: boolean
  sessionId?: string
  proof?: 'observed_closed' | 'unknown' | 'identity_mismatch' | 'cancelled_before_dispatch' | 'owner_exit_before_registration'
}

// These receipts only certify exits observed by this main process. A new main
// process must not turn missing ChildProcess/BrowserWindow handles into proof.
export class ControlledResourceScope {
  private readonly closed = new Set<string>()

  wasClosed(sessionId: string): boolean { return this.closed.has(sessionId) }

  recordClosed(sessionId: string): void {
    this.closed.add(sessionId)
    if (this.closed.size > 128) this.closed.delete(this.closed.values().next().value!)
  }

  check(expected: unknown, liveIds: readonly (string | undefined)[]): ControlledCloseReceipt | null {
    if (typeof expected !== 'string' || !expected.trim() || expected.length > 128) return { closed: false, proof: 'unknown' }
    if (liveIds.length) {
      return liveIds.every(id => id === expected) ? null : { closed: false, sessionId: expected, proof: 'identity_mismatch' }
    }
    return this.closed.has(expected)
      ? { closed: true, sessionId: expected, proof: 'observed_closed' }
      : { closed: false, sessionId: expected, proof: 'unknown' }
  }
}
