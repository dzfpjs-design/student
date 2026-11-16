import { describe, it, expect, vi } from 'vitest'
import { DocumentVisibilityChannel } from './documentVisibilityChannel'
const context = { childId: 'child', leaseId: 'lease', epoch: 'epoch', generation: 2 }
const sample = (tickMs: number, frameCount: number) => ({ tickMs, frameCount, generation: 2,
  pageRevisionId: 'revision', qualityOk: true, pageMatches: true })

describe('visibility submission', () => {
  it('uses monotonic-derived UTC and at most one in-flight write; temporary interruption flushes', async () => {
    let tick = 0, wall = 100000
    const send = vi.fn(async (p) => ({ segment_id: p.segment_id, recorded: true }))
    const c = new DocumentVisibilityChannel(context, send, () => tick, () => wall, () => 'id')
    c.sample(sample(0, 1)); tick = 500; wall += 500; c.sample(sample(tick, 2)); c.pause()
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    c.stop()
    expect(send.mock.calls[0][0]).toMatchObject({ seq: 0, visible_ms: 500, revision_id: 'revision', lease_id: 'lease' })
  })
  it('does not retry an unknown response or emit another segment in that generation', async () => {
    let tick = 0, wall = 100000
    const send = vi.fn(async () => { throw new Error('UNKNOWN') })
    const c = new DocumentVisibilityChannel(context, send, () => tick, () => wall, () => 'id')
    c.sample(sample(0, 1)); tick = 500; wall += 500; c.sample(sample(tick, 2))
    tick = 1000; wall += 500; c.sample({ ...sample(tick, 3), qualityOk: false })
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(1))
    await Promise.resolve(); await Promise.resolve()
    tick = 1500; wall += 500; c.sample(sample(tick, 4)); tick = 2000; wall += 500; c.sample(sample(tick, 5)); c.stop()
    expect(send).toHaveBeenCalledTimes(1)
  })
  it('drops the current segment when wall time jumps', () => {
    let tick = 0, wall = 100000
    const send = vi.fn()
    const c = new DocumentVisibilityChannel(context, send, () => tick, () => wall)
    c.sample(sample(0, 1)); tick = 500; wall += 500; c.sample(sample(tick, 2))
    tick = 1000; wall += 5000; c.sample(sample(tick, 3)); c.stop()
    expect(send).not.toHaveBeenCalled()
  })
  it('discards the tail on stop and on privacy pause', () => {
    let tick = 0, wall = 100000
    const send = vi.fn()
    const c = new DocumentVisibilityChannel(context, send, () => tick, () => wall)
    c.sample(sample(0, 1)); tick = 500; wall += 500; c.sample(sample(tick, 2)); c.discard(); c.stop()
    expect(send).not.toHaveBeenCalled()
  })
  it('does not start a queued submission after stop', async () => {
    let tick = 0, wall = 100000
    const send = vi.fn()
    const c = new DocumentVisibilityChannel(context, send, () => tick, () => wall)
    c.sample(sample(0, 1)); tick = 500; wall += 500; c.sample(sample(tick, 2)); c.pause(); c.stop()
    await Promise.resolve(); await Promise.resolve()
    expect(send).not.toHaveBeenCalled()
  })
  it('drops a China midnight crossing rather than reporting it on the wrong day', () => {
    let tick = 0, wall = Date.parse('2026-11-04T15:59:59.750Z')
    const send = vi.fn()
    const c = new DocumentVisibilityChannel(context, send, () => tick, () => wall)
    c.sample(sample(0, 1)); tick = 500; wall += 500; c.sample(sample(tick, 2)); c.pause()
    expect(send).not.toHaveBeenCalled()
  })
})
