import { describe, expect, it, vi } from 'vitest'
import { VoiceInputQueue, resolveVoiceInteraction, VoiceOnboardingRuntime } from './voiceOnboardingRuntime'

const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('cloud voice interaction ordering', () => {
  it('queues continuous speech across a network wait without processing twice', async () => {
    const queue = new VoiceInputQueue()
    const pending = deferred<void>()
    const seen: string[] = []
    const first = queue.enqueue(async () => { seen.push('先跳过教程学数学'); await pending.promise })
    const second = queue.enqueue(async () => { seen.push('还有英语') })
    expect(seen).toEqual(['先跳过教程学数学'])
    pending.resolve()
    await Promise.all([first, second])
    expect(seen).toEqual(['先跳过教程学数学', '还有英语'])
  })

  it('times out without accepting a late cloud decision or retrying', async () => {
    vi.useFakeTimers()
    try {
      const pending = deferred<never>()
      const request = vi.fn((_signal: AbortSignal) => pending.promise)
      const result = resolveVoiceInteraction(request, new AbortController().signal, 10)
      const assertion = expect(result).rejects.toThrow('VOICE_INTERACTION_TIMEOUT')
      await vi.advanceTimersByTimeAsync(11)
      await assertion
      expect(request).toHaveBeenCalledTimes(1)
      expect(request.mock.calls[0]?.[0].aborted).toBe(true)
    } finally { vi.useRealTimers() }
  })

  it('cancels active routing and queued utterances even if the transport ignores abort', async () => {
    const queue = new VoiceInputQueue()
    const pending = deferred<never>()
    const action = vi.fn()
    const first = queue.enqueue(async (signal) => {
      try { await resolveVoiceInteraction(() => pending.promise, signal); action() } catch { /* cancelled */ }
    })
    const second = queue.enqueue(async () => { action() })
    queue.cancel()
    await Promise.all([first, second])
    expect(action).not.toHaveBeenCalled()
  })

  it('waits for progress storage before deferring and never advances on a mismatched receipt', async () => {
    const pending = deferred<any>()
    const save = vi.fn().mockReturnValueOnce(pending.promise).mockImplementation(async (value) => value)
    const runtime = new VoiceOnboardingRuntime('a', '小知', {
      getVoiceOnboarding: async () => null, saveVoiceOnboarding: save,
    }, () => undefined)
    const loading = runtime.load()
    await Promise.resolve()
    const deferring = runtime.applyAction('skip', true)
    expect(save).toHaveBeenCalledTimes(1)
    pending.resolve({ childId: 'a', version: 1, step: 'wake_practice', updatedAt: new Date().toISOString() })
    await Promise.all([loading, deferring])
    expect(runtime.snapshot).toMatchObject({ phase: 'idle', step: 'deferred', silent: true })
    expect(save).toHaveBeenCalledTimes(2)
  })

  it('guides the child from their fixed grade profile into spoken wake-word naming', async () => {
    const snapshots: string[] = []
    const runtime = new VoiceOnboardingRuntime('a', '', {
      getVoiceOnboarding: async () => null,
      saveVoiceOnboarding: async (value) => value,
    }, (value) => snapshots.push(value.prompt), '四年级')
    await runtime.load()
    expect(runtime.snapshot).toMatchObject({ phase: 'naming', step: null, silent: false })
    expect(runtime.snapshot.prompt).toContain('固定到四年级学习档案')
    expect(runtime.snapshot.prompt).toContain('请直接说出来')
    expect(snapshots.at(-1)).toBe(runtime.snapshot.prompt)
  })
})
