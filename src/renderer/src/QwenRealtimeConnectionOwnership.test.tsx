import { act, cleanup, render } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceInput as CurrentVoiceInput, QWEN_REALTIME_SESSION_REFRESH_MS } from './QwenRealtimeVoiceInput'
import { createNativeRuntimeBridge } from './nativeRuntimeBridge'
import type { QwenRealtimeSessionCallbacks, QwenRealtimeQueuedInput } from './qwenRealtimeVoice'
import { claimVoiceCapture, releaseVoiceCapture, resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import { clearVoiceCaptureContext, setVoiceQuestionCapture } from './voiceCaptureContext'

// Migration evidence: nine original assertions below target the removed
// onToolCall/onCommittedTurn/onUntooledTurn direct-dispatch protocol and are
// intentionally retained as failing tests. NativeVoiceBridgeJourney covers
// onset source, original transcript, one-time admission and retired admission;
// playback, wake gating and pending-input recovery still need native equivalents.
const mock = vi.hoisted(() => ({
  transports: [] as { callbacks: QwenRealtimeSessionCallbacks; close: ReturnType<typeof vi.fn>; pending: string[]; resumed: string[][] }[],
  register: vi.fn(), release: vi.fn(), renew: vi.fn(), diagnostic: vi.fn(), connect: vi.fn(),
}))
vi.mock('./qwenRealtimeVoice', () => ({ QwenRealtimeVoiceSession: class {
  pending: string[] = []
  resumed: string[][] = []
  close = vi.fn(() => { this.pending = [] }); interrupt = vi.fn(); setExternalPlaybackActive = vi.fn()
  syncNativeAuthority = vi.fn(); syncConversationActivation = vi.fn(); enterLocalWakeRecovery = vi.fn(() => false)
  readPlannedRefreshState = vi.fn().mockResolvedValue({ current: true, known: true, fresh: true, pending: false })
  takePendingInputsForReconnect = () => this.pending.splice(0)
  resumePendingInputs = (values: string[]) => { this.resumed.push([...values]); this.pending.push(...values) }
  constructor(public options: { callbacks: QwenRealtimeSessionCallbacks }) {
    mock.transports.push(this)
  }
  get callbacks() { return this.options.callbacks }
  async connect() {
    await mock.connect()
    await this.options.callbacks.onMicrophoneOpen?.({} as MediaStreamTrack)
    this.options.callbacks.onStatus({ runtime: 'qwen_omni_realtime', phase: 'listening', speechActive: false, pendingUtterances: 0, recoverable: true })
  }
} }))

// Exercise the current native Runtime admission path, while retaining a fake
// transport so the tests never open audio hardware or contact a cloud service.
function QwenRealtimeVoiceInput(props: ComponentProps<typeof CurrentVoiceInput>) {
  const nativeRuntime = createNativeRuntimeBridge({
    sessionId: props.sessionId, childId: props.childId, current: () => true,
    forward: async () => { throw new Error('unexpected native bridge request') },
  })
  return <CurrentVoiceInput {...props} audioAllowed nativeRuntime={nativeRuntime} />
}
vi.mock('./mediaDevices', () => ({ mediaDeviceRegistration: vi.fn().mockResolvedValue({ systemIdHash: 'synthetic', capabilities: {} }) }))
vi.mock('./api', () => ({ localApi: { registerDevice: mock.register, releaseDeviceLease: mock.release, renewDeviceLease: mock.renew } }))
vi.mock('./companionVoice', async importOriginal => ({
  ...await importOriginal<typeof import('./companionVoice')>(), getCompanionVoiceState: () => 'idle', getCompanionPlaybackLocalOnly: () => true,
  stopCompanionSpeech: vi.fn(), subscribeCompanionVoice: () => () => {}, speakCompanion: vi.fn() }))

beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(100_000); mock.transports.length = 0
  mock.register.mockReset(); mock.release.mockReset().mockResolvedValue({}); mock.renew.mockReset().mockResolvedValue({})
  mock.connect.mockReset().mockResolvedValue(undefined); mock.diagnostic.mockReset().mockResolvedValue(undefined)
  Object.defineProperty(window, 'desktop', { configurable: true, value: { recordRendererDiagnostic: mock.diagnostic } })
  resetVoiceCaptureCoordinatorForTests()
})
afterEach(() => { cleanup(); vi.useRealTimers(); resetVoiceCaptureCoordinatorForTests(); clearVoiceCaptureContext(); vi.unstubAllGlobals() })
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const status = (phase: 'error' | 'listening') => ({ runtime: 'qwen_omni_realtime' as const, phase, speechActive: false, pendingUtterances: 0, recoverable: true, detail: phase === 'error' ? '连接已关闭' : undefined })

