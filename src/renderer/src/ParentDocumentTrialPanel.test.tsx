import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ParentDocumentTrialPanel, type DocumentTrialReview } from './ParentDocumentTrialPanel'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const review: DocumentTrialReview = {
  session_id: 'one-session', summary: { frame_count: 1, correct_count: 0, wrong_count: 1, unscored_count: 0, capture_failure_count: 0 },
  findings: [], has_more: false, questions: [], frames: [{ artifact_id: 'one-frame', captured_at: '2026-09-30T10:00:00Z',
    reason: 'ocr_capture', image_available: true, questions: [{ candidate_id: 'candidate', question_text: '求速度', answer_text: '5 m/s', correctness: 0, authority_type: 'published_answer_key', mistake_id: 'mistake' }] }]
}
function request<T>(path: string): Promise<T> {
  return Promise.resolve((path.includes('?offset=') ? review : { items: [{ session_id: 'one-session', started_at: '2026-09-30T10:00:00Z', status: 'completed' }] }) as T)
}

it('replays the image beside its answer and scoring source and releases object URLs', async () => {
  const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:local-image')
  const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  const readImage = vi.fn().mockResolvedValue(new Blob(['synthetic'], { type: 'image/jpeg' }))
  const view = render(<ParentDocumentTrialPanel childId="one" request={request} readImage={readImage} />)
  await screen.findByAltText('该次高拍仪诊断采样原图')
  expect(screen.getByText('作答：5 m/s')).toBeTruthy()
  expect(screen.getByText('评分依据：标准答案')).toBeTruthy()
  expect(screen.getByText('错误')).toBeTruthy()
  expect(readImage).toHaveBeenCalledWith('one', 'one-frame', expect.any(AbortSignal))
  view.unmount(); expect(revoke).toHaveBeenCalledWith('blob:local-image'); expect(create).toHaveBeenCalledOnce()
})

it('rejects stale child reads and never requests a previous child session for the next child', async () => {
  let resolve!: (blob: Blob) => void
  const lateImage = new Promise<Blob>(yes => { resolve = yes })
  const reader = vi.fn().mockReturnValue(lateImage)
  const calls: string[] = []
  const scopedRequest = <T,>(path: string): Promise<T> => {
    calls.push(path)
    return path.includes('/children/two/') ? Promise.resolve({ items: [] } as T) : request<T>(path)
  }
  const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:stale')
  const view = render(<ParentDocumentTrialPanel childId="one" request={scopedRequest} readImage={reader} />)
  await waitFor(() => expect(reader).toHaveBeenCalledOnce())
  const signal = reader.mock.calls[0][2] as AbortSignal
  view.rerender(<ParentDocumentTrialPanel childId="two" request={scopedRequest} readImage={reader} />)
  await screen.findByText('尚无高拍仪试用留图。')
  await act(async () => resolve(new Blob(['stale'])))
  expect(signal.aborted).toBe(true); expect(create).not.toHaveBeenCalled()
  expect(calls.filter(path => path.includes('/children/two/'))).toEqual(['/v1/parent/children/two/document-trial/sessions'])
})

it('shows recoverable image failure rather than infinite loading and retries on refresh', async () => {
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recovered')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  const reader = vi.fn().mockRejectedValueOnce(new Error('missing')).mockResolvedValue(new Blob(['image']))
  render(<ParentDocumentTrialPanel childId="one" request={request} readImage={reader} />)
  await screen.findByText('图片读取失败，请刷新重试。')
  fireEvent.click(screen.getByTitle('刷新复盘'))
  await screen.findByAltText('该次高拍仪诊断采样原图')
})

it('pages through all frame questions and returns from a failed page without stale loading', async () => {
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:local-image')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  const calls: string[] = []
  let failed = false
  const pagedRequest = <T,>(path: string): Promise<T> => {
    calls.push(path)
    if (path.includes('/frames/')) {
      if (!failed) { failed = true; return Promise.reject(new Error('network')) }
      return Promise.resolve({ questions: [{ ...review.frames[0].questions[0], candidate_id: 'last', question_text: '最后一道题' }], has_more: false } as T)
    }
    if (path.includes('?offset=')) return Promise.resolve({ ...review, frames: [{ ...review.frames[0], questions_has_more: true }] } as T)
    return request<T>(path)
  }
  render(<ParentDocumentTrialPanel childId="one" request={pagedRequest} readImage={vi.fn().mockResolvedValue(new Blob(['image']))} />)
  await screen.findByText('求速度')
  fireEvent.click(screen.getByTitle('下一组作答'))
  await screen.findByRole('alert')
  expect(screen.getByText('读取未完成')).toBeInTheDocument()
  expect(screen.queryByText('101–101')).toBeNull()
  fireEvent.click(screen.getByTitle('重试作答记录'))
  await screen.findByText('最后一道题')
  expect(calls).toContain('/v1/parent/children/one/document-trial/frames/one-frame/questions?offset=100')
  expect(screen.queryByText('求速度')).toBeNull()
  fireEvent.click(screen.getByTitle('上一组作答'))
  await screen.findByText('求速度')
  expect(screen.queryByRole('status')).toBeNull()
})
