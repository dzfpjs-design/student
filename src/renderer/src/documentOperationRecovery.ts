export type DocumentDeliveryReceipt = {
  session_id: string; client_action_id: string; operation: 'ocr' | 'revision'
  status: 'saved' | 'unconfirmed'; local_only: true; replay_allowed: false
  artifact_id?: string; revision_id?: string | null
}

function validate(value: DocumentDeliveryReceipt, sessionId: string): void {
  if (!value || value.session_id !== sessionId || typeof value.client_action_id !== 'string' || !value.client_action_id
    || !['ocr', 'revision'].includes(value.operation) || !['saved', 'unconfirmed'].includes(value.status)
    || value.local_only !== true || value.replay_allowed !== false
    || (value.status === 'saved' && (typeof value.artifact_id !== 'string' || !value.artifact_id
      || (value.operation === 'revision' ? typeof value.revision_id !== 'string' || !value.revision_id : value.revision_id !== null)))) {
    throw new Error('DOCUMENT_RECOVERY_SCOPE_MISMATCH')
  }
}

// Query/acknowledge delivery only; never replay OCR, a page review, or teaching.
export async function recoverDocumentOperations(sessionId: string,
  read: (session: string) => Promise<DocumentDeliveryReceipt[]>,
  acknowledge: (session: string, operation: 'ocr' | 'revision', actionId: string) => Promise<DocumentDeliveryReceipt>,
  current: () => boolean): Promise<{ saved: number; unconfirmed: number }> {
  const pending = await read(sessionId)
  if (!Array.isArray(pending) || pending.length > 128) throw new Error('DOCUMENT_RECOVERY_INVALID')
  pending.forEach(value => validate(value, sessionId))
  if (new Set(pending.map(value => value.client_action_id)).size !== pending.length) throw new Error('DOCUMENT_RECOVERY_DUPLICATE')
  let saved = 0, unconfirmed = 0
  for (const value of pending) {
    if (!current()) throw new DOMException('Recovery cancelled', 'AbortError')
    if (value.status !== 'saved') { unconfirmed++; continue }
    const receipt = await acknowledge(sessionId, value.operation, value.client_action_id)
    if (!current()) throw new DOMException('Recovery cancelled', 'AbortError')
    validate(receipt, sessionId)
    if (receipt.status !== 'saved' || receipt.client_action_id !== value.client_action_id || receipt.operation !== value.operation
      || receipt.artifact_id !== value.artifact_id || receipt.revision_id !== value.revision_id) throw new Error('DOCUMENT_RECOVERY_SOURCE_CHANGED')
    saved++
  }
  return { saved, unconfirmed }
}