it.each(['input_loss', 'connection', 'standby', 'busy', 'healthy_while_busy', 'privacy', 'throwing'] as const)('requests re-speaking only after an owned idle recovery: %s', async scenario => {
  mock.register.mockResolvedValue({ lease_id: 'cue-lease' })
  const cue = vi.fn(() => { if (scenario === 'throwing') throw Error('cue consumer failed') })
  const props = { active: true, sessionId: 'trial', childId: 'synthetic', childName: '测试',
    onTurn: vi.fn(), onStatus: vi.fn(), onRepeatRequired: cue,
    interactionBusy: ['busy', 'healthy_while_busy'].includes(scenario), ...(scenario === 'standby' ? { wakeWord: '小知' } : {}) }
  const view = render(<QwenRealtimeVoiceInput {...props} />)
  await advance(1); expect(cue).not.toHaveBeenCalled()
  const retired = mock.transports[0]
  await act(async () => retired.callbacks.onStatus({ ...status('error'), detail: scenario === 'connection'
    ? '连接已关闭' : '音频活动后30秒仍未取得完整转写。恢复后请重说。' }))
  expect(cue).not.toHaveBeenCalled()
  if (scenario === 'privacy') await act(async () => window.dispatchEvent(new CustomEvent('hsc-privacy-pause')))
  await advance(1001)
  if (['busy', 'healthy_while_busy'].includes(scenario)) {
    expect(cue).not.toHaveBeenCalled()
    if (scenario === 'healthy_while_busy') await act(async () => mock.transports[1].callbacks.onInputCompleted?.('已经识别的新输入', {
      localInputId: 'new-healthy-input', sessionId: 'trial', source: 'accepted_onset', capturedAt: Date.now(),
      carryoverConfirmationId: null, reference: mock.transports[1].callbacks.captureInputContext!().reference }))
    view.rerender(<QwenRealtimeVoiceInput {...props} interactionBusy={false} />)
    await advance(1001)
  }
  const expected = ['connection', 'standby', 'privacy', 'healthy_while_busy'].includes(scenario) ? 0 : 1
  expect(cue).toHaveBeenCalledTimes(expected)
  await act(async () => retired.callbacks.onStatus(status('listening')))
  if (mock.transports[1]) await act(async () => mock.transports[1].callbacks.onStatus(status('listening')))
  await advance(10_000)
  expect(cue).toHaveBeenCalledTimes(expected)
  expect(mock.transports).toHaveLength(scenario === 'privacy' ? 1 : 2)
  expect(props.onTurn).not.toHaveBeenCalled()
})

it.each(['stored', 'failed', 'timeout', 'unmounted', 'privacy'] as const)('keeps terminal originals until an owned durable handoff is confirmed: %s', async result => {
  mock.register.mockResolvedValue({ lease_id: 'terminal-handoff' })
  let settle!: (stored: boolean) => void
  const handoff = vi.fn(() => new Promise<boolean>(resolve => { settle = resolve }))
  const onStatus = vi.fn(), onTurn = vi.fn(), permanent = vi.fn()
  const view = render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={onStatus} onPermanentFailure={permanent}
    onPendingInputHandoff={handoff} />)
  await advance(1)
  const original: QwenRealtimeQueuedInput = { utterance: '原话必须先保存', capture: {
    localInputId: 'terminal-input-1', sessionId: 'trial', source: 'accepted_onset', capturedAt: Date.now(),
    reference: { child_id: 'synthetic', question_reference: null }, carryoverConfirmationId: null,
  } }
  ;(mock.transports[0].pending as unknown as QwenRealtimeQueuedInput[]).push(original)
  for (const [index, delay] of [1000, 3000, 8000].entries()) {
    await act(async () => mock.transports[index].callbacks.onStatus(status('error')))
    await advance(delay + 1)
  }
  await act(async () => mock.transports[3].callbacks.onStatus(status('error')))
  expect(handoff).toHaveBeenCalledExactlyOnceWith([original], expect.any(AbortSignal))
  expect(onStatus.mock.calls.at(-1)![0]).toMatchObject({ phase: 'error', recoverable: false, pendingUtterances: 1 })
  expect(onTurn).not.toHaveBeenCalled()
  if (result === 'unmounted') view.unmount()
  if (result === 'privacy') await act(async () => window.dispatchEvent(new Event('hsc-privacy-pause')))
  if (result === 'timeout') await advance(20_001)
  await act(async () => settle(result !== 'failed'))
  expect(onStatus.mock.calls.at(-1)![0].pendingUtterances).toBe(result === 'stored' ? 0 : 1)
  if (result === 'stored') {
    // Successful durable handoff permits a new background transport, but it
    // must not replay any input whose ownership has already been transferred.
    await advance(29_999)
    expect(mock.transports).toHaveLength(4)
    await advance(2)
    expect(mock.transports).toHaveLength(5)
    expect(mock.transports[4].resumed).toEqual([])
    expect(onTurn).not.toHaveBeenCalled()
  } else {
    await advance(300_000)
    expect(mock.transports).toHaveLength(4)
  }
  expect(permanent).toHaveBeenCalledTimes(1)
})

