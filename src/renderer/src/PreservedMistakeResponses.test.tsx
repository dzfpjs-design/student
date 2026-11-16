import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { PreservedMistakeResponses } from './PreservedMistakeResponses'

const item = { response_id: 'answer-2', child_id: 'child-a', mistake_id: 'mistake-a', original_version: 2,
  response_text: '我原来认为是九十，现在改为十二。', responded_at: '2026-09-07T12:00:00Z',
  source_erased: true as const, score_authority: 'none' as const, response_status: 'historical_only' as const }
const expand = () => {
  const details = screen.getByText('来源删除后的历史作答').closest('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))
}
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('preserved answer history is parent-readable but never score authority', () => {
  it('loads only when opened, shows original words and survives remount through the service', async () => {
    const request = vi.spyOn(localApi, 'preservedMistakeResponses').mockResolvedValue({ items: [item] })
    const mounted = render(<PreservedMistakeResponses childId="child-a" mistakeId="mistake-a" />)
    expect(request).not.toHaveBeenCalled()
    expand()
    expect(await screen.findByText(item.response_text)).toBeTruthy()
    expect(screen.getByText(/不作为掌握程度的依据/)).toBeTruthy()
    mounted.unmount()
    render(<PreservedMistakeResponses childId="child-a" mistakeId="mistake-a" />)
    expand()
    expect(await screen.findByText(item.response_text)).toBeTruthy()
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('retries failures and refuses another child or wrong score qualification', async () => {
    const request = vi.spyOn(localApi, 'preservedMistakeResponses')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ items: [{ ...item, child_id: 'child-b' }] })
      .mockResolvedValueOnce({ items: [item] })
    render(<PreservedMistakeResponses childId="child-a" mistakeId="mistake-a" />)
    expand()
    expect(await screen.findByRole('alert')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.queryByText(item.response_text)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText(item.response_text)).toBeTruthy()
  })

  it('drops a slow old-child result after switching scope', async () => {
    let resolve!: (value: { items: typeof item[] }) => void
    vi.spyOn(localApi, 'preservedMistakeResponses')
      .mockImplementationOnce(() => new Promise(done => { resolve = done }))
      .mockResolvedValueOnce({ items: [] })
    const mounted = render(<PreservedMistakeResponses childId="child-a" mistakeId="mistake-a" />)
    expand()
    mounted.rerender(<PreservedMistakeResponses childId="child-b" mistakeId="mistake-b" />)
    expand()
    await act(async () => { resolve({ items: [item] }) })
    expect(await screen.findByText('没有此类历史作答。')).toBeTruthy()
    expect(screen.queryByText(item.response_text)).toBeNull()
  })
})
