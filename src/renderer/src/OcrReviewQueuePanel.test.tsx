import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { OcrReviewQueuePanel } from './OcrReviewQueuePanel'
import { clearOcrReviewRecovery } from './ocrReviewRecovery'

const item = { review_id: 'review-1', artifact_id: 'artifact-1', child_id: 'child-1', review_kind: 'formula' as const, region_index: 0, region: [[1, 2], [20, 2]], proposed_text: 'x=2', confidence: .42, status: 'pending' as const, corrected_text: null, created_at: '', updated_at: '', raw_media_retained: false as const }
describe('OcrReviewQueuePanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear() })
  it('opens the exact non-first work item without writing and rejects a vanished target', async () => {
    const second = { ...item, review_id: 'review-2', proposed_text: 'second original' }
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([item, second])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const confirm = vi.spyOn(localApi, 'confirmOcrReview')
    const done = vi.fn()
    const view = render(<OcrReviewQueuePanel childId="child-1" focusRequest={{ objectId: 'review-2', nonce: 'focus-2' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('focus-2'))
    expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('second original')
    expect(document.activeElement).toHaveAttribute('data-work-object', 'review-2')
    expect(confirm).not.toHaveBeenCalled()
    view.rerender(<OcrReviewQueuePanel childId="child-1" focusRequest={{ objectId: 'deleted-item', nonce: 'focus-missing' }} onFocusResolved={done} />)
    await waitFor(() => expect(done).toHaveBeenCalledWith('focus-missing', expect.any(String)))
    expect(done).not.toHaveBeenCalledWith('focus-missing')
    expect(confirm).not.toHaveBeenCalled()
  })
  it('shows the no-image boundary, persists a correction, and supports undo', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([item])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const confirm = vi.spyOn(localApi, 'confirmOcrReview').mockResolvedValue({ ...item, status: 'confirmed', corrected_text: 'x=3' })
    vi.spyOn(localApi, 'undoOcrReview').mockResolvedValue(item)
    render(<OcrReviewQueuePanel childId="child-1" />)
    expect(await screen.findByText(/原始页面未保留，刷新或重启后不能查看图片/)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('x=2'))
    fireEvent.change(screen.getByLabelText('OCR 校对文字'), { target: { value: 'x=3' } })
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('x=3'))
    fireEvent.click(screen.getByRole('button', { name: '确认修正' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledWith('review-1', 'child-1', 'x=3', expect.any(String)))
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    await screen.findByText('数学表达式')
  })

  it('offers restart-persisted history for undo and never confirms blank text', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([{ ...item, status: 'skipped' }])
    const undo = vi.spyOn(localApi, 'undoOcrReview').mockResolvedValue(item)
    const confirm = vi.spyOn(localApi, 'confirmOcrReview')
    render(<OcrReviewQueuePanel childId="child-1" />)
    expect(await screen.findByText('最近处理（刷新或重启后仍可撤销）')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    await waitFor(() => expect(undo).toHaveBeenCalledWith('review-1', 'child-1', expect.any(String)))
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('x=2'))
    fireEvent.change(screen.getByLabelText('OCR 校对文字'), { target: { value: ' ' } })
    await waitFor(() => expect(screen.getByRole('button', { name: '确认修正' })).toBeDisabled())
    expect(confirm).not.toHaveBeenCalled()
  })

  it('retries the same non-first history record and keeps pending items after history loading fails', async () => {
    const second = { ...item, review_id: 'review-2', review_kind: 'handwriting' as const }
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([item])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValueOnce([{ ...item, status: 'confirmed' }, { ...second, status: 'skipped' }]).mockRejectedValueOnce(new Error('history unavailable'))
    const undo = vi.spyOn(localApi, 'undoOcrReview').mockRejectedValueOnce(new Error('temporary')).mockResolvedValue(second)
    render(<OcrReviewQueuePanel childId="child-1" />)
    expect(await screen.findByText('最近处理（刷新或重启后仍可撤销）')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: '撤销' })[1])
    expect(await screen.findByRole('button', { name: '重试刚才操作' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试刚才操作' }))
    await waitFor(() => expect(undo).toHaveBeenNthCalledWith(2, 'review-2', 'child-1', expect.any(String)))
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    expect(await screen.findByText('数学表达式')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: '重试加载' })).toBeInTheDocument()
  })

  it('reuses the action ID after a confirmation response is lost', async () => {
    const textItem = { ...item, review_kind: 'text' as const }
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([textItem])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const confirm = vi.spyOn(localApi, 'confirmOcrReview').mockRejectedValueOnce(new Error('response lost')).mockResolvedValue({ ...textItem, status: 'confirmed', corrected_text: '已修正' })
    render(<OcrReviewQueuePanel childId="child-1" />)
    expect(await screen.findByText('低置信文字')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('OCR 校对文字'), { target: { value: '已修正' } })
    fireEvent.click(screen.getByRole('button', { name: '确认修正' }))
    expect(await screen.findByRole('button', { name: '重试刚才操作' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试刚才操作' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
    const firstAction = confirm.mock.calls[0][3]
    expect(typeof firstAction).toBe('string')
    expect(confirm.mock.calls[1]).toEqual(['review-1', 'child-1', '已修正', firstAction])
  })

  it('processes selected rows as one durable batch and advances to the next item', async () => {
    const second = { ...item, review_id: 'review-2', review_kind: 'text' as const, proposed_text: '第二行' }
    const third = { ...item, review_id: 'review-3', review_kind: 'handwriting' as const, proposed_text: '第三行' }
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([item, second, third])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const batch = vi.spyOn(localApi, 'batchOcrReview').mockResolvedValue({
      items: [
        { ...item, status: 'confirmed', corrected_text: item.proposed_text },
        { ...second, status: 'confirmed', corrected_text: second.proposed_text },
      ],
      processed_count: 2,
      remaining_count: 1,
      decision: 'confirm_proposed',
    })
    render(<OcrReviewQueuePanel childId="child-1" />)

    await screen.findByText('数学表达式')
    fireEvent.click(screen.getByLabelText('选择第1条 OCR 校对项'))
    fireEvent.click(screen.getByLabelText('选择第2条 OCR 校对项'))
    fireEvent.click(screen.getByRole('button', { name: '确认选中候选' }))

    await waitFor(() => expect(batch).toHaveBeenCalledWith(
      'child-1', ['review-1', 'review-2'], 'confirm_proposed', expect.any(String)
    ))
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('第三行'))
    expect(screen.queryByText('低置信文字')).not.toBeInTheDocument()
  })

  it('requires individual full-question confirmation and keeps the complete edit', async () => {
    const full = { ...item, review_kind: 'formula_question' as const, proposed_text: '计算 (37+13)×4，只回答整数。' }
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([full])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const batch = vi.spyOn(localApi, 'batchOcrReview')
    const confirm = vi.spyOn(localApi, 'confirmOcrReview').mockResolvedValue({ ...full, status: 'confirmed', corrected_text: '计算 (38+13)×4，只回答整数。' })
    render(<OcrReviewQueuePanel childId="child-1" />)
    await screen.findByText('公式题干')
    fireEvent.click(screen.getByLabelText('选择第1条 OCR 校对项'))
    expect(screen.getByRole('button', { name: '确认选中候选' })).toBeDisabled()
    expect(batch).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('OCR 校对文字'), { target: { value: '计算 (38+13)×4，只回答整数。' } })
    fireEvent.click(screen.getByRole('button', { name: '确认修正' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledWith('review-1', 'child-1', '计算 (38+13)×4，只回答整数。', expect.any(String)))
  })

  it('restores an unsent draft after remount without submitting it', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([item])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const confirm = vi.spyOn(localApi, 'confirmOcrReview')
    const first = render(<OcrReviewQueuePanel childId="child-1" />)
    fireEvent.change(await screen.findByLabelText('OCR 校对文字'), { target: { value: 'x=42' } })
    first.unmount()
    render(<OcrReviewQueuePanel childId="child-1" />)
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('x=42'))
    expect(confirm).not.toHaveBeenCalled()
  })

  it('recovers a lost-response command across remount and refresh using exactly the original action', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([item])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const confirm = vi.spyOn(localApi, 'confirmOcrReview').mockRejectedValueOnce(new Error('lost response'))
      .mockResolvedValueOnce({ ...item, status: 'confirmed', corrected_text: 'x=42' })
    const first = render(<OcrReviewQueuePanel childId="child-1" />)
    fireEvent.change(await screen.findByLabelText('OCR 校对文字'), { target: { value: 'x=42' } })
    fireEvent.click(screen.getByRole('button', { name: '确认修正' }))
    await screen.findByRole('button', { name: '重试刚才操作' })
    const original = confirm.mock.calls[0]
    first.unmount()
    const second = render(<OcrReviewQueuePanel childId="child-1" />)
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '重试刚才操作' })).toBeEnabled())
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: '确认修正' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '重试刚才操作' }))
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
    expect(confirm.mock.calls[1]).toEqual(original)
    await waitFor(() => expect(screen.queryByRole('button', { name: '重试刚才操作' })).not.toBeInTheDocument())
    second.unmount()
    render(<OcrReviewQueuePanel childId="child-1" />)
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: '重试刚才操作' })).not.toBeInTheDocument()
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('does not reuse another child draft and clears removed data in the live panel', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockImplementation(async childId => [{ ...item, child_id: childId }])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const view = render(<OcrReviewQueuePanel childId="child-1" />)
    fireEvent.change(await screen.findByLabelText('OCR 校对文字'), { target: { value: 'first child private draft' } })
    view.rerender(<OcrReviewQueuePanel childId="child-2" />)
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('x=2'))
    view.rerender(<OcrReviewQueuePanel childId="child-1" />)
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('first child private draft'))
    act(() => clearOcrReviewRecovery('child-1'))
    await waitFor(() => expect(screen.getByLabelText('OCR 校对文字')).toHaveValue('x=2'))
    expect(Object.keys(localStorage).some(key => key.includes('ocr-review-recovery:child-1'))).toBe(false)
  })

  it('does not declare an empty queue after a load failure or expose another child result', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([{ ...item, child_id: 'other-child' }])
    vi.spyOn(localApi, 'ocrReviewHistory').mockRejectedValue(new Error('offline'))
    render(<OcrReviewQueuePanel childId="child-1" />)
    expect(await screen.findByText('待处理的 OCR 校对项暂时无法确认。')).toBeInTheDocument()
    expect(screen.queryByLabelText('OCR 校对文字')).not.toBeInTheDocument()
    expect(screen.queryByText('没有待处理的 OCR 校对项。')).not.toBeInTheDocument()
  })

  it('refuses to submit if the original operation cannot be journaled', async () => {
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValue([item])
    vi.spyOn(localApi, 'ocrReviewHistory').mockResolvedValue([])
    const confirm = vi.spyOn(localApi, 'confirmOcrReview')
    render(<OcrReviewQueuePanel childId="child-1" />)
    await screen.findByLabelText('OCR 校对文字')
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('disk full') })
    fireEvent.click(screen.getByRole('button', { name: '确认修正' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('恢复记录')
    expect(confirm).not.toHaveBeenCalled()
  })
})