it('exhausts speech failures after successful handshakes, retaining pending input and never reviving through rotation', async () => {
  mock.register.mockResolvedValue({ lease_id: 'bounded-lease' })
  const permanent = vi.fn(), onTurn = vi.fn(), onStatus = vi.fn(value => {
    if (value.recoverable === false) throw Error('terminal UI notification failed')
  })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={onStatus} onPermanentFailure={permanent} />)
  await advance(1)
  mock.transports[0].pending.push('原话仍待接续')
  for (const [index, delay] of [1000, 3000, 8000].entries()) {
    await act(async () => mock.transports[index].callbacks.onStatus(status('error')))
    await advance(delay - 1)
    expect(mock.transports).toHaveLength(index + 1)
    await advance(2)
    expect(mock.transports).toHaveLength(index + 2)
    expect(mock.transports[index + 1].resumed).toEqual([['原话仍待接续']])
  }
  await act(async () => expect(() => mock.transports[3].callbacks.onStatus(status('error')))
    .toThrow('terminal UI notification failed'))
  expect(permanent).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('还有1句已识别的话尚未交给运行时'))
  expect(onStatus).toHaveBeenCalledWith(expect.objectContaining({ phase: 'error', recoverable: false, pendingUtterances: 1 }))
  expect(mock.transports[3].close).toHaveBeenCalledWith('retry_exhausted')
  expect(mock.release).toHaveBeenCalledWith('bounded-lease', 'qwen_realtime_retry_exhausted')
  const fallbackOwner = 'fallback-after-exhaustion'
  expect(claimVoiceCapture(fallbackOwner)).toBe(true)
  releaseVoiceCapture(fallbackOwner)
  await act(async () => mock.transports[3].callbacks.onStatus(status('error')))
  await advance(300_000)
  expect(mock.transports).toHaveLength(4)
  expect(permanent).toHaveBeenCalledTimes(1)
  expect(onTurn).not.toHaveBeenCalled()
})

it('restores the reconnect budget only after a newly accepted source-bound transcript, not an unbound completion', async () => {
  mock.register.mockResolvedValue({ lease_id: 'healthy-lease' })
  const accepted = vi.fn(), permanent = vi.fn()
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={vi.fn()} onStatus={vi.fn()} onInputCompleted={accepted} onPermanentFailure={permanent} />)
  await advance(1)
  await act(async () => mock.transports[0].callbacks.onStatus(status('error')))
  await advance(1001)
  const context = mock.transports[1].callbacks.captureInputContext!()
  await act(async () => mock.transports[1].callbacks.onInputCompleted?.('来源未绑定', {
    ...context, source: 'unbound', localInputId: 'unbound-input', capturedAt: Date.now(),
  }))
  await act(async () => mock.transports[1].callbacks.onInputCompleted?.('不是当前孩子的来源', {
    ...context, reference: { child_id: 'another-child' }, source: 'accepted_onset', localInputId: 'foreign-input', capturedAt: Date.now(),
  }))
  await act(async () => mock.transports[1].callbacks.onStatus(status('error')))
  await advance(1001)
  expect(mock.transports).toHaveLength(2)
  await advance(2000)
  expect(mock.transports).toHaveLength(3)
  const validContext = mock.transports[2].callbacks.captureInputContext!()
  await act(async () => mock.transports[2].callbacks.onInputCompleted?.('重新说完整的一句', {
    ...validContext, source: 'accepted_onset', localInputId: 'fresh-input', capturedAt: Date.now(),
  }))
  await act(async () => mock.transports[2].callbacks.onStatus(status('error')))
  await advance(1001)
  expect(mock.transports).toHaveLength(4)
  expect(permanent).not.toHaveBeenCalled()
})

