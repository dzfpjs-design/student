import { ApiError, request } from './api'
import type { MistakeLearningState } from './mistakeLearningTypes'

export type PrintMastery = 'unassessed' | 'not_mastered' | 'retest_pending' | 'mastered'
export type MistakePrintItem = {
  record: MistakeLearningState
  mastery: { status: PrintMastery; label: string; detail: string }
  review_count: number
  last_review_at: string | null
  review_counts: { original: number; practice: number; formal: number }
  diagnosis: { label: string; explanation: string; status: string } | null
}
export type MistakePrintSnapshot = { version: 1; child_id: string; generated_at: string; total: number; items: MistakePrintItem[] }
const masteryStates = new Set(['unassessed', 'not_mastered', 'retest_pending', 'mastered'])

export async function loadMistakePrintSnapshot(childId: string, signal: AbortSignal): Promise<MistakePrintSnapshot> {
  const value = await request<MistakePrintSnapshot>(`/v1/children/${encodeURIComponent(childId)}/mistake-learning/print`, { signal, cache: 'no-store' }, false, true)
  const ids = new Set<string>()
  if (!value || value.version !== 1 || value.child_id !== childId || !Array.isArray(value.items) || value.total !== value.items.length || !Number.isFinite(Date.parse(value.generated_at))) throw new ApiError('打印资料不完整，请重新读取。')
  for (const item of value.items) {
    const record = item?.record
    const counts = item?.review_counts
    if (!record || record.child_id !== childId || typeof record.mistake_id !== 'string' || ids.has(record.mistake_id) || typeof record.source_revision !== 'string' || !record.source || !['question', 'title', 'subject', 'original_answer'].every(key => typeof record.source[key as keyof typeof record.source] === 'string') || !masteryStates.has(item.mastery?.status) || typeof item.mastery.label !== 'string' || typeof item.mastery.detail !== 'string' || !Number.isSafeInteger(item.review_count) || item.review_count < 0 || !counts || ![counts.original, counts.practice, counts.formal].every(count => Number.isSafeInteger(count) && count >= 0) || counts.original + counts.practice + counts.formal !== item.review_count) throw new ApiError('打印资料与当前孩子或题目不一致，请重新读取。')
    ids.add(record.mistake_id)
  }
  return value
}

export async function loadMistakePrintCrop(item: MistakePrintItem, signal: AbortSignal): Promise<string> {
  const record = item.record
  const crop = await request<{ image_base64: string; media_type: string; width: number; height: number }>(`/v1/children/${encodeURIComponent(record.child_id)}/mistakes/${encodeURIComponent(record.mistake_id)}/question-crop?source_revision=${encodeURIComponent(record.source_revision)}`, { signal, cache: 'no-store' }, false, true)
  if (crop.media_type !== 'image/jpeg' || !Number.isSafeInteger(crop.width) || crop.width < 1 || !Number.isSafeInteger(crop.height) || crop.height < 1 || typeof crop.image_base64 !== 'string' || crop.image_base64.length > 2_800_000 || !/^\/9j\/[A-Za-z0-9+/]+={0,2}$/.test(crop.image_base64)) throw new ApiError('原题图片读取不完整，请重试。')
  return `data:image/jpeg;base64,${crop.image_base64}`
}
