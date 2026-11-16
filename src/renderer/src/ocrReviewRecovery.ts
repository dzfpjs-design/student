export type OcrRetryCommand =
  | { kind: 'confirm'; reviewId: string; text: string; actionId: string }
  | { kind: 'skip' | 'undo'; reviewId: string; actionId: string }
  | { kind: 'batch'; reviewIds: string[]; decision: 'confirm_proposed' | 'skip'; actionId: string }

export type OcrReviewRecovery = {
  childId: string
  drafts: Record<string, { proposed: string; text: string }>
  pending: OcrRetryCommand | null
  artifactIds?: Record<string, string>
}
const prefix = 'hsc:ocr-review-recovery:'
export const ocrRecoveryClearedEvent = 'hsc:ocr-review-recovery-cleared'
export const ocrRecoveryChangedEvent = 'hsc:ocr-review-recovery-changed'
const validText = (value: unknown, max = 4000): value is string => typeof value === 'string' && value.length <= max

export function loadOcrReviewRecovery(childId: string): OcrReviewRecovery {
  const raw = localStorage.getItem(prefix + childId)
  if (!raw) return { childId, drafts: {}, pending: null }
  const value = JSON.parse(raw) as OcrReviewRecovery
  if (value?.childId !== childId || !value.drafts || typeof value.drafts !== 'object' || Array.isArray(value.drafts)) throw new Error('Invalid OCR recovery owner')
  for (const [id, draft] of Object.entries(value.drafts)) {
    if (!id || !draft || !validText(draft.proposed) || !validText(draft.text)) throw new Error('Invalid OCR draft')
  }
  if (value.artifactIds && (typeof value.artifactIds !== 'object' || Array.isArray(value.artifactIds)
    || Object.values(value.artifactIds).some(id => !validText(id, 200) || !id))) throw new Error('Invalid OCR artifact mapping')
  const command = value.pending
  if (command !== null) {
    if (!command || !validText(command.actionId, 200) || !command.actionId) throw new Error('Invalid OCR pending action')
    if (command.kind === 'batch') {
      if (!['confirm_proposed', 'skip'].includes(command.decision) || !Array.isArray(command.reviewIds)
        || !command.reviewIds.length || command.reviewIds.some(id => !validText(id, 200) || !id)
        || new Set(command.reviewIds).size !== command.reviewIds.length) throw new Error('Invalid OCR batch')
    } else if (!['confirm', 'skip', 'undo'].includes(command.kind) || !validText(command.reviewId, 200) || !command.reviewId
      || (command.kind === 'confirm' && (!validText(command.text) || !command.text.trim()))) throw new Error('Invalid OCR command')
  }
  return value
}

export function saveOcrReviewRecovery(value: OcrReviewRecovery): void {
  localStorage.setItem(prefix + value.childId, JSON.stringify(value))
  window.dispatchEvent(new CustomEvent(ocrRecoveryChangedEvent, { detail: value.childId }))
}

export function clearOcrReviewRecovery(childId?: string): void {
  for (const key of Object.keys(localStorage)) {
    if (childId === undefined ? key.startsWith(prefix) : key === prefix + childId) localStorage.removeItem(key)
  }
  window.dispatchEvent(new CustomEvent(ocrRecoveryClearedEvent, { detail: childId }))
}

export function clearOcrArtifactRecovery(childId: string, artifactId: string): void {
  const record = loadOcrReviewRecovery(childId)
  const removed = new Set(Object.entries(record.artifactIds ?? {}).filter(([, id]) => id === artifactId).map(([id]) => id))
  if (!removed.size) return
  const command = record.pending
  const affected = command && (command.kind === 'batch' ? command.reviewIds.some(id => removed.has(id)) : removed.has(command.reviewId))
  saveOcrReviewRecovery({ ...record,
    drafts: Object.fromEntries(Object.entries(record.drafts).filter(([id]) => !removed.has(id))),
    artifactIds: Object.fromEntries(Object.entries(record.artifactIds ?? {}).filter(([id]) => !removed.has(id))),
    pending: affected ? null : command,
  })
  window.dispatchEvent(new CustomEvent(ocrRecoveryClearedEvent, { detail: childId }))
}