it('passes the captured local source to Runtime instead of reading a later page', async () => {
  mock.register.mockResolvedValue({ lease_id: 'source-lease' })
  const onTurn = vi.fn().mockResolvedValue({ responseText: 'local', speak: false })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={vi.fn()} />)
  await advance(1)
  setVoiceQuestionCapture('trial', 'synthetic', { kind: 'diagnostic', binding: { prompt_id: 'original', item_version: 'v1' } })
  const context = mock.transports[0].callbacks.captureInputContext!()
  const capture = { ...context, localInputId: 'same-input', capturedAt: Date.now(), source: 'accepted_onset' as const }
  setVoiceQuestionCapture('trial', 'synthetic', { kind: 'diagnostic', binding: { prompt_id: 'new-page', item_version: 'v1' } })
  await act(async () => { await mock.transports[0].callbacks.onToolCall?.('这道题选B', capture) })
  expect(onTurn).toHaveBeenCalledExactlyOnceWith('这道题选B', capture)
  expect(capture.reference.question_reference?.binding).toMatchObject({ prompt_id: 'original' })
})

it('reports incomplete RTC delivery without acknowledging teaching or replaying the original turn', async () => {
  mock.register.mockResolvedValue({ lease_id: 'certainty-lease' })
  const end = vi.fn(), unconfirmed = vi.fn(), abandoned = vi.fn(), record = vi.fn()
  vi.stubGlobal('desktop', { recordRendererDiagnostic: record })
  const onTurn = vi.fn().mockResolvedValue({ responseText: '保留原结果', onPlaybackEnd: end,
    onPlaybackUnconfirmed: unconfirmed, onPlaybackAbandoned: abandoned })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={vi.fn()} />)
  await advance(1)
  const reply = await mock.transports[0].callbacks.onToolCall!('原问题')
  reply.onPlaybackStart?.()
  reply.onPlaybackUnconfirmed?.()
  reply.onPlaybackUnconfirmed?.()
  reply.onPlaybackEnd?.()
  reply.onPlaybackAbandoned?.()
  expect(unconfirmed).toHaveBeenCalledOnce()
  expect(end).not.toHaveBeenCalled(); expect(abandoned).not.toHaveBeenCalled()
  expect(record).toHaveBeenCalledWith(expect.objectContaining({ level: 'warn', details: expect.objectContaining({
    delivered: false, completion_verified: false, reason_code: 'QWEN_REALTIME_PLAYBACK_COMPLETION_UNVERIFIED',
  }) }))
  await advance(1000)
  expect(onTurn).toHaveBeenCalledOnce()
  await mock.transports[0].callbacks.onToolCall!('新的问题')
  expect(onTurn.mock.calls.map(([text]) => text)).toEqual(['原问题', '新的问题'])
  vi.unstubAllGlobals()
})

it('does not promote a retired reply into complete or unconfirmed delivery after connection replacement', async () => {
  mock.register.mockResolvedValue({ lease_id: 'certainty-old-lease' })
  const end = vi.fn(), started = vi.fn(), unconfirmed = vi.fn(), abandoned = vi.fn()
  const onTurn = vi.fn().mockResolvedValue({ responseText: '原结果', onPlaybackStart: started,
    onPlaybackEnd: end, onPlaybackUnconfirmed: unconfirmed, onPlaybackAbandoned: abandoned })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={vi.fn()} />)
  await advance(1)
  const reply = await mock.transports[0].callbacks.onToolCall!('原问题')
  await act(async () => mock.transports[0].callbacks.onStatus(status('error')))
  await advance(1001)
  reply.onPlaybackStart?.(); reply.onPlaybackUnconfirmed?.(); reply.onPlaybackEnd?.()
  expect(started).not.toHaveBeenCalled(); expect(unconfirmed).not.toHaveBeenCalled(); expect(end).not.toHaveBeenCalled()
  expect(abandoned).toHaveBeenCalledOnce()
})

it('uses the wake word for activation without deleting the child original ASR text', async () => {
  mock.register.mockResolvedValue({ lease_id: 'wake-source-lease' })
  const onTurn = vi.fn().mockResolvedValue({ responseText: 'local', speak: false })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    wakeWord="小知" onTurn={onTurn} onStatus={vi.fn()} />)
  await advance(1)
  const context = mock.transports[0].callbacks.captureInputContext!()
  const capture = { ...context, localInputId: 'wake-input', capturedAt: Date.now(), source: 'accepted_onset' as const }
  await act(async () => { await mock.transports[0].callbacks.onToolCall?.('小知，原题我选B。', capture) })
  expect(onTurn).toHaveBeenCalledExactlyOnceWith('小知，原题我选B。', capture)
  await act(async () => { await mock.transports[0].callbacks.onToolCall?.('为什么不是C？', { ...capture, localInputId: 'follow-up' }) })
  expect(onTurn.mock.calls[1][0]).toBe('为什么不是C？')
})

