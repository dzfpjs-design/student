// A registration mutation can outlive its consumer. Keep its original response
// and quarantine the same bound camera until an actual late lease is released.
type Lease = { lease_id: string; status: string; expires_at: string }
type Release = (leaseId: string, reason: string) => Promise<{ lease_id: string; status: string }>
export type RetainedObservationRegistration = {
  result: Promise<Lease>
  accept: () => void
  readonly leaseId: string | null
  retire: (reason?: string) => Promise<boolean>
  reconcile: () => Promise<boolean>
}
const retained = new Map<string, RetainedObservationRegistration>()

export async function boundedObservationCleanup<T>(work: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(new Error('OBSERVATION_RELEASE_TIMEOUT')), 5_000)
  const cancelled = new Promise<never>((_resolve, reject) => {
    controller.signal.addEventListener('abort', () => reject(controller.signal.reason), { once: true })
  })
  try { return await Promise.race([Promise.resolve().then(() => work(controller.signal)), cancelled]) }
  finally { window.clearTimeout(timer) }
}

export const releaseObservationLease = (work: (signal: AbortSignal) => Promise<{ lease_id: string; status: string }>): Promise<{ lease_id: string; status: string }> => boundedObservationCleanup(work)

export function reconcileObservationRegistration(key: string): Promise<boolean> | null {
  return retained.get(key)?.reconcile() ?? null
}

export function retainObservationRegistration(key: string, register: () => Promise<Lease>, release: Release): RetainedObservationRegistration {
  if (retained.has(key)) throw new Error('OBSERVATION_REGISTRATION_PENDING')
  let lease: Lease | null = null, retired = false, accepted = false, completed = false, failed = false, releaseReason = 'observation_registration_retired'
  let resolveResult!: (value: Lease) => void, rejectResult!: (error: unknown) => void
  let resolveClosed!: (value: boolean) => void, cleaning: Promise<boolean> | null = null
  const result = new Promise<Lease>((resolve, reject) => { resolveResult = resolve; rejectResult = reject })
  const closed = new Promise<boolean>(resolve => { resolveClosed = resolve })
  const finish = (ok: boolean): boolean => {
    if (ok) { completed = true; if (retained.get(key) === owner) retained.delete(key) }
    resolveClosed(ok)
    return ok
  }
  const clean = (): Promise<boolean> => {
    if (completed) return Promise.resolve(true)
    if (!lease) return closed
    if (cleaning) return cleaning
    const held = lease
    cleaning = Promise.resolve().then(() => release(held.lease_id, releaseReason))
      .then(value => finish(value.lease_id === held.lease_id && value.status === 'released'), () => finish(false))
      .finally(() => { cleaning = null })
    return cleaning
  }
  const owner: RetainedObservationRegistration = {
    result,
    get leaseId() { return lease?.lease_id ?? null },
    accept() {
      if (retired || !lease) throw new Error('OBSERVATION_REGISTRATION_RETIRED')
      accepted = true // Keep the bound-device owner until actual release ACK.
    },
    retire(reason) {
      if (!retired && accepted && reason) releaseReason = reason
      retired = true
      rejectResult(new DOMException('Observation registration consumer retired', 'AbortError'))
      return lease ? clean() : closed
    },
    reconcile() { return retired && lease ? clean() : completed ? Promise.resolve(true) : failed ? Promise.resolve(false) : closed },
  }
  retained.set(key, owner)
  void Promise.resolve().then(register).then(value => {
    lease = value
    if (retired) void clean()
    else resolveResult(value)
  }, error => {
    // A transport failure does not prove the mutation was never committed.
    // Without a lease receipt no new camera/registration is admitted.
    failed = true
    rejectResult(error)
    resolveClosed(false)
  })
  return owner
}
