import { afterEach, expect, it, vi } from 'vitest'
import { withReceiptRecoveryWindow } from './receiptRecoveryWindow'

afterEach(() => vi.useRealTimers())

it('uses one ten-second budget across successive lookups, not ten seconds per lookup', async () => {
  vi.useFakeTimers()
  const stages: string[] = []
  const checked = withReceiptRecoveryWindow(() => true, undefined, async window => {
    await window.read(async () => { stages.push('admission'); await new Promise(resolve => setTimeout(resolve, 6_000)) })
    await window.read(async () => { stages.push('agent'); await new Promise(resolve => setTimeout(resolve, 6_000)) })
    if (window.current()) stages.push('acknowledge')
  })
  const failure = expect(checked).rejects.toMatchObject({ code: 'VOICE_RECEIPT_CHECK_TIMEOUT' })
  await vi.advanceTimersByTimeAsync(10_000)
  await failure
  expect(stages).toEqual(['admission', 'agent'])
  await vi.advanceTimersByTimeAsync(3_000)
  expect(stages).not.toContain('acknowledge')
  expect(vi.getTimerCount()).toBe(0)
})

it('stops a hung provider immediately on privacy or exit and rejects late work', async () => {
  const parent = new AbortController()
  let readSignal: AbortSignal | undefined
  let finish!: () => void
  let acknowledged = false
  const checked = withReceiptRecoveryWindow(() => true, parent.signal, async window => {
    await window.read(signal => {
      readSignal = signal
      return new Promise<void>(resolve => { finish = resolve })
    })
    if (window.current()) acknowledged = true
  })
  await Promise.resolve(); await Promise.resolve()
  const failure = expect(checked).rejects.toMatchObject({ name: 'AbortError' })
  parent.abort()
  await failure
  expect(readSignal?.aborted).toBe(true)
  finish()
  await Promise.resolve(); await Promise.resolve()
  expect(acknowledged).toBe(false)
})

it('does not start work after cancellation, and closes timers/listeners after success', async () => {
  vi.useFakeTimers()
  const work = vi.fn()
  await expect(withReceiptRecoveryWindow(() => true, AbortSignal.abort(), work)).rejects.toMatchObject({ name: 'AbortError' })
  expect(work).not.toHaveBeenCalled()
  const parent = new AbortController()
  const remove = vi.spyOn(parent.signal, 'removeEventListener')
  let valid!: () => boolean
  expect(await withReceiptRecoveryWindow(() => true, parent.signal, async window => {
    valid = window.current
    return window.read(async () => 'canonical receipt')
  })).toBe('canonical receipt')
  expect(valid()).toBe(false)
  expect(remove).toHaveBeenCalledWith('abort', expect.any(Function))
  expect(vi.getTimerCount()).toBe(0)
})
