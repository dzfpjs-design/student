import { boundedObservationCleanup } from './observationCameraStartup'

type Receipt = { status: string; event_id?: string }
type Boundary = 'paused' | 'unavailable' | 'ended' | 'disconnected' | 'context_changed'
export type RetainedObservationCoverage = {
  result: Promise<Receipt>
  accept: () => void
  retire: (sequence: number, reason: Boundary) => Promise<boolean>
  reconcile: () => Promise<boolean>
}
const retained = new Map<string, RetainedObservationCoverage>()
const retainedBindings = new Map<string, RetainedObservationCoverage>()

async function eventId(epoch: string, boundary: 'start' | 'stop'): Promise<string> {
  const bytes = new TextEncoder().encode(`coverage-${boundary}:${epoch}`)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return 'coverage-action:' + Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}

export function reconcileObservationCoverage(sessionId: string, bindingKey?: string): Promise<boolean> | null {
  const owner = (bindingKey ? retainedBindings.get(bindingKey) : undefined) ?? retained.get(sessionId)
  return owner?.reconcile() ?? null
}

// An unknown original begin may still add a start event. No replacement epoch
// is admitted until that original response arrives and its exact epoch stops.
export function retainObservationCoverage(sessionId: string, epoch: string, begin: () => Promise<Receipt>, stop: (sequence: number, reason: Boundary, signal: AbortSignal) => Promise<Receipt>, bindingKey: string): RetainedObservationCoverage {
  if (!bindingKey || retained.has(sessionId) || retainedBindings.has(bindingKey)) throw new Error('OBSERVATION_COVERAGE_PENDING')
  let opened = false, completed = false, failed = false
  let stopEventId = ''
  let retirement: { sequence: number; reason: Boundary } | null = null
  let resolveResult!: (value: Receipt) => void, rejectResult!: (error: unknown) => void
  let resolveClosed!: (value: boolean) => void, cleaning: Promise<boolean> | null = null
  const result = new Promise<Receipt>((resolve, reject) => { resolveResult = resolve; rejectResult = reject })
  const closed = new Promise<boolean>(resolve => { resolveClosed = resolve })
  const finish = (ok: boolean): boolean => {
    if (ok) {
      completed = true
      if (retained.get(sessionId) === owner) retained.delete(sessionId)
      if (retainedBindings.get(bindingKey) === owner) retainedBindings.delete(bindingKey)
    }
    resolveClosed(ok)
    return ok
  }
  const clean = (): Promise<boolean> => {
    if (completed) return Promise.resolve(true)
    if (!opened || !retirement) return closed
    if (cleaning) return cleaning
    const original = retirement
    cleaning = boundedObservationCleanup(signal => stop(original.sequence, original.reason, signal))
      .then(value => finish(value.status === 'recorded' && value.event_id === stopEventId), () => finish(false))
      .finally(() => { cleaning = null })
    return cleaning
  }
  const owner: RetainedObservationCoverage = {
    result,
    accept() { if (retirement || !opened) throw new Error('OBSERVATION_COVERAGE_RETIRED') },
    retire(sequence, reason) {
      retirement ??= { sequence, reason }
      rejectResult(new DOMException('Observation coverage consumer retired', 'AbortError'))
      return opened ? clean() : closed
    },
    reconcile() { return retirement && opened ? clean() : completed ? Promise.resolve(true) : failed ? Promise.resolve(false) : closed },
  }
  retained.set(sessionId, owner)
  retainedBindings.set(bindingKey, owner)
  void Promise.resolve().then(begin).then(async receipt => {
    const [startEventId, stoppedEventId] = await Promise.all([eventId(epoch, 'start'), eventId(epoch, 'stop')])
    if (receipt.status !== 'recorded' || receipt.event_id !== startEventId) throw new Error('OBSERVATION_COVERAGE_RECEIPT_REQUIRED')
    stopEventId = stoppedEventId
    opened = true
    if (retirement) void clean()
    else resolveResult(receipt)
  }).catch(error => {
    failed = true
    rejectResult(error)
    resolveClosed(false) // Unknown start outcome remains quarantined.
  })
  return owner
}
