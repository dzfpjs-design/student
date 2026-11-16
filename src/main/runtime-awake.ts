type AwakeKind = 'learning' | 'qualification'
type AwakeRequest = { kind: AwakeKind; leaseId: string; sessionId: string | null; enabled: boolean }
type PowerBlocker = { start(type: 'prevent-display-sleep'): number; isStarted(id: number): boolean; stop(id: number): void }
type AwakeWindow = { isDestroyed(): boolean; webContents: { id: number; mainFrame: unknown; isDestroyed(): boolean; on(event: string, callback: (...args: any[]) => void): unknown } }
type AwakeEvent = { sender: AwakeWindow['webContents']; senderFrame: unknown; returnValue?: unknown }

// These two local owners share one native blocker. A nonce belongs to one
// mount/run; an older finally must never release a newer run or the other kind.
export function createRuntimeAwake(power: PowerBlocker, currentWindow: () => AwakeWindow | null, onFailure: () => void) {
  const owners = new Map<string, string | null>()
  let blocker: number | null = null
  const navigating = new Set<number>()
  const failed = (): void => { try { onFailure() } catch { /* diagnostics must not break sync IPC or shutdown */ } }
  const reconcile = (): boolean => {
    if (owners.size) {
      if (blocker === null || !power.isStarted(blocker)) blocker = power.start('prevent-display-sleep')
      if (!power.isStarted(blocker)) throw Error('RUNTIME_AWAKE_UNCONFIRMED')
      return true
    }
    if (blocker !== null) {
      if (power.isStarted(blocker)) power.stop(blocker)
      if (power.isStarted(blocker)) throw Error('RUNTIME_AWAKE_UNCONFIRMED')
      blocker = null
    }
    return false
  }
  const revokeWindow = (windowId: number): void => {
    for (const key of owners.keys()) if (key.startsWith(`${windowId}:`)) owners.delete(key)
    try { reconcile() } catch { failed() }
  }
  return {
    handle(event: AwakeEvent, raw: unknown): void {
      // Electron sends immediately from the returnValue setter. Compute the
      // finite reply locally, then send exactly once after synchronous work.
      let reply: unknown = { ok: false, code: 'RUNTIME_AWAKE_UNCONFIRMED' }
      try {
        const window = currentWindow()
        if (!window || window.isDestroyed() || event.sender !== window.webContents || event.sender.isDestroyed() || event.senderFrame !== window.webContents.mainFrame) throw Error('RUNTIME_AWAKE_OWNER_REQUIRED')
        if (!raw || typeof raw !== 'object' || Array.isArray(raw) || Object.keys(raw).sort().join(',') !== 'enabled,kind,leaseId,sessionId') throw Error('RUNTIME_AWAKE_REQUEST_REQUIRED')
        const request = raw as AwakeRequest
        if (!['learning', 'qualification'].includes(request.kind) || typeof request.enabled !== 'boolean' || typeof request.leaseId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(request.leaseId) || (request.kind === 'learning' ? typeof request.sessionId !== 'string' || !request.sessionId || request.sessionId.length > 160 : request.sessionId !== null)) throw Error('RUNTIME_AWAKE_REQUEST_REQUIRED')
        if (request.enabled && navigating.has(event.sender.id)) throw Error('RUNTIME_AWAKE_NAVIGATION_PENDING')
        const key = `${event.sender.id}:${request.kind}:${request.leaseId}`
        if (owners.has(key) && owners.get(key) !== request.sessionId) throw Error('RUNTIME_AWAKE_SCOPE_CHANGED')
        const added = request.enabled && !owners.has(key)
        // Bound accidental/repeated requests without ever blocking exact release.
        if (added && [...owners.keys()].filter(owner => owner.startsWith(`${event.sender.id}:`)).length >= 8) throw Error('RUNTIME_AWAKE_OWNER_LIMIT')
        if (request.enabled) owners.set(key, request.sessionId)
        else owners.delete(key)
        try {
          const active = reconcile()
          reply = { ok: true, value: { active, held: owners.has(key) } }
        } catch (cause) {
          if (added) {
            owners.delete(key)
            try { reconcile() } catch { /* keep the native id for a later release/quit retry */ }
          }
          throw cause
        }
      } catch { failed() } finally { event.returnValue = reply }
    },
    watchWindow(window: AwakeWindow): void {
      const windowId = window.webContents.id
      window.webContents.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => {
        // Keep an existing owner if navigation aborts, but forbid the retiring
        // document from acquiring again. A committed document starts owner-free.
        if (isMainFrame && !isInPlace) navigating.add(windowId)
      })
      window.webContents.on('did-navigate', () => { revokeWindow(windowId); navigating.delete(windowId) })
      const failedNavigation = (_event: unknown, _code: unknown, _description: unknown, _url: unknown, isMainFrame: boolean): void => {
        if (isMainFrame) navigating.delete(windowId)
      }
      window.webContents.on('did-fail-load', failedNavigation)
      window.webContents.on('did-fail-provisional-load', failedNavigation)
      window.webContents.on('render-process-gone', () => { navigating.delete(windowId); revokeWindow(windowId) })
      window.webContents.on('destroyed', () => { navigating.delete(windowId); revokeWindow(windowId) })
    },
    clear(): void {
      owners.clear()
      try { reconcile() } catch { failed() }
    },
  }
}
