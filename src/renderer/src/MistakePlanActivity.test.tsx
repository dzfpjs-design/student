import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { localApi } from './api'
import { MistakePlanActivity } from './MistakePlanActivity'
import type { MistakeItem, MistakeList, MistakeNextActivity } from './types'
import { privacyStopKey } from './deviceCommandReceipt'

const item = {
  mistake_id: 'mistake-1', child_id: 'child-1', cluster_id: 'cluster-1', diagnostic_item_id: null,
  source_evidence_id: null, subject: '数学', title: '数列规律与归纳', prompt_summary: '找规律',
  submitted_answer: '12', source_type: 'manual', source_ref: null, confidence: 1,
  status: 'awaiting_confirmation', diagnosis_status: 'suspected', occurrence_count: 1, relapse_count: 0,
  confirmed_at: null, created_at: '', updated_at: '',
  knowledge_node: { knowledge_node_id: 'node-1', title: '数列规律与归纳', code: 'N1', track: 'school' },
  diagnosis: null, mastery_gate: null
} as MistakeItem

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const list = (...items: MistakeItem[]): MistakeList => ({ items,
  counts: { awaiting_confirmation: 0, today_strengthening: items.length, due_review: 0, mastered: 0 } })
const attempt = (id: string, prompt: string): MistakeNextActivity => ({ kind: 'attempt', stage: 'near_transfer',
  item: { diagnostic_item_id: `question-${id}`, prompt } } as MistakeNextActivity)
const retry = (action: 'reload' | 'sync', resolve = vi.fn()) => {
  act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-recovery-voice-action', { detail: { action, resolve } })))
  return resolve
}
const originalScroll = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')

