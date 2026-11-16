import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceInput } from './QwenRealtimeVoiceInput'
import type { QwenRealtimeSessionCallbacks, QwenRealtimeToolResult } from './qwenRealtimeVoice'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'

const mock = vi.hoisted(() => ({
  callbacks: [] as QwenRealtimeSessionCallbacks[],
  listeners: new Set<(state: string) => void>(), voice: 'idle', localOnly: true,
  gates: vi.fn(), stop: vi.fn(), speak: vi.fn(),
}))
vi.mock('./qwenRealtimeVoice', () => ({ QwenRealtimeVoiceSession: class {
  constructor(options: { callbacks: QwenRealtimeSessionCallbacks }) { mock.callbacks.push(options.callbacks) }
  connect = async () => { mock.callbacks.at(-1)?.onStatus({ runtime: 'qwen_omni_realtime', phase: 'listening', speechActive: false, pendingUtterances: 0, recoverable: true }) }
  close = vi.fn(); interrupt = vi.fn(); setExternalPlaybackActive = mock.gates
  takePendingInputsForReconnect = () => []
  resumePendingInputs = vi.fn()
} }))
vi.mock('./companionVoice', async importOriginal => ({
  ...await importOriginal<typeof import('./companionVoice')>(), getCompanionVoiceState: () => mock.voice,
  getCompanionPlaybackLocalOnly: () => mock.localOnly, stopCompanionSpeech: mock.stop,
  subscribeCompanionVoice: (fn: (state: string) => void) => { mock.listeners.add(fn); return () => mock.listeners.delete(fn) },
  speakCompanion: mock.speak }))
vi.mock('./api', () => ({ localApi: { releaseDeviceLease: vi.fn().mockResolvedValue({}) } }))

beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(100_000)
  mock.callbacks.length = 0; mock.listeners.clear(); mock.voice = 'idle'; mock.localOnly = true
  mock.gates.mockClear(); mock.stop.mockReset(); mock.speak.mockReset()
  resetVoiceCaptureCoordinatorForTests()
})
afterEach(() => { cleanup(); vi.useRealTimers(); resetVoiceCaptureCoordinatorForTests() })
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const mount = (onTurn = vi.fn(), onPublicSpeechInterrupt = vi.fn()) => render(
  <QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    wakeWord="小知" initialConversationActive onTurn={onTurn} onStatus={vi.fn()}
    onPublicSpeechInterrupt={onPublicSpeechInterrupt} />)

