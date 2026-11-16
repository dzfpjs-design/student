import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { ParentWorkNavigator } from './ParentCenter'
import type { OcrReviewItem } from './types'

const item = (childId: string): OcrReviewItem => ({ child_id: childId } as OcrReviewItem)
const deferred = <T,>(): { promise: Promise<T>; resolve: (value: T) => void } => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('Parent work queue recovery', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  function otherQueuesEmpty(): void {
    vi.spyOn(localApi, 'variantCandidates').mockResolvedValue([])
    vi.spyOn(localApi, 'specializedSupplyCandidates').mockResolvedValue([])
  }

  it('does not declare no work when an unread source fails, and recovers explicitly', async () => {
    otherQueuesEmpty()
    vi.spyOn(localApi, 'ocrReviewQueue').mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce([])
    render(<ParentWorkNavigator childId="a" safetyEvents={[]} />)
    expect(await screen.findByRole('button', { name: /待确认OCR 校对读取失败/ })).toBeInTheDocument()
    expect(screen.queryByText('当前没有需要家长处理的审核项。')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    expect(await screen.findByText('当前没有需要家长处理的审核项。')).toBeInTheDocument()
    expect(screen.queryByText('读取失败')).not.toBeInTheDocument()
  })

  it('keeps the last known count but labels it stale after a failed refresh', async () => {
    otherQueuesEmpty()
    vi.spyOn(localApi, 'ocrReviewQueue').mockResolvedValueOnce([item('a'), item('a')]).mockRejectedValueOnce(new Error('offline'))
    render(<ParentWorkNavigator childId="a" safetyEvents={[]} />)
    expect(await screen.findByRole('button', { name: '2OCR 校对' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    expect(await screen.findByRole('button', { name: '2OCR 校对上次结果，未更新' })).toBeInTheDocument()
  })

  it('never publishes an old child response or a mismatched server result', async () => {
    otherQueuesEmpty()
    const old = deferred<OcrReviewItem[]>()
    vi.spyOn(localApi, 'ocrReviewQueue').mockImplementation((childId) =>
      childId === 'a' ? old.promise : Promise.resolve([item('a')]))
    const view = render(<ParentWorkNavigator childId="a" safetyEvents={[]} />)
    view.rerender(<ParentWorkNavigator childId="b" safetyEvents={[]} />)
    expect(await screen.findByRole('button', { name: /待确认OCR 校对读取失败/ })).toBeInTheDocument()
    await act(async () => { old.resolve([item('a'), item('a')]); await old.promise })
    expect(screen.queryByRole('button', { name: /2OCR/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /待确认OCR 校对读取失败/ })).toBeInTheDocument()
  })

  it('makes successful sources actionable while another source is still loading', async () => {
    const slow = deferred<OcrReviewItem[]>()
    vi.spyOn(localApi, 'ocrReviewQueue').mockReturnValue(slow.promise)
    vi.spyOn(localApi, 'variantCandidates').mockResolvedValue([{ child_id: 'a' } as never])
    vi.spyOn(localApi, 'specializedSupplyCandidates').mockResolvedValue([])
    render(<ParentWorkNavigator childId="a" safetyEvents={[]} />)
    expect(await screen.findByRole('button', { name: '1错题变式审核' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '正在汇总' })).toBeDisabled()
    await act(async () => { slow.resolve([]); await slow.promise })
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled())
  })
})
