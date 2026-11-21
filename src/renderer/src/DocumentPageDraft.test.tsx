import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { DocumentPageDraft } from './DocumentPageDraft'
import type { DocumentPreviewPageDraft } from './types'

const draft: DocumentPreviewPageDraft = {
  page_token: 'page', anchor_preview_id: 'first', frame_count: 2, merged: true, reason: 'matched',
  lines: ['先前看清的第一题', '这次看清的第二题'].map((text, i) => ({ text, status: i ? 'current' : 'historical',
    conflict: i === 1, frame_sha256: 'a'.repeat(64), preview_id: `p${i}`, confidence: .97,
    original_polygon: [[0, 0], [1, 0], [1, .1], [0, .1]], canonical_polygon: [[0, 0], [1, 0], [1, .1], [0, .1]] }))
}
describe('accumulated paper text', () => {
  afterEach(cleanup)
  it('shows both actual reads and distinguishes history and conflicts', () => {
    render(<DocumentPageDraft draft={draft} />)
    expect(screen.getByText('先前看清的第一题')).toBeTruthy()
    expect(screen.getByText('这次看清的第二题')).toBeTruthy()
    expect(screen.getByText('先前画面 ·')).toBeTruthy()
    expect(screen.getByText('不同读值 ·')).toBeTruthy()
    expect(screen.getByText(/2 次采样 · 2 行文字/)).toBeTruthy()
    expect(screen.getByText(/未读到的区域仍未知/)).toBeTruthy()
  })
  it('never describes unmatched frames as a merged full page', () => {
    render(<DocumentPageDraft draft={{ ...draft, merged: false, frame_count: 1 }} />)
    expect(screen.getByText(/尚未确认与先前画面是同一页/)).toBeTruthy()
    expect(screen.queryByText('累计纸面文字，待核对')).toBeNull()
  })
})
