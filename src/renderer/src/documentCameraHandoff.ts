type DocumentMonitor = { yieldCamera: () => Promise<boolean>; resumeCamera: () => void }

const monitors = new Set<DocumentMonitor>()
let manualOwner: symbol | null = null

export function documentCameraReservedForCapture(): boolean {
  return manualOwner !== null
}

export function registerDocumentMonitorHandoff(monitor: DocumentMonitor): () => void {
  monitors.add(monitor)
  return () => { monitors.delete(monitor) }
}

/** A manual capture owns the high camera until it finishes releasing its video track and lease. */
export function reserveDocumentCameraForCapture(): { ready: Promise<boolean>; release: () => void } | null {
  if (manualOwner) return null
  const owner = Symbol()
  manualOwner = owner
  const ready = Promise.all([...monitors].map(monitor => monitor.yieldCamera())).then(results =>
    manualOwner === owner && results.every(Boolean), () => false)
  return { ready, release: () => {
    if (manualOwner !== owner) return
    manualOwner = null
    queueMicrotask(() => {
      if (!manualOwner) for (const monitor of monitors) monitor.resumeCamera()
    })
  } }
}
