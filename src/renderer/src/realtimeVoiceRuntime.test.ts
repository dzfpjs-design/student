import { describe, expect, it } from 'vitest'
import { avatarModeForRealtimeVoice, idleRealtimeVoiceStatus, wakeListenerMessage, type RealtimeVoiceStatus } from './realtimeVoiceRuntime'

describe('realtime voice runtime presentation', () => {
  const settings = { wakeWord: '小知', enabled: true, inactivitySeconds: 60 }
  it.each(['idle', 'connecting', 'wake_standby', 'listening', 'transcribing', 'backpressure', 'error'] as const)(
    'privacy pause overrides stale %s capture status', (phase) => {
      expect(wakeListenerMessage({ ...idleRealtimeVoiceStatus, phase }, settings, true)).toBe('语音已暂停，当前不监听唤醒词')
    })
  it('only reports standby when both saved settings and the runtime support it', () => {
    expect(wakeListenerMessage({ ...idleRealtimeVoiceStatus, phase: 'wake_standby' }, settings, false)).toBe('待机中，说“小知”唤醒')
    expect(wakeListenerMessage({ ...idleRealtimeVoiceStatus, phase: 'wake_standby' }, { ...settings, enabled: false }, false)).toBe('唤醒词待机未启用')
  })
  it('shows the avatar listening while capture remains open even with queued recognition', () => {
    const status: RealtimeVoiceStatus = { runtime: 'segmented_local', phase: 'listening', pendingUtterances: 2, recoverable: true }
    expect(avatarModeForRealtimeVoice(status, true)).toBe('listening')
  })

  it('shows work and failure states truthfully', () => {
    expect(avatarModeForRealtimeVoice({ ...idleRealtimeVoiceStatus, phase: 'wake_standby' }, false)).toBe('waiting')
    expect(avatarModeForRealtimeVoice({ ...idleRealtimeVoiceStatus, phase: 'backpressure', pendingUtterances: 8 }, false)).toBe('thinking')
    expect(avatarModeForRealtimeVoice({ ...idleRealtimeVoiceStatus, phase: 'error', detail: 'microphone unavailable' }, false)).toBe('error')
    expect(avatarModeForRealtimeVoice(idleRealtimeVoiceStatus, true)).toBe('thinking')
  })
})
