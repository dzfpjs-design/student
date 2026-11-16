import { beforeEach, expect, it, vi } from 'vitest'
import { request } from './api'
import { loadMistakePrintCrop, loadMistakePrintSnapshot, type MistakePrintItem } from './mistakeBookPrintApi'

vi.mock('./api', () => ({ request: vi.fn(), ApiError: class extends Error {} }))
const childId = 'public-child'
function entry(): MistakePrintItem {
  return { record: { child_id: childId, mistake_id: 'public-id', source_revision: 'version-1', source: { question: '1+1?', title: '加法', subject: '数学', original_answer: '3' } }, mastery: { status: 'unassessed', label: '待验证', detail: '尚未复习' }, review_count: 0, review_counts: { original: 0, practice: 0, formal: 0 }, last_review_at: null, diagnosis: null } as MistakePrintItem
}
const signal = new AbortController().signal
beforeEach(() => { vi.clearAllMocks() })
it('uses authenticated local reading and validates complete child-scoped results', async () => {
  const result = { version: 1, child_id: childId, total: 1, generated_at: '2026-11-11T10:00:00Z', items: [entry()] }
  vi.mocked(request).mockResolvedValue(result)
  expect(await loadMistakePrintSnapshot(childId, signal)).toEqual(result)
  expect(request).toHaveBeenCalledWith(`/v1/children/${childId}/mistake-learning/print`, { signal, cache: 'no-store' }, false, true)
})
it.each(['wrong-child', 'missing', 'duplicate', 'count-mismatch'])('rejects %s snapshots before rendering', async kind => {
  const result = { version: 1, child_id: childId, total: 1, generated_at: '2026-11-11T10:00:00Z', items: [entry()] }
  if (kind === 'wrong-child') result.items[0].record.child_id = 'other-child'
  if (kind === 'missing') result.total = 2
  if (kind === 'duplicate') { result.items.push(entry());result.total = 2 }
  if (kind === 'count-mismatch') result.items[0].review_count = 5
  vi.mocked(request).mockResolvedValue(result)
  await expect(loadMistakePrintSnapshot(childId, signal)).rejects.toThrow('打印资料')
})
it('binds image requests to the snapshot revision and refuses corrupt or foreign-format crops', async () => {
  vi.mocked(request).mockResolvedValue({ image_base64: '/9j/AAAA', media_type: 'image/jpeg', width: 12, height: 12 })
  expect(await loadMistakePrintCrop(entry(), signal)).toBe('data:image/jpeg;base64,/9j/AAAA')
  expect(vi.mocked(request).mock.calls[0][0]).toContain('source_revision=version-1')
  vi.mocked(request).mockResolvedValue({ image_base64: '<svg onload="bad()">', media_type: 'image/svg+xml', width: 12, height: 12 })
  await expect(loadMistakePrintCrop(entry(), signal)).rejects.toThrow('原题图片')
})
