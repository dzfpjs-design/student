import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceInput } from './QwenRealtimeVoiceInput'
import type { QwenRealtimeSessionCallbacks } from './qwenRealtimeVoice'
import { resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'

const mock = vi.hoisted(() => ({ callbacks: [] as QwenRealtimeSessionCallbacks[], listeners: new Set<(state: string) => void>(), voice: 'idle' }))
vi.mock('./qwenRealtimeVoice', () => ({ QwenRealtimeVoiceSession: class {
  constructor(options: { callbacks: QwenRealtimeSessionCallbacks }) { mock.callbacks.push(options.callbacks) }
  connect = async () => { mock.callbacks.at(-1)?.onStatus({ runtime: 'qwen_omni_realtime', phase: 'listening', speechActive: false, pendingUtterances: 0, recoverable: true }) }
  close = vi.fn(); interrupt = vi.fn(); setExternalPlaybackActive = vi.fn()
  takePendingInputsForReconnect = () => []
  resumePendingInputs = vi.fn()
} }))
vi.mock('./companionVoice', async importOriginal => ({
  ...await importOriginal<typeof import('./companionVoice')>(), getCompanionVoiceState: () => mock.voice,
  getCompanionPlaybackLocalOnly: () => true, stopCompanionSpeech: vi.fn(),
  subscribeCompanionVoice: (fn: (state: string) => void) => { mock.listeners.add(fn); return () => mock.listeners.delete(fn) },
  speakCompanion: vi.fn() }))
vi.mock('./api', () => ({ localApi: { releaseDeviceLease: vi.fn().mockResolvedValue({}) } }))

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(100_000); mock.callbacks.length = 0; mock.listeners.clear(); mock.voice = 'idle'; resetVoiceCaptureCoordinatorForTests() })
afterEach(() => { cleanup(); vi.useRealTimers(); resetVoiceCaptureCoordinatorForTests() })
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const voice = async (state: string) => { await act(async () => { mock.voice = state; mock.listeners.forEach(fn => fn(state)) }) }

it.each(['preparing', 'speaking'])('keeps conversation active during local %s across provider reconnection and starts silence afterwards', async (state) => {
  const changed = vi.fn()
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试" wakeWord="小知" inactivitySeconds={30} initialConversationActive onTurn={vi.fn()} onStatus={vi.fn()} onConversationActiveChange={changed} />)
  await advance(1)
  await voice(state)
  await act(async () => { mock.callbacks[0].onStatus({ runtime: 'qwen_omni_realtime', phase: 'error', speechActive: false, pendingUtterances: 0, recoverable: true, detail: '连接已关闭' }) })
  await advance(45_000)
  expect(mock.callbacks.length).toBe(2)
  expect(changed).not.toHaveBeenCalledWith(false)
  await voice('idle')
  await advance(29_000)
  expect(changed).not.toHaveBeenCalledWith(false)
  await advance(2_000)
  expect(changed).toHaveBeenCalledWith(false)
})