describe('MistakePlanActivity', () => {
  afterEach(() => {
    cleanup(); vi.restoreAllMocks(); localStorage.clear()
    if (originalScroll) Object.defineProperty(Element.prototype, 'scrollIntoView', originalScroll)
    else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  })

  it('keeps B question, focus and voice action when A finishes loading last', async () => {
    const a = { ...item, mistake_id: 'A', status: 'near_transfer' as const }
    const b = { ...item, mistake_id: 'B', status: 'near_transfer' as const }
    const old = deferred<MistakeList>()
    vi.spyOn(localApi, 'mistakes').mockReturnValueOnce(old.promise).mockResolvedValueOnce(list(b))
    const next = vi.spyOn(localApi, 'nextMistakeActivity').mockImplementation(async id => attempt(id, `${id}题干`))
    const submit = vi.spyOn(localApi, 'submitMistakeAttempt').mockResolvedValue(b)
    const focus = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: focus })
    const oldProgress = vi.fn(), newProgress = vi.fn().mockResolvedValue(true)
    const view = render(<MistakePlanActivity sessionId="session" childId="child-1" mistakeId="A" active onProgress={oldProgress} />)
    view.rerender(<MistakePlanActivity sessionId="session" childId="child-1" mistakeId="B" active onProgress={newProgress} />)
    await screen.findByText('B题干')
    await act(async () => { old.resolve(list(a)); await old.promise })
    expect(screen.queryByText('A题干')).toBeNull()
    expect(next.mock.calls).toEqual([['B', 'child-1']])
    expect(focus).toHaveBeenCalledTimes(1)
    expect((focus.mock.instances[0] as HTMLElement).textContent).toBe('B题干')
    act(() => window.dispatchEvent(new CustomEvent('hsc-mistake-voice-action', {
      detail: { action: 'answer', answer: 'B答案', resolve: vi.fn() }
    })))
    await waitFor(() => expect(newProgress).toHaveBeenCalledTimes(1))
    expect(submit).toHaveBeenCalledWith('B', 'child-1', 'near_transfer', 'question-B', 'B答案')
    expect(oldProgress).not.toHaveBeenCalled()
  })

  it('does not synchronize an A save that returns after switching to B', async () => {
    const b = { ...item, mistake_id: 'B', status: 'near_transfer' as const }
    vi.spyOn(localApi, 'mistakes').mockResolvedValueOnce(list(item)).mockResolvedValueOnce(list(b))
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValueOnce({ kind: 'confirmation', stage: null, item: null })
      .mockResolvedValueOnce(attempt('B', 'B题干'))
    const saved = deferred<MistakeItem>()
    const confirm = vi.spyOn(localApi, 'confirmMistake').mockReturnValue(saved.promise)
    const oldProgress = vi.fn(), newProgress = vi.fn()
    const view = render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={oldProgress} />)
    fireEvent.click(await screen.findByRole('button', { name: '识别有误，排除记录' }))
    expect(confirm).toHaveBeenCalledTimes(1)
    view.rerender(<MistakePlanActivity childId="child-1" mistakeId="B" active onProgress={newProgress} />)
    await screen.findByText('B题干')
    await act(async () => { saved.resolve({ ...item, status: 'excluded' }); await saved.promise })
    expect(screen.getByText('B题干')).toBeInTheDocument()
    expect(oldProgress).not.toHaveBeenCalled(); expect(newProgress).not.toHaveBeenCalled()
    expect(screen.queryByText(/记录已经排除/)).toBeNull()
  })

  it('lets a newer retry win and stops old loads after unmount', async () => {
    const first = deferred<MistakeList>()
    const unmounted = deferred<MistakeList>()
    vi.spyOn(localApi, 'mistakes').mockReturnValueOnce(first.promise).mockResolvedValueOnce(list(item))
      .mockReturnValueOnce(unmounted.promise)
    const next = vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const view = render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={vi.fn()} />)
    const handled = retry('reload')
    await screen.findByText('正在核对评分依据')
    await act(async () => { first.resolve(list()); await first.promise })
    expect(screen.queryByText(/已被移除/)).toBeNull()
    expect(handled).toHaveBeenCalledWith(true)
    retry('reload')
    view.unmount()
    await act(async () => { unmounted.resolve(list(item)); await unmounted.promise })
    expect(next).toHaveBeenCalledTimes(1)
  })

  it('rejects late next activity results and a list owned by another child', async () => {
    const a = { ...item, mistake_id: 'A', status: 'near_transfer' as const }
    const b = { ...item, mistake_id: 'B', status: 'near_transfer' as const }
    const oldNext = deferred<MistakeNextActivity>()
    vi.spyOn(localApi, 'mistakes').mockResolvedValueOnce(list(a)).mockResolvedValueOnce(list(b))
      .mockResolvedValueOnce(list({ ...b, child_id: 'other-child' }))
    const next = vi.spyOn(localApi, 'nextMistakeActivity').mockReturnValueOnce(oldNext.promise)
      .mockResolvedValueOnce(attempt('B', 'B题干'))
    const view = render(<MistakePlanActivity childId="child-1" mistakeId="A" active onProgress={vi.fn()} />)
    await waitFor(() => expect(next).toHaveBeenCalledTimes(1))
    view.rerender(<MistakePlanActivity childId="child-1" mistakeId="B" active onProgress={vi.fn()} />)
    await screen.findByText('B题干')
    await act(async () => { oldNext.resolve(attempt('A', 'A题干')); await oldNext.promise })
    expect(screen.queryByText('A题干')).toBeNull()
    retry('reload')
    await screen.findByText(/已被移除/)
    expect(next).toHaveBeenCalledTimes(2)
  })

  it('does not apply an unmounted sync receipt or let its callback remain current', async () => {
    const b = { ...item, mistake_id: 'B', status: 'near_transfer' as const }
    vi.spyOn(localApi, 'mistakes').mockResolvedValueOnce(list(item)).mockResolvedValueOnce(list(b))
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValueOnce({ kind: 'confirmation', stage: null, item: null })
      .mockResolvedValueOnce(attempt('B', 'B题干'))
    vi.spyOn(localApi, 'confirmMistake').mockResolvedValue({ ...item, status: 'excluded' })
    const syncing = deferred<boolean>()
    const oldProgress = vi.fn((_updated: MistakeItem, isCurrent: () => boolean) => {
      expect(isCurrent()).toBe(true)
      return syncing.promise
    })
    const view = render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={oldProgress} />)
    fireEvent.click(await screen.findByRole('button', { name: '识别有误，排除记录' }))
    await waitFor(() => expect(oldProgress).toHaveBeenCalledTimes(1))
    view.rerender(<MistakePlanActivity childId="child-1" mistakeId="B" active onProgress={vi.fn()} />)
    expect(oldProgress.mock.calls[0][1]()).toBe(false)
    await screen.findByText('B题干')
    await act(async () => { syncing.resolve(false); await syncing.promise })
    expect(screen.queryByText(/计划状态还没有同步/)).toBeNull()
    expect(screen.queryByRole('button', { name: '重试同步计划进度' })).toBeNull()
  })

  it('keeps a saved receipt for explicit recovery when pause interrupts the save', async () => {
    vi.spyOn(localApi, 'mistakes').mockResolvedValue(list(item))
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const saving = deferred<MistakeItem>()
    const confirm = vi.spyOn(localApi, 'confirmMistake').mockReturnValue(saving.promise)
    const onProgress = vi.fn().mockResolvedValue(true)
    const view = render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    fireEvent.click(await screen.findByRole('button', { name: '识别有误，排除记录' }))
    view.rerender(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active={false} onProgress={onProgress} />)
    view.rerender(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    await act(async () => { saving.resolve({ ...item, status: 'excluded' }); await saving.promise })
    expect(onProgress).not.toHaveBeenCalled()
    expect(await screen.findByRole('button', { name: '重试同步计划进度' })).toBeEnabled()
    retry('sync')
    await waitFor(() => expect(onProgress).toHaveBeenCalledTimes(1))
    expect(confirm).toHaveBeenCalledTimes(1)
  })

  it('blocks privacy stopped actions and late follow-up progress', async () => {
    vi.spyOn(localApi, 'mistakes').mockResolvedValue(list(item))
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const saving = deferred<MistakeItem>()
    const confirm = vi.spyOn(localApi, 'confirmMistake').mockReturnValue(saving.promise)
    const onProgress = vi.fn()
    render(<MistakePlanActivity sessionId="session" childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    const button = await screen.findByRole('button', { name: '识别有误，排除记录' })
    localStorage.setItem(privacyStopKey('session'), 'stopped')
    fireEvent.click(button)
    expect(confirm).not.toHaveBeenCalled()
    localStorage.removeItem(privacyStopKey('session'))
    fireEvent.click(button)
    expect(confirm).toHaveBeenCalledTimes(1)
    localStorage.setItem(privacyStopKey('session'), 'stopped')
    await act(async () => { saving.resolve({ ...item, status: 'excluded' }); await saving.promise })
    expect(onProgress).not.toHaveBeenCalled()
  })

  it('does not reload or launch concurrent syncs while a saved step is being reconciled', async () => {
    const mistakes = vi.spyOn(localApi, 'mistakes').mockResolvedValue(list(item))
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const saving = deferred<MistakeItem>(), syncing = deferred<boolean>()
    vi.spyOn(localApi, 'confirmMistake').mockReturnValue(saving.promise)
    const onProgress = vi.fn().mockReturnValue(syncing.promise)
    render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    fireEvent.click(await screen.findByRole('button', { name: '识别有误，排除记录' }))
    const whileSaving = retry('reload')
    await waitFor(() => expect(whileSaving).toHaveBeenCalledWith(false))
    await act(async () => { saving.resolve({ ...item, status: 'excluded' }); await saving.promise })
    await waitFor(() => expect(onProgress).toHaveBeenCalledTimes(1))
    const sync = retry('sync'), reload = retry('reload')
    await waitFor(() => expect(sync).toHaveBeenCalledWith(false))
    expect(reload).toHaveBeenCalledWith(false)
    expect(mistakes).toHaveBeenCalledTimes(1); expect(onProgress).toHaveBeenCalledTimes(1)
    await act(async () => { syncing.resolve(false); await syncing.promise })
    expect(await screen.findByRole('button', { name: '重试同步计划进度' })).toBeEnabled()
  })

  it('recovers a rejected plan sync without repeating its persisted mistake action', async () => {
    vi.spyOn(localApi, 'mistakes').mockResolvedValue(list(item))
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const confirm = vi.spyOn(localApi, 'confirmMistake').mockResolvedValue({ ...item, status: 'excluded' })
    const onProgress = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(true)
    render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    fireEvent.click(await screen.findByRole('button', { name: '识别有误，排除记录' }))
    await screen.findByText(/计划进度暂未同步/)
    const handled = retry('sync')
    await waitFor(() => expect(handled).toHaveBeenCalledWith(true))
    expect(confirm).toHaveBeenCalledTimes(1); expect(onProgress).toHaveBeenCalledTimes(2)
  })

  it('does not let a child turn an unscored observation into a mistake', async () => {
    vi.spyOn(localApi, 'mistakes').mockResolvedValue({ items: [item], counts: { awaiting_confirmation: 1, today_strengthening: 0, due_review: 0, mastered: 0 } })
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const confirm = vi.spyOn(localApi, 'confirmMistake')
    const onProgress = vi.fn().mockResolvedValue(true)

    render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    expect(await screen.findByText('正在核对评分依据')).toBeInTheDocument()

    expect(screen.queryByRole('button', { name: '确认是错题' })).toBeNull()
    expect(confirm).not.toHaveBeenCalled()
    expect(onProgress).not.toHaveBeenCalled()
  })

  it('rejects a legacy voice confirmation of correctness', async () => {
    vi.spyOn(localApi, 'mistakes').mockResolvedValue({ items: [item], counts: { awaiting_confirmation: 1, today_strengthening: 0, due_review: 0, mastered: 0 } })
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const confirm = vi.spyOn(localApi, 'confirmMistake')
    const onProgress = vi.fn().mockResolvedValue(true)
    render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    await screen.findByText('正在核对评分依据')

    let handled: boolean | null = null
    window.dispatchEvent(new CustomEvent('hsc-mistake-voice-action', {
      detail: { action: 'review', decision: 'confirm', resolve: (value: boolean) => { handled = value } }
    }))

    await waitFor(() => expect(handled).toBe(false))
    expect(confirm).not.toHaveBeenCalled()
    expect(onProgress).not.toHaveBeenCalled()
  })

  it('recovers a failed mistake load from the child voice event', async () => {
    const mistakes = vi.spyOn(localApi, 'mistakes')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ items: [item], counts: { awaiting_confirmation: 1, today_strengthening: 0, due_review: 0, mastered: 0 } })
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={vi.fn()} />)
    expect(await screen.findByText(/错题步骤没有加载成功/)).toBeInTheDocument()

    const handled = vi.fn()
    window.dispatchEvent(new CustomEvent('hsc-mistake-recovery-voice-action', {
      detail: { action: 'reload', resolve: handled }
    }))

    expect(await screen.findByText('正在核对评分依据')).toBeInTheDocument()
    expect(mistakes).toHaveBeenCalledTimes(2)
    expect(handled).toHaveBeenCalledWith(true)
  })

  it('persists exclusion of a wrongly recognized record before synchronizing the plan', async () => {
    vi.spyOn(localApi, 'mistakes').mockResolvedValue({ items: [item], counts: { awaiting_confirmation: 1, today_strengthening: 0, due_review: 0, mastered: 0 } })
    vi.spyOn(localApi, 'nextMistakeActivity').mockResolvedValue({ kind: 'confirmation', stage: null, item: null })
    const updated = { ...item, status: 'excluded' as const }
    const confirm = vi.spyOn(localApi, 'confirmMistake').mockResolvedValue(updated)
    const onProgress = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    render(<MistakePlanActivity childId="child-1" mistakeId="mistake-1" active onProgress={onProgress} />)
    fireEvent.click(await screen.findByRole('button', { name: '识别有误，排除记录' }))
    expect(await screen.findByText(/计划状态还没有同步/)).toBeInTheDocument()

    const handled = vi.fn()
    window.dispatchEvent(new CustomEvent('hsc-mistake-recovery-voice-action', {
      detail: { action: 'sync', resolve: handled }
    }))

    await waitFor(() => expect(onProgress).toHaveBeenCalledTimes(2))
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(confirm).toHaveBeenCalledWith('mistake-1', 'child-1', 'exclude')
    expect(handled).toHaveBeenCalledWith(true)
  })
})
