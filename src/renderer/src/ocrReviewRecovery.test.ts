import { afterEach, describe, expect, it } from 'vitest'
import { clearOcrReviewRecovery, clearOcrArtifactRecovery, loadOcrReviewRecovery, saveOcrReviewRecovery } from './ocrReviewRecovery'

describe('OCR review recovery record', () => {
  afterEach(() => localStorage.clear())
  it('retains exact command identity and drafts without writing on read', () => {
    const value = { childId: 'a', drafts: { first: { proposed: 'x=2', text: 'x=3' } }, pending: {
      kind: 'confirm' as const, reviewId: 'first', text: 'x=3', actionId: 'original-action',
    } }
    saveOcrReviewRecovery(value)
    const before = localStorage.getItem('hsc:ocr-review-recovery:a')
    expect(loadOcrReviewRecovery('a')).toEqual(value)
    expect(localStorage.getItem('hsc:ocr-review-recovery:a')).toBe(before)
    expect(loadOcrReviewRecovery('b')).toEqual({ childId: 'b', drafts: {}, pending: null })
  })
  it.each([
    { childId: 'b', drafts: {}, pending: null },
    { childId: 'a', drafts: [], pending: null },
    { childId: 'a', drafts: {}, pending: { kind: 'confirm', actionId: 'original', reviewId: 'first', text: '' } },
    { childId: 'a', drafts: {}, pending: { kind: 'batch', actionId: 'original', reviewIds: ['first', 'first'], decision: 'skip' } },
  ])('rejects malformed or wrong-child recovery without erasing it', value => {
    const raw = JSON.stringify(value)
    localStorage.setItem('hsc:ocr-review-recovery:a', raw)
    expect(() => loadOcrReviewRecovery('a')).toThrow()
    expect(localStorage.getItem('hsc:ocr-review-recovery:a')).toBe(raw)
  })
  it('deletes only one child or this feature namespace during privacy operations', () => {
    for (const childId of ['a', 'b']) saveOcrReviewRecovery({ childId, drafts: {}, pending: null })
    localStorage.setItem('unrelated-setting', 'keep')
    clearOcrReviewRecovery('a')
    expect(localStorage.getItem('hsc:ocr-review-recovery:a')).toBeNull()
    expect(localStorage.getItem('hsc:ocr-review-recovery:b')).not.toBeNull()
    clearOcrReviewRecovery()
    expect(Object.keys(localStorage)).toEqual(['unrelated-setting'])
  })
  it('erases a deleted artifact draft and affected pending batch without deleting unrelated human work', () => {
    saveOcrReviewRecovery({ childId: 'a', artifactIds: { first: 'artifact-one', second: 'artifact-two' },
      drafts: { first: { proposed: 'x', text: 'private deleted correction' }, second: { proposed: 'y', text: 'keep this correction' } },
      pending: { kind: 'batch', reviewIds: ['first', 'second'], decision: 'skip', actionId: 'batch-pending' },
    })
    clearOcrArtifactRecovery('a', 'artifact-one')
    const value = loadOcrReviewRecovery('a')
    expect(value.pending).toBeNull()
    expect(value.drafts).toEqual({ second: { proposed: 'y', text: 'keep this correction' } })
    expect(value.artifactIds).toEqual({ second: 'artifact-two' })
    expect(localStorage.getItem('hsc:ocr-review-recovery:a')).not.toContain('private deleted correction')
  })
})
