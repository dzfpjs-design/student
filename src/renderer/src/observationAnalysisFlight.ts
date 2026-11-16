/** One renderer analysis request; retirement is not proof of server rollback. */
export const OBSERVATION_ANALYSIS_TIMEOUT_MS = 10_000
export const OBSERVATION_ANALYSIS_TIMEOUT = 'OBSERVATION_ANALYSIS_TIMEOUT'

export type ObservationAnalysisFlight<T> = {
  readonly result: Promise<T>
  readonly signal: AbortSignal
  retire: () => void
}

export function observationAnalysisRetryDelay(failures: number): number {
  return Math.min(30_000, 5_000 * 2 ** Math.min(3, Math.max(0, failures - 1)))
}

export function startObservationAnalysis<T>(work: (signal: AbortSignal) => Promise<T>): ObservationAnalysisFlight<T> {
  const controller = new AbortController()
  let finished = false
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const result = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  const fail = (error: Error): void => {
    if (finished) return
    finished = true
    clearTimeout(timer)
    controller.abort(error)
    reject(error)
  }
  // The deadline covers runtime lookup, fetch headers and the complete JSON body.
  const timer = setTimeout(() => fail(new Error(OBSERVATION_ANALYSIS_TIMEOUT)), OBSERVATION_ANALYSIS_TIMEOUT_MS)
  // Both late completion paths remain handled; neither can revive a retired result.
  void Promise.resolve().then(() => {
    if (controller.signal.aborted) throw controller.signal.reason
    return work(controller.signal)
  }).then(value => {
    if (finished) return
    finished = true
    clearTimeout(timer)
    resolve(value)
  }, error => {
    if (finished) return
    finished = true
    clearTimeout(timer)
    reject(error)
  })
  return Object.freeze({ result, signal: controller.signal,
    retire: () => fail(new DOMException('Original observation analysis retired', 'AbortError')) })
}
