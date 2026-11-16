import { afterEach, expect, it, vi } from 'vitest'
import { SpeechArbiter, type SpeechAdmission } from './speechArbiter'

afterEach(() => vi.useRealTimers())
function fixture() {
  vi.useFakeTimers()
  const stop = vi.fn(), heard: string[] = [], finish: Record<string, () => void> = {}, settled = vi.fn()
  const arbiter = new SpeechArbiter(stop)
  const say = (name: string, options: Partial<SpeechAdmission> = {}) => arbiter.submit({ scope: 'child-a',
    play: done => { heard.push(name); finish[name] = done }, settled, ...options })
  return { arbiter, stop, heard, finish, settled, say }
}

it('keyed cancellation removes only that queued request and preserves the active lesson', () => {
  const f = fixture()
  f.say('lesson', { key: 'lesson' }); f.say('pet', { key: 'pet' }); f.say('next', { key: 'next' })
  f.arbiter.cancel('pet'); f.arbiter.cancel('unknown')
  expect(f.stop).not.toHaveBeenCalled()
  expect(f.settled.mock.calls.map(call => call[0])).toEqual(['cancelled'])
  f.finish.lesson()
  expect(f.heard).toEqual(['lesson', 'next'])
  f.finish.next()
})

it('keyed cancellation stops only its active owner and ignores its late completion', () => {
  const f = fixture()
  f.say('pet', { key: 'pet' }); f.say('next', { key: 'next' })
  f.arbiter.cancel('pet')
  expect(f.stop).toHaveBeenCalledTimes(1)
  expect(f.heard).toEqual(['pet', 'next'])
  f.finish.pet(); f.arbiter.cancel('pet')
  expect(f.stop).toHaveBeenCalledTimes(1)
  expect(f.settled.mock.calls.map(call => call[0])).toEqual(['cancelled'])
  f.finish.next()
})

it('keyed cancellation abandons only its source read without stopping another output', async () => {
  const f = fixture()
  let resolve!: (allowed: boolean) => void
  let signal!: AbortSignal
  f.say('pet', { key: 'pet', beforePlay: value => { signal = value; return new Promise(done => { resolve = done }) } })
  await vi.advanceTimersByTimeAsync(0)
  f.say('next', { key: 'next' })
  f.arbiter.cancel('pet')
  expect(signal.aborted).toBe(true)
  expect(f.stop).not.toHaveBeenCalled()
  expect(f.heard).toEqual(['next'])
  resolve(true); await vi.advanceTimersByTimeAsync(0)
  expect(f.heard).toEqual(['next'])
  f.finish.next()
})

it('serializes actual playback, priority then FIFO; reminders do not interrupt a lesson', () => {
  const f = fixture()
  f.say('lesson', { priority: 'lesson' }); f.say('camera', { priority: 'reminder' }); f.say('reply-one'); f.say('reply-two')
  expect(f.heard).toEqual(['lesson'])
  f.finish.lesson(); expect(f.heard).toEqual(['lesson', 'reply-one'])
  f.finish['reply-one'](); f.finish['reply-two'](); f.finish.camera()
  expect(f.heard).toEqual(['lesson', 'reply-one', 'reply-two', 'camera'])
  expect(f.stop).not.toHaveBeenCalled()
})

it('expires stale guidance and drops superseded queued source messages', async () => {
  const f = fixture()
  f.say('lesson'); f.say('old', { priority: 'reminder', key: 'camera' }); f.say('new', { priority: 'reminder', key: 'camera' })
  await vi.advanceTimersByTimeAsync(16_000); f.finish.lesson()
  expect(f.heard).toEqual(['lesson'])
  expect(f.settled.mock.calls.map(c => c[0])).toEqual(['superseded', 'finished', 'expired'])
})

it('explicit cancellation and a different child invalidate late callbacks and pending audio', () => {
  const f = fixture()
  f.say('old'); f.say('queued'); f.arbiter.cancel(); f.say('other', { scope: 'child-b' })
  f.finish.old(); expect(f.heard).toEqual(['old', 'other'])
  expect(f.settled).not.toHaveBeenCalledWith('finished')
  f.say('third', { scope: 'child-c' }); f.finish.other()
  expect(f.heard).toEqual(['old', 'other', 'third'])
  const stopped = f.stop.mock.calls.length
  f.say('late-invalid-old-child', { scope: 'child-a', isCurrent: () => false })
  expect(f.stop).toHaveBeenCalledTimes(stopped)
  f.finish.third()
  expect(f.settled).toHaveBeenCalledWith('finished')
})

it('only urgent speech preempts and a failed source cannot stall following messages', () => {
  const f = fixture()
  f.say('lesson', { priority: 'lesson' }); f.say('urgent', { priority: 'urgent' }); f.say('next')
  expect(f.heard).toEqual(['lesson', 'urgent']); f.finish.lesson()
  expect(f.heard).toEqual(['lesson', 'urgent']); f.finish.urgent()
  expect(f.heard.at(-1)).toBe('next')
  f.finish.next(); f.say('broken', { play: (_, fail) => fail() }); f.say('recovered')
  expect(f.heard.at(-1)).toBe('recovered')
})

it('checks source validity at playback and bounds stalled audio and backlog', async () => {
  const f = fixture(); let current = true
  f.say('stalled'); f.say('source', { isCurrent: () => current })
  for (let i = 0; i < 9; i++) f.say(`queued-${i}`)
  expect(f.settled).toHaveBeenCalledWith('full')
  current = false; await vi.advanceTimersByTimeAsync(180_000)
  expect(f.heard).toEqual(['stalled'])
  expect(f.settled).toHaveBeenCalledWith('unavailable')
  f.say('new'); expect(f.heard.at(-1)).toBe('new')
})

it('admits urgent speech even when ordinary backlog is full', () => {
  const f = fixture()
  f.say('lesson')
  for (let i = 0; i < 8; i++) f.say(`queued-${i}`)
  f.say('urgent', { priority: 'urgent' })
  expect(f.heard).toEqual(['lesson', 'urgent'])
  expect(f.settled).not.toHaveBeenCalledWith('full')
  f.finish.urgent()
  expect(f.heard.at(-1)).toBe('queued-0')
})

it('consumer and stop-adapter failures cannot strand later valid speech', () => {
  const f = fixture()
  f.say('broken-consumer', { settled: () => { throw new Error('consumer failed') } })
  f.say('next')
  expect(() => f.finish['broken-consumer']()).not.toThrow()
  expect(f.heard.at(-1)).toBe('next')
  f.stop.mockImplementation(() => { throw new Error('device failed') })
  expect(() => f.arbiter.cancel()).not.toThrow()
  f.say('recovered')
  expect(f.heard.at(-1)).toBe('recovered')
})

it('permits known long local audio but keeps a bounded failure deadline', async () => {
  const f = fixture()
  f.say('long-listening', { playbackTimeoutMs: 300_000 })
  await vi.advanceTimersByTimeAsync(180_000)
  expect(f.settled).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(120_000)
  expect(f.settled).toHaveBeenCalledExactlyOnceWith('unavailable')
  f.say('bad-unbounded', { playbackTimeoutMs: 99_000_000 })
  await vi.advanceTimersByTimeAsync(615_000)
  expect(f.settled).toHaveBeenCalledTimes(2)
})
