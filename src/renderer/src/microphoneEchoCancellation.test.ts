import { afterEach, expect, it, vi } from 'vitest'
import { microphoneEchoAllowsDuplex, microphoneEchoMode, negotiateMicrophoneEchoCancellation } from './microphoneEchoCancellation'

afterEach(() => vi.useRealTimers())
const microphone = (value: unknown, supported: unknown = [true, false]) => {
  const track = { readyState: 'live', getSettings: () => ({ echoCancellation: value }),
    getCapabilities: () => ({ echoCancellation: supported }), applyConstraints: vi.fn().mockResolvedValue(undefined) }
  return track as unknown as MediaStreamTrack & typeof track
}

it.each([
  ['all', 'all', true, true], ['remote-only', 'remote-only', true, false],
  [true, 'browser-selected', true, false], [false, 'disabled', false, false],
  [undefined, 'unknown', false, false], ['true', 'unknown', false, false], [{}, 'unknown', false, false],
])('reports actual mode %s without assuming acoustic success', (value, mode, remote, external) => {
  const track = microphone(value)
  expect(microphoneEchoMode(track)).toBe(mode)
  expect(microphoneEchoAllowsDuplex(track)).toBe(remote)
  expect(microphoneEchoAllowsDuplex(track, true)).toBe(external)
})
it('negotiates all only when explicitly advertised and reads the resulting setting', async () => {
  const track = microphone(true, [true, false, 'all', 'remote-only'])
  track.applyConstraints.mockImplementation(async () => { track.getSettings = () => ({ echoCancellation: 'all' }) })
  expect(await negotiateMicrophoneEchoCancellation(track)).toEqual({ mode: 'all', negotiation: 'accepted', acoustic_quality_verified: false })
  expect(track.applyConstraints).toHaveBeenCalledExactlyOnceWith({ echoCancellation: { exact: 'all' } })
})
it('does not relabel an accepted request as all when the driver still reports true', async () => {
  expect(await negotiateMicrophoneEchoCancellation(microphone(true, ['all']))).toMatchObject({ mode: 'browser-selected', negotiation: 'accepted' })
})
it.each([undefined, [true, false], ['remote-only'], 'all'])('keeps legacy capture usable without unsupported mode retries: %s', async supported => {
  const track = microphone(true, supported)
  expect(await negotiateMicrophoneEchoCancellation(track)).toMatchObject({ mode: 'browser-selected', negotiation: 'not_supported' })
  expect(track.applyConstraints).not.toHaveBeenCalled()
})
it('preserves the actual fallback scope after driver rejection', async () => {
  const track = microphone('remote-only', ['all', 'remote-only'])
  track.applyConstraints.mockRejectedValue(new DOMException('unsupported mode', 'OverconstrainedError'))
  expect(await negotiateMicrophoneEchoCancellation(track)).toMatchObject({ mode: 'remote-only', negotiation: 'rejected' })
})
it('bounds a hung driver request and consumes its later rejection', async () => {
  vi.useFakeTimers()
  const track = microphone(true, ['all'])
  let reject!: (reason: Error) => void
  track.applyConstraints.mockImplementation(() => new Promise((_, fail) => { reject = fail }))
  const pending = negotiateMicrophoneEchoCancellation(track)
  await vi.advanceTimersByTimeAsync(1200)
  expect(await pending).toMatchObject({ mode: 'browser-selected', negotiation: 'timed_out' })
  reject(new Error('late driver error')); await Promise.resolve()
  expect(vi.getTimerCount()).toBe(0)
})
it('never requests AEC on an ended track or treats disconnection during negotiation as success', async () => {
  const track = microphone(true, ['all']); track.readyState = 'ended'
  expect(await negotiateMicrophoneEchoCancellation(track)).toMatchObject({ negotiation: 'ended' })
  expect(track.applyConstraints).not.toHaveBeenCalled()
  track.readyState = 'live'
  track.applyConstraints.mockImplementation(async () => { track.readyState = 'ended' })
  expect(await negotiateMicrophoneEchoCancellation(track)).toMatchObject({ negotiation: 'ended' })
})
it('keeps a driver settings exception on the conservative speaker gate', async () => {
  const track = microphone(true)
  track.getSettings = () => { throw new Error('disconnected') }
  expect(microphoneEchoAllowsDuplex(track)).toBe(false)
  expect(await negotiateMicrophoneEchoCancellation(track)).toMatchObject({ mode: 'unknown', acoustic_quality_verified: false })
})
