const active = new Set<symbol>()
const capturePendingKey = 'hsc-parent-capture-delete-unconfirmed-v1'
export type CaptureDeletionIntent = { childId: string; artifactId: string; actionId: string }
let captureOwner: symbol | null = null
export const parentVisualInvalidationEvent = 'hsc:parent-visual-invalidation'
export const captureDeletionUnconfirmed = (): boolean => { try { return localStorage.getItem(capturePendingKey) !== null } catch { return true } }
export const parentVisualInvalidationActive = (): boolean => active.size > 0 || captureDeletionUnconfirmed()
export function captureDeletionIntent(): CaptureDeletionIntent | null {
  try {
    const raw = localStorage.getItem(capturePendingKey)
    if (!raw) return null
    const value = JSON.parse(raw)
    if (!value || typeof value.childId !== 'string' || !value.childId || typeof value.artifactId !== 'string' || !value.artifactId || typeof value.actionId !== 'string' || !/^[a-f0-9-]{36}$/.test(value.actionId)) return null
    return { childId: value.childId, artifactId: value.artifactId, actionId: value.actionId }
  } catch { return null }
}
export function beginCaptureDeletion(childId: string, artifactId: string): { id: symbol; intent: CaptureDeletionIntent } {
  if (parentVisualInvalidationActive()) throw new Error('PARENT_VISUAL_OPERATION_UNCONFIRMED')
  const intent = { childId, artifactId, actionId: crypto.randomUUID() }
  const raw = JSON.stringify(intent)
  localStorage.setItem(capturePendingKey, raw)
  if (localStorage.getItem(capturePendingKey) !== raw) throw new Error('PARENT_VISUAL_OPERATION_NOT_DURABLE')
  const id = beginParentVisualInvalidation(); captureOwner = id
  return { id, intent }
}
export function confirmCaptureDeletion(intent: CaptureDeletionIntent): void {
  const current = captureDeletionIntent()
  if (!current || current.childId !== intent.childId || current.artifactId !== intent.artifactId || current.actionId !== intent.actionId) throw new Error('CAPTURE_DELETION_IDENTITY_CHANGED')
  localStorage.removeItem(capturePendingKey)
  if (captureOwner) { endParentVisualInvalidation(captureOwner); captureOwner = null }
  else window.dispatchEvent(new Event(parentVisualInvalidationEvent))
}
export function beginParentVisualInvalidation(): symbol {
  const id = Symbol('parent-visual-invalidation')
  active.add(id)
  window.dispatchEvent(new Event(parentVisualInvalidationEvent))
  return id
}
export function endParentVisualInvalidation(id: symbol): void {
  if (active.delete(id)) window.dispatchEvent(new Event(parentVisualInvalidationEvent))
}
