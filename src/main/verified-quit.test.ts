import { describe, expect, it, vi } from 'vitest'
import { createVerifiedQuit } from './verified-quit'

describe('application quit keeps service alive until external close is confirmed', () => {
  it('waits for both real closure receipts and coalesces repeated quit requests', async () => {
    let release!: (result: { closed: boolean }) => void
    const game = vi.fn(() => new Promise<{ closed: boolean }>(resolve => { release = resolve }))
    const media = vi.fn(async () => ({ closed: true })), confirmed = vi.fn(async () => {}), blocked = vi.fn(async () => {})
    const quit = createVerifiedQuit([game, media], confirmed, blocked)
    const first = quit(), repeated = quit()
    await Promise.resolve(); expect(confirmed).not.toHaveBeenCalled()
    release({ closed: true }); await first; await repeated
    expect(game).toHaveBeenCalledOnce(); expect(media).toHaveBeenCalledOnce()
    expect(confirmed).toHaveBeenCalledOnce(); expect(blocked).not.toHaveBeenCalled()
  })
  it.each([false, undefined, 'true', 1])('never shuts down the backend for unconfirmed closed=%s', async closed => {
    const confirmed = vi.fn(async () => {}), blocked = vi.fn(async () => {})
    await createVerifiedQuit([async () => ({ closed }), async () => ({ closed: true })], confirmed, blocked)()
    expect(confirmed).not.toHaveBeenCalled(); expect(blocked).toHaveBeenCalledOnce()
  })
  it('turns a thrown close failure into a blocked quit and permits explicit retry', async () => {
    const game = vi.fn().mockRejectedValueOnce(new Error('lost OS receipt')).mockResolvedValue({ closed: true })
    const confirmed = vi.fn(async () => {}), blocked = vi.fn(async () => {})
    const quit = createVerifiedQuit([game], confirmed, blocked)
    await quit(); expect(confirmed).not.toHaveBeenCalled(); expect(blocked).toHaveBeenCalledOnce()
    await quit(); expect(game).toHaveBeenCalledTimes(2); expect(confirmed).toHaveBeenCalledOnce()
  })
  it('does not auto-retry or replace an unresolved blocked acknowledgement', async () => {
    let release!: () => void
    const blocked = vi.fn(() => new Promise<void>(resolve => { release = resolve }))
    const close = vi.fn(async () => ({ closed: false })), confirmed = vi.fn(async () => {})
    const quit = createVerifiedQuit([close], confirmed, blocked), first = quit()
    await vi.waitFor(() => expect(blocked).toHaveBeenCalledOnce())
    const second = quit(); expect(close).toHaveBeenCalledOnce()
    release(); await first; await second
    expect(confirmed).not.toHaveBeenCalled()
  })
})
