export type LegacyOcrCounts = {
  unattributed_jobs: number; unattributed_cache: number; protected_jobs: number; protected_cache: number
}
export type LegacyOcrPreview = {
  preview_id: string; snapshot_fingerprint: string; expires_at: string; counts: LegacyOcrCounts
  scope: 'legacy_unattributed_ocr_cache'; current_database_included: false; confirmation_text: string
}
export type LegacyOcrCommand = {
  preview_id: string; expected_fingerprint: string; client_action_id: string; confirmation: string
}
export type LegacyOcrReceipt = {
  receipt_id: string; status: string; preview_id: string
  selected_counts: Pick<LegacyOcrCounts, 'unattributed_jobs' | 'unattributed_cache'>
  remaining_counts: LegacyOcrCounts | null; restored_invalidated?: boolean
}