it('dispatches the original completed input directly without waiting for a supplier tool', async () => {
  mock.register.mockResolvedValue({ lease_id: 'committed-source-lease' })
  const onTurn = vi.fn().mockResolvedValue({ responseText: 'local', speak: false })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    wakeWord="小知" onTurn={onTurn} onStatus={vi.fn()} />)
  await advance(1)
  const callbacks = mock.transports[0].callbacks
  const capture = { ...callbacks.captureInputContext!(), localInputId: 'committed-input',
    capturedAt: Date.now(), source: 'accepted_onset' as const }
  callbacks.onInputCompleted?.('小知，第一句还有第二句。', capture)
  expect(callbacks.onCommittedTurn).toEqual(expect.any(Function))
  await act(async () => { await callbacks.onCommittedTurn?.('小知，第一句还有第二句。', capture) })
  expect(onTurn).toHaveBeenCalledExactlyOnceWith('小知，第一句还有第二句。', capture)
  expect(mock.transports[0].resumed).toEqual([])
})

it('direct dispatch ignores standby and wake-only inputs but accepts the next complete original', async () => {
  mock.register.mockResolvedValue({ lease_id: 'wake-direct-lease' })
  const onTurn = vi.fn().mockResolvedValue({ responseText: 'local', speak: false })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    wakeWord="小知" onTurn={onTurn} onStatus={vi.fn()} />)
  await advance(1)
  const callbacks = mock.transports[0].callbacks
  const send = async (utterance: string, id: string) => {
    const capture = { ...callbacks.captureInputContext!(), localInputId: id,
      capturedAt: Date.now(), source: 'accepted_onset' as const }
    callbacks.onInputCompleted?.(utterance, capture)
    await act(async () => { await callbacks.onCommittedTurn?.(utterance, capture) })
    return capture
  }
  await send('电视里正在说话', 'standby-direct')
  await send('小知', 'wake-direct')
  expect(onTurn).not.toHaveBeenCalled()
  const capture = await send('第二句和第一句都保留。', 'conversation-direct')
  expect(onTurn).toHaveBeenCalledExactlyOnceWith('第二句和第一句都保留。', capture)
})

it('an old direct-dispatch callback cannot run after its connection is replaced', async () => {
  mock.register.mockResolvedValue({ lease_id: 'direct-owner-lease' })
  const onTurn = vi.fn().mockResolvedValue({ responseText: 'local', speak: false })
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={vi.fn()} />)
  await advance(1)
  const old = mock.transports[0].callbacks
  await act(async () => old.onStatus(status('error')))
  await advance(1001)
  expect(mock.transports).toHaveLength(2)
  await act(async () => { await old.onCommittedTurn?.('旧连接迟到原话') })
  expect(onTurn).not.toHaveBeenCalled()
  await act(async () => { await mock.transports[1].callbacks.onCommittedTurn?.('新连接原话') })
  expect(onTurn).toHaveBeenCalledExactlyOnceWith('新连接原话')
})

it('saves final conversation ASR before tool selection but never promotes a wake-only or standby input', async () => {
  mock.register.mockResolvedValue({ lease_id: 'completed-source-lease' })
  const onTurn = vi.fn().mockResolvedValue({ responseText: 'local', speak: false }), onInputCompleted = vi.fn(), onWake = vi.fn()
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    wakeWord="小知" onTurn={onTurn} onInputCompleted={onInputCompleted} onWake={onWake} onStatus={vi.fn()} />)
  await advance(1)
  const callbacks = mock.transports[0].callbacks
  const capture = { ...callbacks.captureInputContext!(), localInputId: 'idle-input', capturedAt: Date.now(), source: 'accepted_onset' as const }
  await act(async () => { callbacks.onInputCompleted?.('这是待机闲话', capture) })
  expect(onInputCompleted).not.toHaveBeenCalled()
  const wake = { ...capture, localInputId: 'wake-only-input' }
  await act(async () => { callbacks.onInputCompleted?.('小知', wake) })
  expect(onInputCompleted).not.toHaveBeenCalled(); expect(onWake).toHaveBeenCalledTimes(1)
  const next = { ...capture, localInputId: 'next-input' }
  await act(async () => { callbacks.onInputCompleted?.('原题我选B。', next) })
  expect(onInputCompleted).toHaveBeenCalledExactlyOnceWith('原题我选B。', next)
  expect(onTurn).not.toHaveBeenCalled()
  await act(async () => {
    await callbacks.onToolCall?.('这是待机闲话', capture)
    await callbacks.onToolCall?.('小知', wake)
    await callbacks.onToolCall?.('原题我选B。', next)
  })
  expect(onTurn).toHaveBeenCalledExactlyOnceWith('原题我选B。', next)
  expect(onWake).toHaveBeenCalledTimes(1)
})

