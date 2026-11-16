import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, localApi } from './api'
import { LocalObservation } from './SessionWorkspace'
import type { PendingBehaviorInterventionResponse } from './types'

const invitation = (id = 'original', message = '你愿意试试我们约好的这一步吗？先圈出卡点。'): PendingBehaviorInterventionResponse => ({
  status: 'pending', intervention: { intervention_id: id, message,
    expires_at: '2099-01-01T00:00:00Z', requires_child_feedback: true }
})
const defer = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
const view = (sessionId = 'a') => <LocalObservation sessionId={sessionId} active enabled={false} snoozeUntil={null} />
async function settle(): Promise<void> { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }

describe('observation invitation source-current display and response', () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    vi.spyOn(localApi, 'pendingSessionBehaviorIntervention').mockResolvedValue(invitation())
    vi.spyOn(localApi, 'respondToSessionBehaviorIntervention').mockResolvedValue({ intervention_id: 'original', feedback: 'accepted' })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('NO_NETWORK')))
  })
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers() })

  it('removes the exact stopped invitation when returning from a parent stop', async () => {
    render(view()); await settle()
    expect(screen.getByRole('button', { name: '愿意' })).toBeEnabled()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue({ status: 'no_pending' })
    fireEvent(window, new Event('focus')); await settle()
    expect(screen.queryByRole('group', { name: '回答学习提醒' })).not.toBeInTheDocument()
    expect(localApi.respondToSessionBehaviorIntervention).not.toHaveBeenCalled()
  })

  it('rechecks before accepting instead of writing an already stopped original', async () => {
    render(view()); await settle()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue({ status: 'no_pending' })
    fireEvent.click(screen.getByRole('button', { name: '愿意' })); await settle()
    expect(localApi.respondToSessionBehaviorIntervention).not.toHaveBeenCalled()
    expect(screen.queryByRole('group', { name: '回答学习提醒' })).not.toBeInTheDocument()
  })

  it('refreshes a still-mounted invitation after an external parent stop without focus', async () => {
    vi.useFakeTimers(); render(view()); await settle()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue({ status: 'no_pending' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(screen.queryByRole('group', { name: '回答学习提醒' })).not.toBeInTheDocument()
  })

  it('clears a previously loaded card on expired instead of leaving its accept action', async () => {
    render(view()); await settle()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue({ status: 'expired', expires_at: '2026-01-01T00:00:00Z' })
    fireEvent(window, new Event('focus')); await settle()
    expect(screen.queryByRole('button', { name: '愿意' })).not.toBeInTheDocument()
  })

  it('keeps unknown read failure non-actionable and retries without submitting', async () => {
    render(view()); await settle()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockRejectedValue(new Error('offline'))
    fireEvent(window, new Event('focus')); await settle()
    expect(screen.queryByText('直接说“愿意”“稍后”或“这类提醒少一点”。')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '愿意' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '重试恢复' })).toBeEnabled()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue({ status: 'no_pending' })
    fireEvent.click(screen.getByRole('button', { name: '重试恢复' })); await settle()
    expect(screen.queryByRole('group', { name: '回答学习提醒' })).not.toBeInTheDocument()
    expect(localApi.respondToSessionBehaviorIntervention).not.toHaveBeenCalled()
  })

  it('does not substitute a different current invitation for the clicked original', async () => {
    render(view()); await settle()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue(invitation('new', '现在是另一条提醒。'))
    fireEvent.click(screen.getByRole('button', { name: '愿意' })); await settle()
    expect(localApi.respondToSessionBehaviorIntervention).not.toHaveBeenCalled()
    expect(screen.getByText('现在是另一条提醒。')).toBeInTheDocument()
  })

  it('does not let a late previous session restore replace the new child invitation', async () => {
    const old = defer<PendingBehaviorInterventionResponse>()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockReturnValueOnce(old.promise)
    const mounted = render(view()); await settle()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue(invitation('b', '另一孩子当前提醒。'))
    mounted.rerender(view('b')); await settle()
    await act(async () => { old.resolve(invitation()) })
    expect(screen.getByText('另一孩子当前提醒。')).toBeInTheDocument()
    expect(screen.queryByText(invitation().intervention!.message)).not.toBeInTheDocument()
  })

  it('does not infer invalidity just because observation or the task is paused', async () => {
    const mounted = render(view()); await settle()
    mounted.rerender(view()); fireEvent(window, new Event('focus')); await settle()
    expect(screen.getByRole('button', { name: '愿意' })).toBeEnabled()
    expect(screen.getByText(invitation().intervention!.message)).toBeInTheDocument()
  })

  it('handles a stop after preflight as 409 and removes the rejected invitation', async () => {
    vi.mocked(localApi.respondToSessionBehaviorIntervention).mockImplementation(async () => {
      vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue({ status: 'no_pending' })
      throw new ApiError('这条提醒已处理或已过期。', 409)
    })
    render(view()); await settle()
    fireEvent.click(screen.getByRole('button', { name: '愿意' })); await settle()
    expect(screen.queryByRole('group', { name: '回答学习提醒' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '核对原回答' })).not.toBeInTheDocument()
    expect(screen.queryByText('已记下：你愿意试一试。')).not.toBeInTheDocument()
  })

  it('keeps an attempted exact command recoverable without reviving an invitation after lost receipt', async () => {
    vi.useFakeTimers()
    vi.mocked(localApi.respondToSessionBehaviorIntervention).mockRejectedValueOnce(new Error('lost response'))
    render(view()); await settle()
    fireEvent.click(screen.getByRole('button', { name: '愿意' })); await settle()
    const original = vi.mocked(localApi.respondToSessionBehaviorIntervention).mock.calls[0]
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockResolvedValue({ status: 'no_pending' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(screen.queryByRole('button', { name: '愿意' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '核对原回答' })); await settle()
    expect(vi.mocked(localApi.respondToSessionBehaviorIntervention).mock.calls[1]).toEqual(original)
    expect(screen.getByText('已记下：你愿意试一试。')).toBeInTheDocument()
  })

  it('does not submit after a late preflight from a previous session', async () => {
    render(view()); await settle()
    const check = defer<PendingBehaviorInterventionResponse>()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockReturnValueOnce(check.promise)
    fireEvent.click(screen.getByRole('button', { name: '愿意' }))
    // Unmount is the real child switch boundary; the old request must stay inert.
    cleanup(); render(view('b')); await settle()
    await act(async () => { check.resolve(invitation()) })
    expect(localApi.respondToSessionBehaviorIntervention).not.toHaveBeenCalled()
  })

  it('does not overlap repeated timer reads while a current read is still unresolved', async () => {
    vi.useFakeTimers(); render(view()); await settle()
    const check = defer<PendingBehaviorInterventionResponse>()
    vi.mocked(localApi.pendingSessionBehaviorIntervention).mockReturnValue(check.promise)
    await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
    expect(localApi.pendingSessionBehaviorIntervention).toHaveBeenCalledTimes(2)
    await act(async () => { check.resolve({ status: 'no_pending' }) })
  })

  it('does not launch a recovery read from a late rejected write after unmount', async () => {
    let reject!: (error: Error) => void
    vi.mocked(localApi.respondToSessionBehaviorIntervention).mockReturnValue(new Promise((_resolve, fail) => { reject = fail }))
    const mounted = render(view()); await settle()
    fireEvent.click(screen.getByRole('button', { name: '愿意' })); await settle()
    const reads = vi.mocked(localApi.pendingSessionBehaviorIntervention).mock.calls.length
    mounted.unmount()
    await act(async () => { reject(new ApiError('已撤回', 409)) })
    expect(localApi.pendingSessionBehaviorIntervention).toHaveBeenCalledTimes(reads)
  })
})
