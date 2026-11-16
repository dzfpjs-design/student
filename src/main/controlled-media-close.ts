type MediaWindow = {
  isDestroyed(): boolean
  close(): void
  once(event: 'closed', listener: () => void): unknown
  removeListener(event: 'closed', listener: () => void): unknown
}

export function createControlledMediaCloser<T extends MediaWindow>(
  currentWindow: () => T | null,
  markReason: (window: T, reason: string) => void,
  timeoutMs = 2000,
  hasLiveWindows: () => boolean = () => false,
): (reason: string) => Promise<{ closed: boolean }> {
  const pending = new WeakMap<T, Promise<boolean>>()
  return async (reason) => {
    const target = currentWindow()
    if (!target || target.isDestroyed()) return { closed: !hasLiveWindows() }
    let confirmation = pending.get(target)
    if (!confirmation) {
      confirmation = new Promise<boolean>((resolve) => {
        let settled = false
        const finish = (closed: boolean): void => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          target.removeListener('closed', onClosed)
          resolve(closed)
        }
        const onClosed = (): void => finish(true)
        const timer = setTimeout(() => finish(false), timeoutMs)
        target.once('closed', onClosed)
        try {
          markReason(target, reason)
          target.close()
        } catch { finish(false) }
      })
      pending.set(target, confirmation)
      void confirmation.then(() => pending.delete(target))
    }
    const closed = await confirmation
    const current = currentWindow()
    // An old window's receipt says nothing about a replacement now playing.
    return { closed: closed && (!current || current === target || current.isDestroyed()) && !hasLiveWindows() }
  }
}
