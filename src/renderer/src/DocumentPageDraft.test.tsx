import { cleanup, render, screen, within } from '@testing-library/react'
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
  it('keeps unaligned reads visible separately from the aligned draft', () => {
    const { canonical_polygon: _unused, ...observation } = draft.lines[1]
    render(<DocumentPageDraft draft={{ ...draft, unmapped_lines: [{ ...observation,
      text: '画面边缘的一行读值', status: 'current', reason: 'outside_reference_frame' }] } as DocumentPreviewPageDraft} />)
    expect(within(screen.getByLabelText('本次单独保留的文字')).getByText('画面边缘的一行读值')).toBeTruthy()
    expect(within(screen.getByRole('list', { name: '已对齐的草稿文字' })).queryByText('画面边缘的一行读值')).toBeNull()
    expect(screen.getByText(/1 行文字尚未对齐，已单独保存/)).toBeTruthy()
  })
  it('labels a full draft as saved history instead of claiming this frame was added', () => {
    render(<DocumentPageDraft draft={{ ...draft, merged: false, reason: 'line_limit',
      lines: draft.lines.map(line => ({ ...line, status: 'historical' })) }} />)
    expect(screen.getByText('已保存的纸面草稿')).toBeTruthy()
    expect(screen.getByText(/本次新读值未加入这份草稿，仍单独保存/)).toBeTruthy()
    expect(screen.queryByText(/尚未确认与先前画面是同一页/)).toBeNull()
    expect(screen.queryByText('本次纸面文字，待核对')).toBeNull()
  })
})
