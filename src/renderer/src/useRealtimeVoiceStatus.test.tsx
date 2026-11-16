// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'
import type { RealtimeVoiceStatus } from './realtimeVoiceRuntime'
import { useRealtimeVoiceStatus } from './useRealtimeVoiceStatus'

afterEach(() => { cleanup(); vi.useRealTimers() })

it('keeps continuous native speaking observations alive without redrawing the workspace', () => {
  vi.useFakeTimers()
  let update!: (status: RealtimeVoiceStatus) => void
  let renders = 0
  const activity = vi.fn()
  function Workspace() {
    renders++
    const voice = useRealtimeVoiceStatus()
    update = voice.update
    return <span>{voice.status.phase}:{voice.status.pendingUtterances}</span>
  }
  render(<Workspace />)
  const session = new QwenRealtimeVoiceSession({ childName: 'synthetic',
    nativeAudioAllowed: () => true, callbacks: { onStatus: status => { activity(status); update(status) } } })
  const internal = session as any
  internal.activeResponseId = 'synthetic-response'
  internal.playbackAuthorized = true
  internal.nativeConversation = { ownsResponse: () => true }
  internal.remotePlaybackObservation = { arm: (observed: () => void) => observed(), disarm: () => {} }
  internal.syncMicrophoneGate = () => {}
  act(() => internal.watchNativeActivity())
  expect(screen.getByText('speaking:0')).toBeTruthy()
  const speakingRenders = renders
  for (let i = 0; i < 5; i++) act(() => vi.advanceTimersByTime(200))
  expect(activity).toHaveBeenCalledTimes(6)
  expect(renders).toBe(speakingRenders)
  act(() => internal.publish('error', false, 'synthetic error'))
  expect(screen.getByText('error:0')).toBeTruthy()
  expect(renders).toBe(speakingRenders + 1)
  internal.stopNativeActivityWatch()
})

it('renders every meaningful status-field change and preserves the latest callback reference', () => {
  let update!: (status: RealtimeVoiceStatus) => void
  let current!: { current: RealtimeVoiceStatus }
  let renders = 0
  function Workspace() {
    renders++
    const voice = useRealtimeVoiceStatus()
    update = voice.update; current = voice.current
    return <span>{JSON.stringify(voice.status)}</span>
  }
  render(<Workspace />)
  let next: RealtimeVoiceStatus = { runtime: 'qwen_omni_realtime', phase: 'speaking', speechActive: false,
    capturePending: false, captureReady: false, pendingUtterances: 0, recoverable: true, detail: 'first' }
  act(() => update(next))
  const changes: Partial<RealtimeVoiceStatus>[] = [
    { runtime: 'pipecat' }, { phase: 'listening' }, { speechActive: true }, { capturePending: true },
    { captureReady: true }, { pendingUtterances: 1 }, { recoverable: false }, { detail: 'changed' },
  ]
  for (const change of changes) {
    const before = renders
    next = { ...next, ...change }
    act(() => update(next))
    expect(renders).toBe(before + 1)
    expect(current.current).toBe(next)
    expect(screen.getByText(JSON.stringify(next))).toBeTruthy()
  }
  const before = renders
  const equivalent = { ...next }
  act(() => update(equivalent))
  expect(current.current).toBe(equivalent)
  expect(renders).toBe(before)
})
