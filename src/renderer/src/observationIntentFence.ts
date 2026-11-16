/** Source-only candidate. The cache stores opaque control versions, never media. */
type Requester = <T>(path: string, init?: RequestInit) => Promise<T>
type Session = { session_id: string; status: string; observation_status: 'enabled' | 'paused'; observation_revision: number; observation_generation: string }
type Control = { client_action_id: string; operation: 'enabled' | 'paused'; expected_revision: number | null; expected_generation: string | null; result_revision: number; effect: 'applied' | 'no_op' | 'replayed'; applied: boolean; fresh: boolean }
type Receipt = Session & { observation_control: Control }
type Known = { generation: string; revision: number; ticket: number; epoch: string; activity: string; status: 'enabled' | 'paused' }
type Intent = { sessionId: string; generation: string; revision: number; ticket: number; sequence: number; epoch: string; actionId: string }

export class ObservationIntentError extends Error {
  constructor(readonly code: string, message = '观察状态已变化，请核对当前状态后重新说明恢复观察。') { super(message) }
}

export class ObservationIntentFence {
  private known = new Map<string, Known>()
  private sequence = new Map<string, number>()
  private ticket = 0
  private cacheEpoch = 0
  private restoreDepth = 0
  private scopeEpoch(): string { return `${this.epoch()}:${this.cacheEpoch}` }
  constructor(private readonly request: Requester, private readonly epoch: () => number,
    private readonly actionId: () => string = () => crypto.randomUUID()) {}

  cancel(sessionId: string): void { this.sequence.set(sessionId, (this.sequence.get(sessionId) ?? 0) + 1) }
  cancelAll(): void { for (const sessionId of this.sequence.keys()) this.cancel(sessionId) }
  reset(): void { this.cacheEpoch++; this.cancelAll(); this.known.clear() }

  async duringRestore<T>(restore: () => Promise<T>): Promise<T> {
    this.reset()
    this.restoreDepth++
    try { return await restore() }
    finally { this.restoreDepth--; this.reset() }
  }

  private session(value: unknown, sessionId: string): Session {
    const s = value as Partial<Session> | null
    if (!s || s.session_id !== sessionId || typeof s.status !== 'string'
      || !['enabled', 'paused'].includes(s.observation_status ?? '')
      || !Number.isSafeInteger(s.observation_revision) || s.observation_revision! < 0
      || typeof s.observation_generation !== 'string' || !/^[a-f0-9]{64}$/.test(s.observation_generation)) {
      throw new ObservationIntentError('OBSERVATION_CONTROL_STATE_UNCONFIRMED', '尚未核对观察状态版本，请重新读取当前学习状态后再说明恢复观察。')
    }
    return s as Session
  }

  private observe(value: unknown, sessionId: string, ticket: number, epoch: string, anchor: string | null): Session | null {
    if (epoch !== this.scopeEpoch()) throw new ObservationIntentError('OBSERVATION_CONTROL_SERVICE_CHANGED')
    const s = this.session(value, sessionId), previous = this.known.get(sessionId)
    // These session terminals have no same-generation reactivation writer.
    // A restored session receives a new server generation instead.
    if (previous?.generation === s.observation_generation
      && (previous.activity === 'completed' || previous.activity === 'cancelled') && s.status === 'active') return null
    // Incarnations are opaque, without an order. A response begun before an
    // observed generation switch cannot replace it, even with a newer ticket.
    if (previous && previous.generation !== anchor && previous.generation !== s.observation_generation) return null
    if (previous && (previous.generation !== s.observation_generation
      ? ticket < previous.ticket : s.observation_revision < previous.revision
        || (s.observation_revision === previous.revision && ticket < previous.ticket))) return null
    this.known.set(sessionId, { generation: s.observation_generation, revision: s.observation_revision,
      ticket: Math.max(ticket, previous?.ticket ?? 0), epoch, activity: s.status, status: s.observation_status })
    return s
  }

  async workspace<T extends { session: unknown }>(sessionId: string, read: () => Promise<T>): Promise<T> {
    if (this.restoreDepth) throw new ObservationIntentError('OBSERVATION_CONTROL_RESTORE_IN_PROGRESS', '正在恢复本地数据，请等恢复回执核对后重新读取当前学习状态。')
    const ticket = ++this.ticket, epoch = this.scopeEpoch(), anchor = this.known.get(sessionId)?.generation ?? null
    const value = await read()
    if (!this.observe(value.session, sessionId, ticket, epoch, anchor))
      throw new ObservationIntentError('OBSERVATION_CONTROL_WORKSPACE_SUPERSEDED')
    return value
  }

  async pause<T extends Session>(sessionId: string): Promise<T> {
    this.cancel(sessionId) // Synchronous cancellation precedes any URL resolution or fetch.
    const ticket = ++this.ticket, epoch = this.scopeEpoch(), anchor = this.known.get(sessionId)?.generation ?? null
    const value = await this.request<T>(`/v1/sessions/${sessionId}/observation/pause`, {
      method: 'POST', body: JSON.stringify({ client_action_id: this.actionId() }) })
    this.observe(value, sessionId, ticket, epoch, anchor)
    return value
  }

  async resume<T extends Session>(sessionId: string): Promise<T> {
    if (this.restoreDepth) throw new ObservationIntentError('OBSERVATION_CONTROL_RESTORE_IN_PROGRESS', '正在恢复本地数据，请等恢复回执核对后重新读取当前学习状态。')
    const known = this.known.get(sessionId)
    if (!known || known.epoch !== this.scopeEpoch() || known.activity !== 'active') throw new ObservationIntentError('OBSERVATION_CONTROL_STATE_UNCONFIRMED', '尚未核对观察状态版本，请重新读取当前学习状态后再说明恢复观察。')
    // Capture the original intent synchronously. No GET, await, refresh or retry
    // may substitute a later version for this action.
    this.cancel(sessionId)
    const intent: Intent = { sessionId, generation: known.generation, revision: known.revision,
      ticket: ++this.ticket, sequence: this.sequence.get(sessionId)!, epoch: this.scopeEpoch(), actionId: this.actionId() }
    const value = await this.request<T & Receipt>(`/v1/sessions/${sessionId}/observation/resume`, {
      method: 'POST', body: JSON.stringify({ client_action_id: intent.actionId,
        expected_revision: intent.revision, expected_generation: intent.generation }) })
    if (intent.epoch !== this.scopeEpoch() || intent.sequence !== this.sequence.get(sessionId))
      throw new ObservationIntentError('OBSERVATION_CONTROL_INTENT_CANCELLED')
    const session = this.session(value, sessionId), control = value.observation_control
    if (!control || control.client_action_id !== intent.actionId || control.operation !== 'enabled'
      || control.expected_revision !== intent.revision || control.expected_generation !== intent.generation
      || control.result_revision !== intent.revision + 2 || session.observation_revision !== control.result_revision
      || session.observation_generation !== intent.generation || session.observation_status !== 'enabled'
      || session.status !== 'active' || control.effect !== 'applied' || control.applied !== true || control.fresh !== true)
      throw new ObservationIntentError('OBSERVATION_CONTROL_ACK_NOT_FRESH')
    this.observe(value, sessionId, intent.ticket, intent.epoch, intent.generation)
    const current = this.known.get(sessionId)!
    if (current.generation !== intent.generation || current.revision !== session.observation_revision || current.status !== 'enabled')
      throw new ObservationIntentError('OBSERVATION_CONTROL_ACK_SUPERSEDED')
    return value
  }
}