it.each(['late_success', 'late_failure'])('preserves the new connection after old microphone registration %s', async mode => {
  let resolve: ((lease: { lease_id: string }) => void) | undefined, reject: ((error: Error) => void) | undefined
  mock.register.mockImplementationOnce(() => new Promise((yes, no) => { resolve = yes; reject = no }))
    .mockResolvedValueOnce({ lease_id: 'new-lease' })
  const onStatus = vi.fn(), onTurn = vi.fn(), onTranscript = vi.fn(), failure = vi.fn()
  const mounted = render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={onStatus} onUserTranscript={onTranscript} onPermanentFailure={failure} />)
  await advance(1)
  await act(async () => mock.transports[0].callbacks.onStatus(status('error')))
  await advance(1001)
  expect(mock.transports).toHaveLength(2)
  await act(async () => {
    if (mode === 'late_success') resolve?.({ lease_id: 'old-lease' })
    else reject?.(new Error('old registration failed'))
    await Promise.resolve(); await Promise.resolve()
  })
  expect(mock.transports[1].close).not.toHaveBeenCalled()
  if (mode === 'late_success') expect(mock.release).toHaveBeenCalledWith('old-lease', expect.any(String))
  const previousStatusCount = onStatus.mock.calls.length
  await act(async () => {
    mock.transports[0].callbacks.onStatus(status('error'))
    mock.transports[0].callbacks.onUserTranscript?.('旧连接迟到的内容')
    await mock.transports[0].callbacks.onToolCall?.('旧连接迟到的动作')
  })
  expect(onStatus).toHaveBeenCalledTimes(previousStatusCount)
  expect(onTurn).not.toHaveBeenCalled(); expect(onTranscript).not.toHaveBeenCalled()
  await advance(30_000)
  expect(mock.renew).toHaveBeenCalledWith('new-lease')
  expect(mock.renew).not.toHaveBeenCalledWith('old-lease')
  expect(mock.transports).toHaveLength(2); expect(failure).not.toHaveBeenCalled()
  mounted.unmount()
  expect(mock.release).toHaveBeenCalledWith('new-lease', 'qwen_realtime_unmounted')
})

it('ignores a rejected renewal from the released connection instead of retrying the new one', async () => {
  let reject: ((error: Error) => void) | undefined
  mock.register.mockResolvedValueOnce({ lease_id: 'old-lease' }).mockResolvedValueOnce({ lease_id: 'new-lease' })
  mock.renew.mockImplementationOnce(() => new Promise((_resolve, no) => { reject = no })).mockResolvedValue({})
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    onTurn={vi.fn()} onStatus={vi.fn()} />)
  await advance(30_001)
  expect(mock.renew).toHaveBeenCalledWith('old-lease')
  await act(async () => mock.transports[0].callbacks.onStatus(status('error')))
  await advance(1001)
  expect(mock.transports).toHaveLength(2)
  const beforeLateRenewal = retryEvents()
  await act(async () => { reject?.(new Error('old renewal failed')); await Promise.resolve() })
  expect(retryEvents()).toEqual(beforeLateRenewal)
  expect(retryEvents().map(value => value.details.trigger)).toEqual(['status_error'])
  expect(mock.transports[1].close).not.toHaveBeenCalled()
  await advance(30_000)
  expect(mock.transports).toHaveLength(2)
  expect(mock.renew).toHaveBeenCalledWith('new-lease')
})

it('retains complete waiting input through two failures without replaying an already-dispatched turn', async () => {
  mock.register.mockResolvedValue({ lease_id: 'current-lease' })
  const onTurn = vi.fn().mockResolvedValue({ responseText: '', speak: false }), onStatus = vi.fn()
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={onStatus} />)
  await advance(1)
  mock.transports[0].pending.push('第二句', '第三句')
  await act(async () => mock.transports[0].callbacks.onStatus(status('error')))
  expect(onStatus.mock.calls.at(-1)![0].pendingUtterances).toBe(2)
  await advance(1001)
  expect(mock.transports[1].resumed).toEqual([['第二句', '第三句']])
  await act(async () => mock.transports[1].callbacks.onStatus(status('error')))
  await advance(3001)
  expect(mock.transports[2].resumed).toEqual([['第二句', '第三句']])
  const restored = mock.transports[2].pending.shift()!
  await act(async () => mock.transports[2].callbacks.onUntooledTurn?.(restored, 'reconnect'))
  expect(onTurn).toHaveBeenCalledExactlyOnceWith('第二句')
  await act(async () => mock.transports[2].callbacks.onStatus(status('error')))
  await advance(8001)
  expect(mock.transports[3].resumed).toEqual([['第三句']])
})

