import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
const voice = vi.hoisted(() => ({ state: 'idle', changed: null as null | (() => void) }))
vi.mock('./companionVoice', () => ({ getCompanionVoiceState: () => voice.state, subscribeCompanionVoice: (fn: () => void) => { voice.changed = fn; return () => { voice.changed = null } } }))
import { localApi } from './api'
import { useDailyReflectionSummary } from './useDailyReflectionSummary'

describe('spoken daily reflection summary', () => {
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); voice.state = 'idle' })
  const summary = { child_id: 'child-a', cloud_transmission_allowed: false, child_response: '今天做了分数作业，明天再试一道。' }
  it.each(['preparing', 'speaking'])('waits for commands and earlier %s, then narrates once without a button', async (state) => {
    const load = vi.spyOn(localApi, 'dailyLearningSummary').mockResolvedValue(summary as never)
    const onSummary = vi.fn()
    const { rerender } = renderHook(({ busy }) => useDailyReflectionSummary({ sessionId: 'session-a', childId: 'child-a', enabled: true, busy, onSummary }), { initialProps: { busy: true } })
    await waitFor(() => expect(load).toHaveBeenCalledOnce())
    expect(onSummary).not.toHaveBeenCalled()
    vi.useFakeTimers()
    voice.state = state
    rerender({ busy: false })
    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(onSummary).not.toHaveBeenCalled()
    await act(async () => { voice.state = 'idle'; voice.changed?.(); vi.advanceTimersByTime(700) })
    expect(onSummary).toHaveBeenCalledExactlyOnceWith(summary.child_response)
    await act(async () => { voice.changed?.(); vi.advanceTimersByTime(700) })
    expect(onSummary).toHaveBeenCalledOnce()
  })
  it('drops a late summary after privacy stop or navigation', async () => {
    let resolve!: (value: never) => void
    vi.spyOn(localApi, 'dailyLearningSummary').mockReturnValue(new Promise((done) => { resolve = done }))
    const onSummary = vi.fn()
    const { rerender } = renderHook(({ enabled }) => useDailyReflectionSummary({ sessionId: 'session-a', childId: 'child-a', enabled, busy: false, onSummary }), { initialProps: { enabled: true } })
    rerender({ enabled: false })
    vi.useFakeTimers()
    await act(async () => { resolve(summary as never); await Promise.resolve(); vi.advanceTimersByTime(2000) })
    expect(onSummary).not.toHaveBeenCalled()
  })
  it('does not read another child summary aloud when a response is mismatched', async () => {
    vi.spyOn(localApi, 'dailyLearningSummary').mockResolvedValue({ ...summary, child_id: 'child-b', child_response: '另一孩子内容' } as never)
    const onSummary = vi.fn()
    renderHook(() => useDailyReflectionSummary({ sessionId: 'session-a', childId: 'child-a', enabled: true, busy: false, onSummary }))
    await waitFor(() => expect(onSummary).toHaveBeenCalledOnce(), { timeout: 2000 })
    expect(onSummary.mock.calls[0][0]).not.toContain('另一孩子内容')
    expect(onSummary.mock.calls[0][0]).toContain('暂时没有读到')
  })
})
