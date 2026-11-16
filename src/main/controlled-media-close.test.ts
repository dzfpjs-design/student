import { EventEmitter } from 'node:events'
import { describe, expect, it, vi } from 'vitest'
import { createControlledMediaCloser } from './controlled-media-close'

function setup(timeoutMs = 20) {
  class Window extends EventEmitter {
    destroyed = false
    isDestroyed = () => this.destroyed
    close = vi.fn()
    finish() { this.destroyed = true; this.emit('closed') }
  }
  const original = new Window()
  let current: Window | null = original
  const markReason = vi.fn()
  const close = createControlledMediaCloser(() => current, markReason, timeoutMs)
  return { original, markReason, close, replace: () => (current = new Window()), clear: () => { current = null } }
}

describe('controlled media needs the captured window closed receipt', () => {
  it('does not treat close requested as closed', async () => {
    const s = setup(), settled = vi.fn()
    const pending = s.close('child_requested').then(value => { settled(value); return value })
    await Promise.resolve()
    expect(s.original.close).toHaveBeenCalledOnce(); expect(settled).not.toHaveBeenCalled()
    s.original.finish()
    expect(await pending).toEqual({ closed: true })
    expect(s.original.listenerCount('closed')).toBe(0)
  })
  it('returns false on prevented close timeout without destroying or retrying', async () => {
    const s = setup()
    expect(await s.close('child_requested')).toEqual({ closed: false })
    expect(s.original.close).toHaveBeenCalledOnce()
    expect(s.original.isDestroyed()).toBe(false)
    expect(s.original.listenerCount('closed')).toBe(0)
  })
  it('returns false when close throws', async () => {
    const s = setup()
    s.original.close.mockImplementation(() => { throw Error('OS close unavailable') })
    expect(await s.close('child_requested')).toEqual({ closed: false })
    expect(s.original.listenerCount('closed')).toBe(0)
  })
  it('does not close or claim a replacement window was closed', async () => {
    const s = setup(), pending = s.close('child_requested')
    const replacement = s.replace()
    s.original.finish()
    expect(await pending).toEqual({ closed: false })
    expect(replacement.close).not.toHaveBeenCalled()
  })
  it('coalesces concurrent closes for one original window and preserves its first reason', async () => {
    const s = setup(), first = s.close('child_requested'), retry = s.close('retry')
    s.original.finish()
    expect(await first).toEqual({ closed: true }); expect(await retry).toEqual({ closed: true })
    expect(s.original.close).toHaveBeenCalledOnce()
    expect(s.markReason).toHaveBeenCalledExactlyOnceWith(s.original, 'child_requested')
  })
  it('can retry a failed close against the same live window', async () => {
    const s = setup()
    expect(await s.close('first')).toEqual({ closed: false })
    const retried = s.close('second'); s.original.finish()
    expect(await retried).toEqual({ closed: true })
    expect(s.original.close).toHaveBeenCalledTimes(2)
  })
  it('accepts absent or already destroyed windows as a proven closed state', async () => {
    const s = setup(); s.original.destroyed = true
    expect(await s.close('child_requested')).toEqual({ closed: true })
    s.clear(); expect(await s.close('child_requested')).toEqual({ closed: true })
    expect(s.original.close).not.toHaveBeenCalled()
  })
  it('does not infer all windows closed from an empty pointer while an older window survives', async () => {
    let retained = true
    const close = createControlledMediaCloser(() => null, vi.fn(), 20, () => retained)
    expect(await close('child_requested')).toEqual({ closed: false })
    retained = false
    expect(await close('child_requested')).toEqual({ closed: true })
  })
})