it('arms bounded reconnection and transfers waiting input even when error and recovery UI notifications throw', async () => {
  mock.register.mockResolvedValue({ lease_id: 'notification-lease' })
  const onStatus = vi.fn(value => {
    if (value.phase === 'error' || value.phase === 'recovering') throw Error('synthetic UI notification failed')
  })
  const onTurn = vi.fn()
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试"
    initialConversationActive onTurn={onTurn} onStatus={onStatus} />)
  await advance(1)
  mock.transports[0].pending.push('原话必须接续')
  // The transport failure boundary owns the exception. UI notification is
  // not allowed to prevent the independent internal recovery transaction.
  await act(async () => { expect(() => mock.transports[0].callbacks.onStatus(status('error'))).toThrow('notification failed') })
  await advance(1001)
  expect(mock.transports).toHaveLength(2)
  expect(mock.transports[0].close).toHaveBeenCalledWith('retry_scheduled')
  expect(mock.transports[1].resumed).toEqual([['原话必须接续']])
  expect(onTurn).not.toHaveBeenCalled()
})

it.each(['privacy', 'child', 'session'])('never replays pending input after %s scope cancellation', async mode => {
  mock.register.mockResolvedValue({ lease_id: 'scope-lease' })
  const onTurn = vi.fn(), props = { active: true, sessionId: 'trial', childId: 'synthetic', childName: '测试', onTurn, onStatus: vi.fn() }
  const mounted = render(<QwenRealtimeVoiceInput {...props} />)
  await advance(1)
  mock.transports[0].pending.push('不能跨作用域恢复')
  await act(async () => mock.transports[0].callbacks.onStatus(status('error')))
  await act(async () => {
    if (mode === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
    else mounted.rerender(<QwenRealtimeVoiceInput {...props} {...(mode === 'child' ? { childId: 'another' } : { sessionId: 'another' })} />)
  })
  await advance(8001)
  expect(onTurn).not.toHaveBeenCalled()
  if (mode === 'privacy') expect(mock.transports).toHaveLength(1)
  else expect(mock.transports[1].resumed).toEqual([])
})


const retryEvents = () => mock.diagnostic.mock.calls.map(([value]) => value).filter(value => value.event === 'voice.realtime.retry_scheduled')
const mountRetryObserver = () => render(<QwenRealtimeVoiceInput active sessionId="private-session-not-for-diagnostics" childId="private-child-not-for-diagnostics" childName="测试" onTurn={vi.fn()} onStatus={vi.fn()} />)
it('records one finite retry event on the real current lease-renewal rejection path', async () => {
  mock.register.mockResolvedValue({ lease_id: 'private-lease-not-for-diagnostics' })
  mock.renew.mockRejectedValueOnce(new Error('private renewal error not for diagnostics'))
  mountRetryObserver(); await advance(30_001)
  expect(mock.renew).toHaveBeenCalledWith('private-lease-not-for-diagnostics')
  expect(mock.transports[0].close).toHaveBeenCalledWith('retry_scheduled')
  expect(retryEvents()).toEqual([{ event: 'voice.realtime.retry_scheduled', level: 'warn', details: {
    trigger: 'lease_renewal', reason_code: 'QWEN_REALTIME_MICROPHONE', attempt: 1,
    retry_delay_ms: 1000, connection_generation: 1,
  } }])
  await advance(1000); expect(mock.transports).toHaveLength(2)
})


it.each(['status_error', 'connect_failed'] as const)('projects exact finite retry metadata for %s without error message or identifiers', async trigger => {
  mock.register.mockResolvedValue({ lease_id: 'private-lease-not-for-diagnostics' })
  const message = 'connection private-error-ID https://private.example child spoken words'
  if (trigger === 'connect_failed') mock.connect.mockRejectedValueOnce(new Error(message))
  mountRetryObserver(); await advance(1)
  if (trigger === 'status_error') await act(async () => mock.transports[0].callbacks.onStatus({ ...status('error'), detail: message }))
  expect(retryEvents()).toEqual([{ event: 'voice.realtime.retry_scheduled', level: 'warn', details: {
    trigger, reason_code: 'QWEN_REALTIME_CONNECTION_FAILED', attempt: 1, retry_delay_ms: 1000, connection_generation: 1,
  } }])
  expect(JSON.stringify(retryEvents())).not.toMatch(/private|spoken|https|error-ID/)
  await advance(1000); expect(mock.transports).toHaveLength(2)
})

it('does not duplicate scheduled events for repeated errors from the retired connection', async () => {
  mock.register.mockResolvedValue({ lease_id: 'duplicate-error-lease' })
  mountRetryObserver(); await advance(1)
  const retired = mock.transports[0]
  await act(async () => { retired.callbacks.onStatus(status('error')); retired.callbacks.onStatus(status('error')); retired.callbacks.onStatus(status('error')) })
  expect(retryEvents()).toHaveLength(1)
  expect(retired.close).toHaveBeenCalledTimes(1)
  await advance(1000); expect(mock.transports).toHaveLength(2)
})

it.each(['sync_throw', 'async_rejection'] as const)('diagnostic %s cannot prevent bounded reconnect or UI recovery', async mode => {
  mock.register.mockResolvedValue({ lease_id: 'diagnostic-failure-lease' })
  mock.diagnostic.mockImplementation(value => {
    if (value.event !== 'voice.realtime.retry_scheduled') return Promise.resolve()
    if (mode === 'sync_throw') throw Error('synthetic diagnostic failure')
    return Promise.reject(Error('synthetic diagnostic failure'))
  })
  const onStatus = vi.fn()
  render(<QwenRealtimeVoiceInput active sessionId="trial" childId="synthetic" childName="测试" onTurn={vi.fn()} onStatus={onStatus} />)
  await advance(1)
  for (const [index, delay] of [1000, 3000, 8000].entries()) {
    await act(async () => mock.transports[index].callbacks.onStatus(status('error')))
    expect(onStatus.mock.calls.at(-1)![0].phase).toBe('recovering')
    await advance(delay - 1); expect(mock.transports).toHaveLength(index + 1)
    await advance(1); expect(mock.transports).toHaveLength(index + 2)
  }
  expect(retryEvents()).toHaveLength(3)
  await act(async () => mock.transports[3].callbacks.onStatus(status('error')))
  expect(retryEvents()).toHaveLength(3)
  expect(mock.transports[3].close).toHaveBeenCalledWith('retry_exhausted')
  await advance(20_000); expect(mock.transports).toHaveLength(4)
})

it('reports only three actual retry timers with original 1s/3s/8s delays, attempts and failed connection generations', async () => {
  mock.register.mockResolvedValue({ lease_id: 'bounded-retry-lease' })
  mountRetryObserver(); await advance(1)
  for (const [index, delay] of [1000, 3000, 8000].entries()) {
    await act(async () => mock.transports[index].callbacks.onStatus(status('error')))
    expect(retryEvents()).toHaveLength(index + 1)
    expect(retryEvents()[index].details).toEqual({ trigger: 'status_error', reason_code: 'QWEN_REALTIME_CONNECTION_FAILED',
      attempt: index + 1, retry_delay_ms: delay, connection_generation: index * 2 + 1 })
    await advance(delay - 1); expect(mock.transports).toHaveLength(index + 1)
    await advance(1); expect(mock.transports).toHaveLength(index + 2)
  }
  await act(async () => mock.transports[3].callbacks.onStatus(status('error')))
  expect(retryEvents()).toHaveLength(3)
  expect(mock.transports[3].close).toHaveBeenCalledWith('retry_exhausted')
  await advance(20_000); expect(mock.transports).toHaveLength(4)
})


it.each(['privacy', 'unmount'] as const)('cancels an armed timer without another retry event after %s', async mode => {
  mock.register.mockResolvedValue({ lease_id: 'cancelled-retry-lease' })
  const view = mountRetryObserver(); await advance(1)
  await act(async () => mock.transports[0].callbacks.onStatus(status('error')))
  expect(retryEvents()).toHaveLength(1)
  await act(async () => {
    if (mode === 'privacy') window.dispatchEvent(new Event('hsc-privacy-pause'))
    else view.unmount()
    mock.transports[0].callbacks.onStatus(status('error'))
  })
  await advance(10_000)
  expect(mock.transports).toHaveLength(1)
  expect(retryEvents()).toHaveLength(1)
})

it('keeps genuine planned rotation distinct from retry scheduling', async () => {
  mock.register.mockResolvedValue({ lease_id: 'planned-refresh-lease' })
  mountRetryObserver(); await advance(QWEN_REALTIME_SESSION_REFRESH_MS)
  expect(mock.transports).toHaveLength(2)
  expect(mock.diagnostic.mock.calls.map(([value]) => value.event)).toContain('voice.realtime.session_rotated')
  expect(retryEvents()).toEqual([])
})
