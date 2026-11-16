import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

async function ownedTransport() {
  let energy = 0, duration = 0
  let sourceOwner = 'owned-track'
  const status = vi.fn(), error = vi.fn(), completed = vi.fn(), committed = vi.fn().mockResolvedValue(undefined)
  const track = { id: 'owned-track', enabled: false, readyState: 'live', getSettings: () => ({}), stop: vi.fn() }
  const peer = { connectionState: 'connected', close: vi.fn(), getStats: vi.fn(async (): Promise<Map<string, Record<string, unknown>>> => {
    duration += 1
    return new Map([['source', { id: 'source', type: 'media-source', kind: 'audio', trackIdentifier: sourceOwner,
      totalAudioEnergy: energy, totalSamplesDuration: duration }]])
  }) }
  const sender = { track, replaceTrack: vi.fn().mockResolvedValue(undefined) }
  const session = Object.assign(new QwenRealtimeVoiceSession({ childName: 'public test', callbacks: {
    onStatus: status, onError: error, onInputCompleted: completed, onCommittedTurn: committed,
    captureInputContext: () => ({ sessionId: 'public-owned-session', reference: { child_id: 'public-child' }, carryoverConfirmationId: null }),
  } }), { peer, sender, audioTrack: track, stream: { getTracks: () => [track] } }) as unknown as Pick<QwenRealtimeVoiceSession, 'close' | 'setExternalPlaybackActive'> & {
    receive(raw: string): void
  }
  session.receive(JSON.stringify({ type: 'session.updated' }))
  await Promise.resolve(); await Promise.resolve()
  return { session, track, peer, status, error, completed, committed,
    energy: (value: number) => { energy = value }, owner: (value: string) => { sourceOwner = value } }
}

it('retires an owned connected stream after unconfirmed audio activity without inventing words or child speech', async () => {
  const t = await ownedTransport()
  await vi.advanceTimersByTimeAsync(1000)
  t.energy(0.1)
  // The first positive observation now arrives at the next500ms normal poll.
  await vi.advanceTimersByTimeAsync(500)
  await vi.advanceTimersByTimeAsync(29_999)
  expect(t.error).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(1)
  expect(t.error).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('未取得完整转写'))
  expect(t.track.enabled).toBe(false)
  expect(t.completed).not.toHaveBeenCalled()
  expect(t.committed).not.toHaveBeenCalled()
  expect(t.status).not.toHaveBeenCalledWith(expect.objectContaining({ speechActive: true }))
  const reads = t.peer.getStats.mock.calls.length
  await vi.advanceTimersByTimeAsync(300_000)
  expect(t.error).toHaveBeenCalledTimes(1)
  expect(t.peer.getStats).toHaveBeenCalledTimes(reads)
  t.session.close()
})

it.each(['silence', 'foreign_source', 'playback_gate'] as const)('does not borrow inactive or unowned audio to fail a transport: %s', async condition => {
  const t = await ownedTransport()
  await vi.advanceTimersByTimeAsync(1000)
  if (condition !== 'silence') t.energy(0.1)
  if (condition === 'foreign_source') t.owner('other-track')
  await vi.advanceTimersByTimeAsync(1000)
  if (condition === 'playback_gate') t.session.setExternalPlaybackActive(true, true)
  await vi.advanceTimersByTimeAsync(40_000)
  t.session.setExternalPlaybackActive(false, true)
  await vi.advanceTimersByTimeAsync(40_000)
  expect(t.error).not.toHaveBeenCalled()
  expect(t.committed).not.toHaveBeenCalled()
  t.session.close()
})

it('clears the candidate when a genuinely accepted completion arrives and cannot fail from its old energy', async () => {
  const t = await ownedTransport()
  await vi.advanceTimersByTimeAsync(1000);t.energy(0.1);await vi.advanceTimersByTimeAsync(1000)
  t.session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'accepted-public-input' }))
  t.session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'accepted-public-input', transcript: '公开合成文字' }))
  await vi.advanceTimersByTimeAsync(40_000)
  expect(t.completed).toHaveBeenCalledExactlyOnceWith('公开合成文字', expect.objectContaining({ source: 'accepted_onset' }))
  expect(t.error).not.toHaveBeenCalled()
  t.session.close()
})

it('ignores a stale asynchronous stats result after close and cancels every polling timer', async () => {
  const t = await ownedTransport()
  let finish!: (value: Map<string, Record<string, unknown>>) => void
  t.peer.getStats.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as ReturnType<typeof t.peer.getStats>)
  await vi.advanceTimersByTimeAsync(1000)
  t.session.close()
  const before = t.status.mock.calls.length
  finish(new Map([['source', { id: 'source', type: 'media-source', kind: 'audio', trackIdentifier: 'owned-track', totalAudioEnergy: 1, totalSamplesDuration: 1 }]]))
  await vi.advanceTimersByTimeAsync(40_000)
  expect(t.status).toHaveBeenCalledTimes(before)
  expect(t.error).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
})
