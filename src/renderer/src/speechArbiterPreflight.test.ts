import { afterEach, describe, expect, it, vi } from 'vitest'
import { SpeechArbiter } from './speechArbiter'

const deferred = () => { let resolve!: (value: boolean) => void; const promise = new Promise<boolean>(r => { resolve = r }); return { promise, resolve } }
afterEach(() => vi.useRealTimers())

describe('existing speech queue preflight lease', () => {
  it.each(['dialogue', 'lesson'] as const)('immediately yields a preparing reminder to %s without stopping its replacement', async priority => {
    const stop = vi.fn(), queue = new SpeechArbiter(stop), pending = deferred()
    const stale = vi.fn(), replacement = vi.fn(), settled = vi.fn()
    let signal!: AbortSignal
    queue.submit({ scope: 'a', priority: 'reminder', beforePlay: s => { signal = s; return pending.promise }, play: stale, settled })
    await Promise.resolve()
    queue.submit({ scope: 'a', priority, play: replacement })
    try {
      expect(replacement).toHaveBeenCalledTimes(1)
      expect(signal.aborted).toBe(true)
      expect(settled).toHaveBeenCalledExactlyOnceWith('superseded')
      expect(stop).not.toHaveBeenCalled()
      pending.resolve(true); await Promise.resolve(); await Promise.resolve()
      expect(stale).not.toHaveBeenCalled()
      expect(replacement).toHaveBeenCalledTimes(1)
      expect(stop).not.toHaveBeenCalled()
    } finally { queue.cancel() }
  })

  it.each(['dialogue', 'lesson'] as const)('does not interrupt an already started reminder for a later %s', async priority => {
    const stop = vi.fn(), queue = new SpeechArbiter(stop), pending = deferred(), next = vi.fn()
    let finish!: () => void
    queue.submit({ scope: 'a', priority: 'reminder', beforePlay: () => pending.promise, play: end => { finish = end } })
    pending.resolve(true); await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    queue.submit({ scope: 'a', priority, play: next })
    expect(next).not.toHaveBeenCalled(); expect(stop).not.toHaveBeenCalled()
    finish()
    expect(next).toHaveBeenCalledTimes(1); expect(stop).not.toHaveBeenCalled()
    queue.cancel()
  })

  it.each(['reminder', 'dialogue'] as const)('keeps a preparing dialogue lease when the newcomer is %s', async priority => {
    const queue = new SpeechArbiter(vi.fn()), pending = deferred(), next = vi.fn()
    let signal!: AbortSignal, finish!: () => void
    queue.submit({ scope: 'a', priority: 'dialogue', beforePlay: s => { signal = s; return pending.promise }, play: end => { finish = end } })
    await Promise.resolve()
    queue.submit({ scope: 'a', priority, play: next })
    expect(signal.aborted).toBe(false); expect(next).not.toHaveBeenCalled()
    pending.resolve(true); await vi.waitFor(() => expect(finish).toBeTypeOf('function'))
    finish(); expect(next).toHaveBeenCalledTimes(1); queue.cancel()
  })

  it('checks only the dequeued entry, then moves past a withdrawn source without stopping other speech', async () => {
    const stop = vi.fn(), first = vi.fn(), next = vi.fn(), preflight = vi.fn(async () => false)
    let finish!: () => void
    const queue = new SpeechArbiter(stop)
    queue.submit({ scope: 'a', play: end => { finish = end } })
    queue.submit({ scope: 'a', play: first, beforePlay: preflight })
    queue.submit({ scope: 'a', play: next })
    expect(preflight).not.toHaveBeenCalled()
    finish(); await vi.waitFor(() => expect(next).toHaveBeenCalledTimes(1))
    expect(first).not.toHaveBeenCalled(); expect(stop).not.toHaveBeenCalled()
    queue.cancel()
  })

  it.each(['cancel', 'scope', 'urgent'])('rejects a late valid read after %s releases its original lease', async action => {
    const queue = new SpeechArbiter(vi.fn()), pending = deferred(), obsolete = vi.fn(), replacement = vi.fn()
    let signal!: AbortSignal
    queue.submit({ scope: 'a', beforePlay: s => { signal = s; return pending.promise }, play: obsolete })
    await Promise.resolve()
    if (action === 'cancel') queue.cancel()
    queue.submit({ scope: action === 'scope' ? 'b' : 'a', priority: action === 'urgent' ? 'urgent' : 'dialogue', play: replacement })
    pending.resolve(true)
    await vi.waitFor(() => expect(replacement).toHaveBeenCalledTimes(1))
    expect(signal.aborted).toBe(true); expect(obsolete).not.toHaveBeenCalled()
    queue.cancel()
  })

  it('bounds a hung read and releases the queue; a late response cannot resurrect playback', async () => {
    vi.useFakeTimers()
    const queue = new SpeechArbiter(vi.fn()), pending = deferred(), stale = vi.fn(), next = vi.fn(), settled = vi.fn()
    let signal!: AbortSignal
    queue.submit({ scope: 'a', beforePlay: s => { signal = s; return pending.promise }, play: stale, settled })
    queue.submit({ scope: 'a', play: next })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(signal.aborted).toBe(true); expect(settled).toHaveBeenCalledExactlyOnceWith('unavailable')
    expect(next).toHaveBeenCalledTimes(1)
    pending.resolve(true); await Promise.resolve(); await Promise.resolve()
    expect(stale).not.toHaveBeenCalled()
    queue.cancel()
  })

  it('rechecks owner and expiry after the read without stopping a later owner', async () => {
    let now = 0, owner = true
    const queue = new SpeechArbiter(vi.fn(), () => now), pending = deferred(), stale = vi.fn(), settled = vi.fn()
    queue.submit({ scope: 'a', expiresAt: 50, isCurrent: () => owner, beforePlay: () => pending.promise, play: stale, settled })
    await Promise.resolve(); now = 51; owner = false; pending.resolve(true)
    await vi.waitFor(() => expect(settled).toHaveBeenCalledExactlyOnceWith('superseded'))
    expect(stale).not.toHaveBeenCalled(); queue.cancel()
  })

  it('treats a thrown preflight as unavailable, not a queue lock or playback permission', async () => {
    const queue = new SpeechArbiter(vi.fn()), stale = vi.fn(), next = vi.fn()
    queue.submit({ scope: 'a', beforePlay: () => { throw new Error('read failed') }, play: stale })
    queue.submit({ scope: 'a', play: next })
    await vi.waitFor(() => expect(next).toHaveBeenCalledTimes(1))
    expect(stale).not.toHaveBeenCalled(); queue.cancel()
  })
})
