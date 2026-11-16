export function createVerifiedQuit(
  closeExternal: Array<() => Promise<{ closed?: unknown }>>,
  onConfirmed: () => Promise<void>,
  onBlocked: () => Promise<void>,
): () => Promise<void> {
  let pending: Promise<void> | null = null
  return () => {
    if (pending) return pending
    const request = (async () => {
      const receipts = await Promise.all(closeExternal.map(close =>
        Promise.resolve().then(close).then(receipt => receipt?.closed === true, () => false)))
      if (receipts.every(Boolean)) await onConfirmed()
      else await onBlocked()
    })()
    pending = request
    const release = (): void => { if (pending === request) pending = null }
    void request.then(release, release)
    return request
  }
}