describe('public local reply and realtime input integration', () => {
  it.each(['stop', 'unmount', 'privacy', 'reconnect'])('cancels its preparing recovery playback after %s without accepting late audio receipts', async mode => {
    const result: QwenRealtimeToolResult = { responseText: '运行时的公开回复',
      onPlaybackStart: vi.fn(), onPlaybackEnd: vi.fn(), onPlaybackAbandoned: vi.fn() }
    let finish: ((value: string) => void) | undefined
    mock.speak.mockImplementation(() => new Promise<string>(resolve => { finish = resolve }))
    const mounted = mount(vi.fn().mockResolvedValue(result)); await advance(1)
    let recovery: Promise<void> | undefined
    await act(async () => {
      recovery = mock.callbacks[0].onUntooledTurn?.('帮我复习今天的错题')
      await Promise.resolve(); await Promise.resolve()
    })
    expect(mock.speak).toHaveBeenCalledOnce()
    const options = mock.speak.mock.calls[0][4] as { speechKey?: string; onPlaybackEnd: () => void }
    if (mode === 'stop') window.dispatchEvent(new Event('hsc-stop-realtime-speech'))
    if (mode === 'unmount') mounted.unmount()
    if (mode === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
    if (mode === 'reconnect') await act(async () => mock.callbacks[0].onStatus({ runtime: 'qwen_omni_realtime', phase: 'error', speechActive: false, pendingUtterances: 0, recoverable: true, detail: '连接中断' }))
    expect(options.speechKey).toBeTruthy()
    expect(mock.stop).toHaveBeenCalledWith(options.speechKey)
    expect(result.onPlaybackAbandoned).toHaveBeenCalledOnce()
    mock.speak.mock.calls[0][3]()
    options.onPlaybackEnd()
    await act(async () => { finish?.('managed'); await recovery })
    expect(result.onPlaybackStart).not.toHaveBeenCalled()
    expect(result.onPlaybackEnd).not.toHaveBeenCalled()
    expect(result.onPlaybackAbandoned).toHaveBeenCalledOnce()
  })
  it.each(['speech', 'stop', 'unmount', 'privacy', 'reconnect'])('never starts a late recovery voice after %s', async mode => {
    let finish: ((value: QwenRealtimeToolResult) => void) | undefined
    const onTurn = vi.fn(() => new Promise<QwenRealtimeToolResult>(resolve => { finish = resolve }))
    const mounted = mount(onTurn); await advance(1)
    let recovery: Promise<void> | undefined
    await act(async () => { recovery = mock.callbacks[0].onUntooledTurn?.('打开高拍仪'); await Promise.resolve() })
    const callback = mock.callbacks[0]
    if (mode === 'speech') callback.onUserSpeechStart?.()
    if (mode === 'stop') window.dispatchEvent(new Event('hsc-stop-realtime-speech'))
    if (mode === 'unmount') mounted.unmount()
    if (mode === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
    if (mode === 'reconnect') {
      await act(async () => callback.onStatus({ runtime: 'qwen_omni_realtime', phase: 'error', speechActive: false, pendingUtterances: 0, recoverable: true, detail: '连接已关闭' }))
      await advance(1001)
    }
    const abandoned = vi.fn(), end = vi.fn()
    await act(async () => {
      finish?.({ responseText: '设备已打开', onPlaybackAbandoned: abandoned, onPlaybackEnd: end })
      await recovery
    })
    expect(onTurn).toHaveBeenCalledExactlyOnceWith('打开高拍仪')
    expect(mock.speak).not.toHaveBeenCalled()
    expect(abandoned).toHaveBeenCalledOnce(); expect(end).not.toHaveBeenCalled()
  })
  it.each([true, false])('preserves protected=%s across playback notification and reconnection', async localOnly => {
    const interrupt = vi.fn()
    mount(vi.fn(), interrupt); await advance(1)
    await act(async () => {
      mock.voice = 'speaking'; mock.localOnly = localOnly
      mock.listeners.forEach(fn => fn('speaking'))
    })
    expect(mock.gates).toHaveBeenLastCalledWith(true, localOnly)
    mock.callbacks[0].onUserSpeechStart?.()
    expect(interrupt).toHaveBeenCalledTimes(localOnly ? 0 : 1)
    expect(mock.stop).toHaveBeenCalledTimes(localOnly ? 0 : 1)
    await act(async () => mock.callbacks[0].onStatus({ runtime: 'qwen_omni_realtime', phase: 'error', speechActive: false, pendingUtterances: 0, recoverable: true, detail: '连接已关闭' }))
    await advance(45_000)
    expect(mock.callbacks).toHaveLength(2)
    expect(mock.gates).toHaveBeenLastCalledWith(true, localOnly)
  })

  it.each(['cancelled', 'unavailable', 'delivered'])('settles recovery reply %s exactly once without late delivery', async mode => {
    const result: QwenRealtimeToolResult = { responseText: '这是实际运行时回复', speak: true,
      onPlaybackStart: vi.fn(), onPlaybackEnd: vi.fn(), onPlaybackAbandoned: vi.fn() }
    let finish: ((result: string) => void) | undefined
    mock.speak.mockImplementation(() => new Promise<string>(resolve => { finish = resolve }))
    mount(vi.fn().mockResolvedValue(result)); await advance(1)
    let recovery: Promise<void> | undefined
    await act(async () => {
      recovery = mock.callbacks[0].onUntooledTurn?.('帮我检查这道题')
      await Promise.resolve(); await Promise.resolve()
    })
    expect(mock.speak).toHaveBeenCalledOnce()
    const options = mock.speak.mock.calls[0][4] as { onPlaybackEnd: () => void; onNotDelivered: () => void }
    if (mode === 'unavailable') options.onNotDelivered()
    if (mode === 'delivered') options.onPlaybackEnd()
    await act(async () => { finish?.(mode === 'delivered' ? 'managed' : 'text_only'); await recovery })
    options.onPlaybackEnd(); options.onNotDelivered()
    mock.speak.mock.calls[0][3]()
    expect(result.onPlaybackEnd).toHaveBeenCalledTimes(mode === 'delivered' ? 1 : 0)
    expect(result.onPlaybackAbandoned).toHaveBeenCalledTimes(mode === 'delivered' ? 0 : 1)
    expect(result.onPlaybackStart).not.toHaveBeenCalled()
  })
})
