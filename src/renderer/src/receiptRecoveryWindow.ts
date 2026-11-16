// One voice recovery window spans admission and Runtime lookups. It grants
// read authority only, and remains bounded if a transport ignores abort.
export type ReceiptRecoveryWindow = {
  current: () => boolean
  read: <T>(operation: (signal: AbortSignal) => Promise<T>) => Promise<T>
}

class ReceiptRecoveryTimeout extends Error {
  readonly code = 'VOICE_RECEIPT_CHECK_TIMEOUT'
  constructor() { super('Receipt recovery deadline exceeded'); this.name = 'ReceiptRecoveryTimeout' }
}

export async function withReceiptRecoveryWindow<T>(current: () => boolean, signal: AbortSignal | undefined,
  work: (window: ReceiptRecoveryWindow) => Promise<T>): Promise<T> {
  if (signal?.aborted || !current()) throw new DOMException('Receipt recovery stopped', 'AbortError')
  const controller = new AbortController()
  let active = true
  let reject!: (reason: Error) => void
  const stopped = new Promise<never>((_resolve, fail) => { reject = fail })
  const stop = (reason: Error) => { controller.abort(); reject(reason) }
  const cancel = () => stop(new DOMException('Receipt recovery stopped', 'AbortError'))
  signal?.addEventListener('abort', cancel, { once: true })
  const timer = setTimeout(() => stop(new ReceiptRecoveryTimeout()), 10_000)
  const window: ReceiptRecoveryWindow = {
    current: () => active && !controller.signal.aborted && !signal?.aborted && current(),
    read: operation => Promise.race([Promise.resolve().then(() => {
      if (!window.current()) throw new DOMException('Receipt recovery owner changed', 'AbortError')
      return operation(controller.signal)
    }), stopped]),
  }
  try {
    return await Promise.race([Promise.resolve().then(() => {
      if (!window.current()) throw new DOMException('Receipt recovery owner changed', 'AbortError')
      return work(window)
    }), stopped])
  } finally {
    active = false
    clearTimeout(timer)
    signal?.removeEventListener('abort', cancel)
    controller.abort()
  }
}
