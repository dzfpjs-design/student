import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  parseQwenToolCall,
  normalizeQwenAnswerSdp,
  qwenRealtimeInstructions,
  qwenRealtimeSessionUpdate,
  openQwenRealtimeMicrophone,
  QwenRealtimeVoiceSession,
  QWEN_REALTIME_TOOL_NAME,
} from './qwenRealtimeVoice'
import type { QwenNativeBoundRuntime, QwenNativeRuntimeContext } from './qwenNativeProtocol'
import { protectedPlaybackBarrier } from './protectedPlaybackBarrier'
import { ProtectedPlaybackControl } from './protectedPlaybackControl'

afterEach(() => { vi.useRealTimers() })

// Protocol-only tests model an acquired microphone; connect() tests replace it.
function newProtocolSession(...args: ConstructorParameters<typeof QwenRealtimeVoiceSession>): QwenRealtimeVoiceSession {
  return Object.assign(new QwenRealtimeVoiceSession(...args), {
    audioTrack: { enabled: true, getSettings: () => ({}) },
    microphoneReleased: true,
  })
}

type ExplicitTestTransport = {
  localSourceTrack: MediaStreamTrack
  audioTrack: MediaStreamTrack
  nativeSocketOpen: boolean
  nativeSocketReady: boolean
  nativeHostConnectionId: string
  nativeCatalog: QwenNativeBoundRuntime['catalog']
  nativeContext: QwenNativeRuntimeContext
  nativeModel: string
  nativeAudioDeltaSeen: boolean
  sessionReady: boolean
  microphoneReleased: boolean
  nativeSendTail: Promise<void>
  prepareNativeSession: (supplierSessionId: string) => void
  attachOwnedControlFanout: (stream: MediaStream, root: MediaStreamTrack, cloud: MediaStreamTrack) => Promise<void>
  receive: (raw: string) => void
}

async function explicitVoiceFixture(mode: 'manual' | 'hands-free' = 'manual') {
  const oldDesktop = window.desktop
  const sent: Array<{ type: string }> = []
  Object.assign(window, { desktop: {
    sendQwenNativeSocket: vi.fn(async (_host: string, raw: string) => { sent.push(JSON.parse(raw)) }),
    closeQwenNativeSocket: vi.fn(async () => undefined),
    recordRendererDiagnostic: vi.fn(async () => undefined),
  } })
  let active = false, busy = false, now = 1000
  const clock = vi.spyOn(performance, 'now').mockImplementation(() => now)
  let frameTap: ((pcm: Float32Array, rate: number) => void) | undefined
  const attach = vi.spyOn(ProtectedPlaybackControl.prototype, 'attach').mockImplementation(async function (this: ProtectedPlaybackControl, _stream, callback) {
    frameTap = (pcm, rate) => {
      this.processFrame(pcm, rate)
      callback?.(pcm, rate)
    }
  })
  const scope = { sessionId: 'explicit-session', childId: 'explicit-child' }
  const runtime: QwenNativeBoundRuntime = {
    catalog: { version: 1, protocol: 'qwen_runtime_capability_v1', capabilities: [{
      name: 'learning.prepare_request', tool_name: 'learning_prepare_request',
      description: 'Save a requested learning goal.', required_arguments: [],
      arguments_schema: { type: 'object', properties: {} },
    }] },
    loadCatalog: vi.fn(), loadContext: vi.fn(),
    admit: vi.fn(async () => ({ voice_input_id: 'canonical-explicit-input' })),
    propose: vi.fn(), readReceipt: vi.fn(), recordDialogue: vi.fn(async () => undefined),
  }
  const capture = vi.fn(), interrupt = vi.fn(), recoveryWake = vi.fn()
  const session = new QwenRealtimeVoiceSession({
    childName: 'Synthetic', inputMode: mode, fullDuplex: true, nativeRuntime: runtime,
    nativeAudioAllowed: () => true, explicitReplyBusy: () => busy,
    nativeActivation: {
      isActive: () => active, word: () => '小知',
      onWake: () => { active = true }, onExplicit: () => { active = true }, onRecoveryWake: recoveryWake,
      recognizeStandby: async (_wav, _signal, clientActionId) => ({ client_action_id: clientActionId,
        transcript: '小知', duration_ms: 2400, model_version: 'synthetic', status: 'completed', persisted: false, cloud_used: false }),
    },
    protectedPlaybackControl: { ...scope, isCurrent: () => true, getLease: () => null,
      recognize: vi.fn(), cancelExact: () => false },
    callbacks: { onStatus: vi.fn(), onExplicitVoiceCapture: capture, onExplicitVoiceInterrupt: interrupt,
      captureInputContext: () => ({ sessionId: scope.sessionId, reference: { child_id: scope.childId }, carryoverConfirmationId: null }) },
  })
  const transport = session as unknown as ExplicitTestTransport
  const root = { readyState: 'live', enabled: true, getSettings: () => ({ echoCancellation: true }), stop: vi.fn() } as unknown as MediaStreamTrack
  const cloud = { ...root, enabled: false }
  Object.assign(transport, { localSourceTrack: root, audioTrack: cloud,
    nativeSocketOpen: true, nativeSocketReady: true, nativeHostConnectionId: 'explicit-host',
    nativeCatalog: runtime.catalog, nativeContext: { version: 1, revision: 1, recent_dialogue: [], runtime_facts: {}, pending_calls: [] },
    nativeModel: 'qwen3.8-omni-flash-realtime', sessionReady: true, microphoneReleased: true,
  })
  transport.prepareNativeSession('explicit-supplier')
  await transport.attachOwnedControlFanout({} as MediaStream, root, cloud)
  const frames = (level: number, count: number) => {
    for (let i = 0; i < count; i++) {
      now += 64
      frameTap?.(new Float32Array(1024).fill(level), 16000)
    }
  }
  const count = (type: string) => sent.filter(event => event.type === type).length
  const receive = (event: object) => transport.receive(JSON.stringify(event))
  const admit = async (item: string, transcript: string) => {
    receive({ type: 'input_audio_buffer.committed', item_id: item })
    receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: item, transcript })
    await vi.waitFor(() => expect(runtime.admit).toHaveBeenCalledWith(transcript, expect.any(Object), 'explicit-host'))
    await transport.nativeSendTail
  }
  return { session, transport, scope, frames, count, capture, interrupt, recoveryWake, admit, receive,
    now: () => now, activate: () => { active = true }, setBusy: (value: boolean) => { busy = value },
    dispose: () => {
      session.close()
      clock.mockRestore()
      attach.mockRestore()
      Object.assign(window, { desktop: oldDesktop })
    },
  }
}

describe('Explicit Qwen voice turns', () => {
  it('does not upload idle microphone frames in manual mode, even after activation', async () => {
    const f = await explicitVoiceFixture()
    try {
      f.activate()
      f.frames(.04, 6); f.frames(0, 14)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.append')).toBe(0)
      expect(f.count('input_audio_buffer.commit')).toBe(0)
      expect(f.count('response.create')).toBe(0)
    } finally { f.dispose() }
  })

  it('preserves the existing local-only recovery wake even in manual mode', async () => {
    const f = await explicitVoiceFixture()
    try {
      expect(f.session.enterLocalWakeRecovery()).toBe(true)
      expect(f.session.beginExplicitVoiceTurn()).toBe(false)
      f.frames(.04, 39)
      await vi.waitFor(() => expect(f.recoveryWake).toHaveBeenCalledOnce())
      expect(f.count('input_audio_buffer.append')).toBe(0)
      expect(f.count('input_audio_buffer.commit')).toBe(0)
    } finally { f.dispose() }
  })

  it('sends queued audio and one commit after release, then uses the same response path', async () => {
    const f = await explicitVoiceFixture()
    let release!: () => void
    try {
      f.transport.nativeSendTail = new Promise(resolve => { release = resolve })
      expect(f.session.beginExplicitVoiceTurn()).toBe(true)
      expect(f.capture).toHaveBeenLastCalledWith('ready', undefined)
      f.frames(.04, 6); f.frames(0, 20); f.frames(.04, 6)
      f.session.finishExplicitVoiceTurn()
      f.session.finishExplicitVoiceTurn()
      f.frames(.04, 6); f.frames(0, 14)
      expect(f.count('input_audio_buffer.append')).toBe(0)
      release()
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.append')).toBe(32)
      expect(f.count('input_audio_buffer.commit')).toBe(1)
      await f.admit('explicit-item', '可以再讲深一点吗')
      await vi.waitFor(() => expect(f.count('response.create')).toBe(1))
    } finally { release?.(); f.dispose() }
  })

  it('holds automatic listening through an explicit reply, Tutor work and the quiet tail', async () => {
    const f = await explicitVoiceFixture()
    let token: symbol | null = null
    try {
      f.session.setVoiceInputMode('hands-free')
      expect(f.session.beginExplicitVoiceTurn()).toBe(true)
      f.frames(.04, 6)
      f.session.finishExplicitVoiceTurn()
      f.frames(.04, 6); f.frames(0, 14)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.commit')).toBe(1)
      await f.admit('explicit-item', '可以换个例子吗')
      await vi.waitFor(() => expect(f.count('response.create')).toBe(1))
      f.receive({ type: 'response.created', response: { id: 'explicit-reply' } })
      f.frames(.04, 6); f.frames(0, 14)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.commit')).toBe(1)
      f.setBusy(true)
      f.receive({ type: 'response.done', response: { id: 'explicit-reply', status: 'cancelled' } })
      f.frames(.04, 6); f.frames(0, 14)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.commit')).toBe(1)
      token = protectedPlaybackBarrier.begin(f.scope, 'tutor-tail', 1, f.now())
      expect(token).not.toBeNull()
      f.setBusy(false)
      f.frames(.04, 6); f.frames(0, 10)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.commit')).toBe(1)
      expect(protectedPlaybackBarrier.held()).toBe(true)
      f.frames(0, 6)
      expect(protectedPlaybackBarrier.held()).toBe(false)
      f.frames(.04, 6); f.frames(0, 14)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.commit')).toBe(2)
      await f.admit('automatic-item', '那金刚石呢')
      await vi.waitFor(() => expect(f.count('response.create')).toBe(2))
      f.receive({ type: 'response.created', response: { id: 'automatic-reply' } })
      f.receive({ type: 'response.done', response: { id: 'automatic-reply', status: 'cancelled' } })
      f.session.setVoiceInputMode('manual')
      f.frames(.04, 6); f.frames(0, 14)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.commit')).toBe(2)
    } finally {
      if (token) protectedPlaybackBarrier.rollback(token)
      f.dispose()
    }
  })

  it('takes over an automatic onset before any automatic PCM or clear reaches Qwen', async () => {
    const f = await explicitVoiceFixture()
    try {
      f.session.setVoiceInputMode('hands-free')
      f.frames(.04, 6)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.append')).toBe(0)
      expect(f.session.beginExplicitVoiceTurn()).toBe(true)
      f.frames(.09, 6)
      f.session.finishExplicitVoiceTurn()
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.clear')).toBe(0)
      expect(f.count('input_audio_buffer.commit')).toBe(1)
      await f.admit('takeover-item', '请说青蛙')
      await vi.waitFor(() => expect(f.count('response.create')).toBe(1))
    } finally { f.dispose() }
  })

  it('interrupts an owned spoken reply immediately, then waits to open the microphone', async () => {
    const f = await explicitVoiceFixture('hands-free')
    try {
      f.activate()
      f.frames(.04, 6); f.frames(0, 14)
      await f.transport.nativeSendTail
      await f.admit('initial-item', '先讲讲化学第一课')
      await vi.waitFor(() => expect(f.count('response.create')).toBe(1))
      f.receive({ type: 'response.created', response: { id: 'playing-reply' } })
      f.transport.nativeAudioDeltaSeen = true
      expect(f.session.beginExplicitVoiceTurn()).toBe(true)
      expect(f.interrupt).toHaveBeenCalledOnce()
      expect(f.capture).toHaveBeenLastCalledWith('waiting', undefined)
      await f.transport.nativeSendTail
      expect(f.count('response.cancel')).toBe(1)
      const appendCount = f.count('input_audio_buffer.append')
      f.frames(.04, 3)
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.append')).toBe(appendCount)
      f.frames(.04, 6)
      expect(f.capture).toHaveBeenLastCalledWith('ready', undefined)
      f.session.finishExplicitVoiceTurn()
      await f.transport.nativeSendTail
      expect(f.count('input_audio_buffer.commit')).toBe(2)
      await f.admit('interruption-item', '等一下，为什么这么说')
      await vi.waitFor(() => expect(f.count('response.create')).toBe(2))
    } finally { f.dispose() }
  })
})

describe('Qwen 3.8 realtime voice protocol', () => {
  it('preserves committed input when diagnostic IPC throws and never sends speech to diagnostics', () => {
    const oldDesktop = window.desktop
    const diagnostic = vi.fn(() => { throw Error('DIAGNOSTIC_OFFLINE') })
    Object.assign(window, { desktop: { recordRendererDiagnostic: diagnostic } })
    try {
      const completed = vi.fn()
      const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined,
        captureInputContext: () => ({ sessionId: 'public-session', reference: { child_id: 'public-child' }, carryoverConfirmationId: null }),
        onInputCompleted: completed } }) as unknown as { receive: (raw: string) => void; close: () => void }
      session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'public-item' }))
      session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'public-item', transcript: 'PRIVATE_TEST_SPEECH' }))
      expect(completed).toHaveBeenCalledOnce()
      expect(diagnostic).toHaveBeenCalled()
      expect(JSON.stringify(diagnostic.mock.calls)).not.toContain('PRIVATE_TEST_SPEECH')
      session.close()
    } finally { Object.assign(window, { desktop: oldDesktop }) }
  })
  it.each([true, false])('binds a real onset after an early ASR delta without borrowing its microphone state, previewOpen=%s', async previewOpen => {
    const context = { sessionId: 'owned-session', reference: { child_id: 'owned-child' }, carryoverConfirmationId: null }
    const completed = vi.fn(), committed = vi.fn().mockResolvedValue(undefined), preview = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined,
      captureInputContext: () => context, onInputCompleted: completed, onCommittedTurn: committed,
      onUserTranscript: preview } }) as unknown as {
      audioTrack: { enabled: boolean }; receive: (raw: string) => void; close: () => void
    }
    session.audioTrack.enabled = previewOpen
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.delta', item_id: 'early-delta', text: '暂停' }))
    expect(preview).not.toHaveBeenCalled()
    session.audioTrack.enabled = true
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'early-delta' }))
    session.audioTrack.enabled = false
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'early-delta', transcript: '暂停当前任务。' }))
    await Promise.resolve()
    expect(completed).toHaveBeenCalledExactlyOnceWith('暂停当前任务。', expect.objectContaining({
      ...context, source: 'accepted_onset', localInputId: expect.any(String), capturedAt: expect.any(Number),
    }))
    expect(committed).toHaveBeenCalledExactlyOnceWith('暂停当前任务。', completed.mock.calls[0][1])
    session.close()
  })

  it('does not let a pre-onset preview authorize a later closed-microphone onset', () => {
    const completed = vi.fn(), committed = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined,
      captureInputContext: () => ({ sessionId: 'owned-session', reference: { child_id: 'owned-child' }, carryoverConfirmationId: null }),
      onInputCompleted: completed, onCommittedTurn: committed } }) as unknown as {
      audioTrack: { enabled: boolean }; receive: (raw: string) => void; close: () => void
    }
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.delta', item_id: 'echo-preview', text: '继续' }))
    session.audioTrack.enabled = false
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'echo-preview' }))
    session.audioTrack.enabled = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'echo-preview', transcript: '继续当前任务。' }))
    expect(completed).not.toHaveBeenCalled()
    expect(committed).not.toHaveBeenCalled()
    session.close()
  })

  it('never cancels authorized audio or publishes child speech from a gated buffered input item', () => {
    const status = vi.fn(), abandoned = vi.fn(), start = vi.fn(), transcript = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: {
      onStatus: status, onUserSpeechStart: start, onUserTranscript: transcript } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      pendingPlayback: unknown; playbackAuthorized: boolean; receive: (raw: string) => void; close: () => void
    }
    session.audioTrack = { enabled: false, getSettings: () => ({}) }
    session.pendingPlayback = { responseText: '当前已授权回复', onPlaybackAbandoned: abandoned }
    session.playbackAuthorized = true
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'gated' }))
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_stopped', item_id: 'gated' }))
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.delta', item_id: 'gated', text: '扬声器回声' }))
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'gated', transcript: '继续解释一元二次方程' }))
    expect(abandoned).not.toHaveBeenCalled(); expect(start).not.toHaveBeenCalled()
    expect(transcript).not.toHaveBeenCalled(); expect(status).not.toHaveBeenCalled()
    session.close()
  })

  it('retains a real open-microphone item after gate closure but rejects a gated item after reopening', () => {
    const transcript = vi.fn(), handle = vi.fn().mockResolvedValue({ responseText: '', speak: false })
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onUserTranscript: transcript, onToolCall: handle } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      channel: { readyState: string; send: (raw: string) => void; close: () => void }
      receive: (raw: string) => void; completedTranscriptQueue: string[]; close: () => void
    }
    session.audioTrack = { enabled: true, getSettings: () => ({}) }
    session.channel = { readyState: 'open', send: () => undefined, close: () => undefined }
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'child-a' }))
    session.audioTrack.enabled = false
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'echo-b' }))
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'child-a', transcript: '帮我解释这道物理题' }))
    session.audioTrack.enabled = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'echo-b', transcript: '不请自来的讲解' }))
    expect(transcript).toHaveBeenCalledExactlyOnceWith('帮我解释这道物理题')
    expect(session.completedTranscriptQueue).toEqual([{ utterance: '帮我解释这道物理题' }])
    session.close()
  })

  it('never authorizes an anonymous transcript from a closed microphone merely because it later reopens', () => {
    const transcript = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined, onUserTranscript: transcript } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      receive: (raw: string) => void; close: () => void
    }
    session.audioTrack = { enabled: false, getSettings: () => ({}) }
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    session.audioTrack.enabled = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '扬声器回声' }))
    expect(transcript).not.toHaveBeenCalled()
    session.close()
  })

  it.each([true, false])('binds an anonymous onset to its first ASR item without leaving a stale grant, allowed=%s', allowed => {
    const transcript = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined, onUserTranscript: transcript } }) as unknown as {
      audioTrack: { enabled: boolean }; receive: (raw: string) => void; close: () => void
    }
    session.audioTrack.enabled = allowed
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.delta', item_id: 'bound', text: '预览' }))
    session.audioTrack.enabled = !allowed
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'bound', transcript: '真实话语' }))
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'unknown', transcript: '不应借用旧授权' }))
    expect(transcript.mock.calls).toEqual(allowed ? [['预览'], ['真实话语']] : [])
    session.close()
  })

  it('preserves an ID-less completion only when it has one unambiguous pending onset', () => {
    const transcript = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined, onUserTranscript: transcript } }) as unknown as {
      audioTrack: { enabled: boolean }; receive: (raw: string) => void; close: () => void
    }
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'real' }))
    session.audioTrack.enabled = false
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '关麦前已说的话' }))
    session.audioTrack.enabled = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'real', transcript: '关麦前已说的话' }))
    expect(transcript).toHaveBeenCalledExactlyOnceWith('关麦前已说的话')
    session.close()
  })

  it('rejects ambiguous anonymous onsets rather than borrowing the first permission', () => {
    const transcript = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined, onUserTranscript: transcript } }) as unknown as {
      audioTrack: { enabled: boolean }; receive: (raw: string) => void; close: () => void
    }
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    session.audioTrack.enabled = false
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    session.audioTrack.enabled = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'cannot-bind', transcript: '无法区分的来源' }))
    expect(transcript).not.toHaveBeenCalled()
    session.close()
  })

  it('deduplicates anonymous onset replay and clears bounded permissions on close', () => {
    const start = vi.fn(), transcript = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined, onUserSpeechStart: start, onUserTranscript: transcript } }) as unknown as {
      receive: (raw: string) => void; close: () => void
      inputItemPermissions: Map<string, boolean>; pendingInputIds: Set<string>; inputStartEventIds: Set<string>
      anonymousInputPermissions: boolean[]
    }
    const onset = JSON.stringify({ type: 'input_audio_buffer.speech_started', event_id: 'onset-1' })
    session.receive(onset); session.receive(onset)
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'one', transcript: '开始学习' }))
    expect(start).toHaveBeenCalledOnce(); expect(transcript).toHaveBeenCalledOnce()
    expect(session.anonymousInputPermissions).toEqual([])
    session.close()
    expect(session.inputItemPermissions.size).toBe(0); expect(session.pendingInputIds.size).toBe(0)
    expect(session.inputStartEventIds.size).toBe(0)
    session.receive(onset); expect(start).toHaveBeenCalledOnce()
  })

  it('does not treat a session without an acquired microphone as child speech', () => {
    const start = vi.fn(), transcript = vi.fn()
    const session = new QwenRealtimeVoiceSession({ childName: 'test', callbacks: {
      onStatus: () => undefined, onUserSpeechStart: start, onUserTranscript: transcript } }) as unknown as {
      receive: (raw: string) => void; close: () => void
    }
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'missing-track' }))
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'missing-track', transcript: '伪输入' }))
    expect(start).not.toHaveBeenCalled(); expect(transcript).not.toHaveBeenCalled()
    session.close()
  })
  it('does not dispatch a tool backed only by a rejected transcription', async () => {
    const handle = vi.fn(), rejected = vi.fn()
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onToolCall: handle, onInputRejected: rejected } }) as unknown as {
      audioTrack: { enabled: boolean }; receive: (raw: string) => void; close: () => void
    }
    session.audioTrack.enabled = false
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'echo' }))
    session.audioTrack.enabled = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'echo', transcript: '打开摄像头' }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'echo-response' } }))
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'echo-response', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'tool-echo', arguments: JSON.stringify({ utterance: '打开摄像头' }) }))
    await Promise.resolve(); await Promise.resolve()
    expect(handle).not.toHaveBeenCalled()
    expect(rejected.mock.calls).toEqual([['microphone_closed'], ['input_item_denied']])
    session.close()
  })

  it('preserves two identified utterances across gate closure without accepting an ambiguous completion', () => {
    const transcript = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined, onUserTranscript: transcript } }) as unknown as {
      audioTrack: { enabled: boolean }; completedTranscriptQueue: string[]; receive: (raw: string) => void; close: () => void
    }
    for (const id of ['first', 'second']) session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: id }))
    session.audioTrack.enabled = false
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '无法定位' }))
    for (const [item_id, text] of [['first', '讲解物理'], ['second', '然后记录错题']]) {
      session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id, transcript: text }))
    }
    expect(session.completedTranscriptQueue).toEqual([{ utterance: '讲解物理' }, { utterance: '然后记录错题' }])
    expect(transcript.mock.calls).toEqual([['讲解物理'], ['然后记录错题']])
    session.close()
  })

  it('bounds pending item ownership and rejects overflow without discarding accepted utterances', () => {
    const transcript = vi.fn(), rejected = vi.fn()
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined,
      onUserTranscript: transcript, onInputRejected: rejected } }) as unknown as {
      pendingInputIds: Set<string>; receive: (raw: string) => void; close: () => void
    }
    for (let index = 0; index < 65; index++) session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: `item-${index}` }))
    expect(session.pendingInputIds.size).toBe(64)
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'item-64', transcript: '越界输入' }))
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'item-0', transcript: '已接受输入' }))
    expect(transcript).toHaveBeenCalledExactlyOnceWith('已接受输入')
    expect(rejected.mock.calls).toEqual([['anonymous_input_overflow'], ['input_item_denied']])
    expect(session.pendingInputIds.size).toBe(63)
    session.close()
  })
  it('preserves queued child requests after abandoning only the old pending speech', async () => {
    const sent: Array<{ type: string; item?: { call_id: string; output: string } }> = []
    let finish: ((value: { responseText: string; onPlaybackAbandoned: () => void }) => void) | undefined
    const oldAbandoned = vi.fn()
    const onToolCall = vi.fn((utterance: string) => utterance === '打开高拍仪'
      ? new Promise<{ responseText: string; onPlaybackAbandoned: () => void }>(resolve => { finish = resolve })
      : Promise.resolve({ responseText: '已保存复习请求', speak: false }))
    const session = newProtocolSession({ childName: 'test',
      callbacks: { onStatus: () => undefined, onToolCall } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      channel: { readyState: string; send: (raw: string) => void; close: () => void }
      enqueueToolCall: (call: { callId: string; utterance: string }) => void
      receive: (raw: string) => void; close: () => void
    }
    session.channel = { readyState: 'open', send: raw => sent.push(JSON.parse(raw)), close: () => undefined }
    session.audioTrack = { enabled: true, getSettings: () => ({}) }
    session.enqueueToolCall({ callId: 'first', utterance: '打开高拍仪' })
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    session.enqueueToolCall({ callId: 'second', utterance: '明天复习今天的错题' })
    finish?.({ responseText: '高拍仪已经打开', onPlaybackAbandoned: oldAbandoned })
    await vi.waitFor(() => expect(onToolCall).toHaveBeenCalledTimes(2))
    expect(onToolCall.mock.calls.map(([utterance]) => utterance)).toEqual(['打开高拍仪', '明天复习今天的错题'])
    expect(oldAbandoned).toHaveBeenCalledOnce()
    expect(sent.filter(event => event.type === 'conversation.item.create').map(event => event.item?.call_id)).toEqual(['first', 'second'])
    expect(sent.some(event => event.type === 'response.create')).toBe(false)
    session.close()
  })

  it('does not cancel pending work from a buffered speech event while the microphone is protected', async () => {
    const sent: Array<{ type: string }> = [], abandoned = vi.fn()
    let finish: ((value: { responseText: string; onPlaybackAbandoned: () => void }) => void) | undefined
    const session = newProtocolSession({ childName: 'test',
      callbacks: { onStatus: () => undefined, onToolCall: () => new Promise(resolve => { finish = resolve }) } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      channel: { readyState: string; send: (raw: string) => void; close: () => void }
      runTool: (call: { callId: string; utterance: string }) => Promise<void>
      receive: (raw: string) => void; close: () => void
    }
    session.channel = { readyState: 'open', send: raw => sent.push(JSON.parse(raw)), close: () => undefined }
    session.audioTrack = { enabled: false, getSettings: () => ({}) }
    const pending = session.runTool({ callId: 'tool', utterance: '记录学习安排' })
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    finish?.({ responseText: '安排已保存', onPlaybackAbandoned: abandoned })
    await pending
    expect(abandoned).not.toHaveBeenCalled()
    expect(sent.some(event => event.type === 'response.create')).toBe(true)
    session.close()
    expect(abandoned).toHaveBeenCalledOnce()
  })
  it.each(['speech', 'explicit'])('does not revive or upload a pending tool reply after %s interruption', async mode => {
    const sent: Record<string, unknown>[] = [], abandoned = vi.fn(), end = vi.fn()
    let finish: ((result: { responseText: string; onPlaybackAbandoned: () => void; onPlaybackEnd: () => void }) => void) | undefined
    const session = newProtocolSession({ childName: 'test',
      callbacks: { onStatus: () => undefined, onToolCall: () => new Promise(resolve => { finish = resolve }) } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      channel: { readyState: string; send: (value: string) => void; close: () => void }
      runTool: (call: { callId: string; utterance: string }) => Promise<void>
      receive: (raw: string) => void; interrupt: () => void; close: () => void
    }
    session.audioTrack = { enabled: true, getSettings: () => ({}) }
    session.channel = { readyState: 'open', send: raw => sent.push(JSON.parse(raw)), close: () => undefined }
    const pending = session.runTool({ callId: 'old-tool', utterance: '打开高拍仪' })
    if (mode === 'speech') session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    else session.interrupt()
    finish?.({ responseText: '高拍仪已经打开', onPlaybackAbandoned: abandoned, onPlaybackEnd: end })
    await pending
    expect(abandoned).toHaveBeenCalledOnce(); expect(end).not.toHaveBeenCalled()
    expect(sent.some(event => event.type === 'response.create')).toBe(false)
    const output = sent.find(event => event.type === 'conversation.item.create') as { item: { output: string } }
    expect(JSON.parse(output.item.output)).toEqual({ response_text: 'reply_abandoned_no_remote_speech', playback_abandoned: true })
    expect(JSON.stringify(sent)).not.toContain('高拍仪已经打开')
    session.close()
  })
  it.each([
    [true, true, false, false], [true, false, false, false],
    [true, undefined, false, false], [false, true, false, false],
    [true, true, true, false], [false, true, true, false],
  ])('gates local playback with duplex=%s actualAEC=%s protected=%s', (fullDuplex, aec, localOnly, open) => {
    const speechStart = vi.fn()
    const session = newProtocolSession({ childName: 'test', fullDuplex,
      callbacks: { onStatus: () => undefined, onUserSpeechStart: speechStart } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => { echoCancellation?: boolean } }
      microphoneReleased: boolean; receive: (raw: string) => void
      setExternalPlaybackActive: (active: boolean, localOnly?: boolean) => void; close: () => void
    }
    session.audioTrack = { enabled: false, getSettings: () => ({ echoCancellation: aec }) }
    session.microphoneReleased = true
    session.setExternalPlaybackActive(true, localOnly)
    expect(session.audioTrack.enabled).toBe(open)
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    expect(speechStart).toHaveBeenCalledTimes(open ? 1 : 0)
    session.setExternalPlaybackActive(true)
    expect(session.audioTrack.enabled).toBe(false)
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    expect(speechStart).toHaveBeenCalledTimes(open ? 1 : 0)
    session.setExternalPlaybackActive(false)
    expect(session.audioTrack.enabled).toBe(true)
    session.close()
    expect(session.audioTrack).toBeNull()
  })
  it.each([
    ['all', false, true], ['remote-only', false, false],
    ['all', true, false], ['remote-only', true, false],
  ])('respects actual AEC scope %s during external playback protected=%s', (aec, localOnly, open) => {
    const start = vi.fn()
    const session = newProtocolSession({ childName: 'test', fullDuplex: true,
      callbacks: { onStatus: () => undefined, onUserSpeechStart: start } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      microphoneReleased: boolean; receive: (raw: string) => void
      setExternalPlaybackActive: (active: boolean, localOnly?: boolean) => void; close: () => void
    }
    session.audioTrack = { enabled: false, getSettings: () => ({ echoCancellation: aec }) }
    session.microphoneReleased = true
    session.setExternalPlaybackActive(true, localOnly)
    expect(session.audioTrack.enabled).toBe(open)
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'onset' }))
    expect(start).toHaveBeenCalledTimes(open ? 1 : 0)
    session.setExternalPlaybackActive(false)
    expect(session.audioTrack.enabled).toBe(true)
    session.close()
  })
  it.each(['all', 'remote-only'])('permits remote playback duplex for explicitly reported %s AEC', aec => {
    const start = vi.fn()
    const session = newProtocolSession({ childName: 'test', fullDuplex: true,
      callbacks: { onStatus: () => undefined, onUserSpeechStart: start } }) as unknown as {
      audioTrack: { enabled: boolean; getSettings: () => Record<string, unknown> }
      microphoneReleased: boolean; receive: (raw: string) => void
      setRemotePlaybackAuthorized: (allowed: boolean) => void
      setExternalPlaybackActive: (active: boolean, localOnly?: boolean) => void; close: () => void
    }
    session.audioTrack = { enabled: false, getSettings: () => ({ echoCancellation: aec }) }
    session.microphoneReleased = true
    session.setRemotePlaybackAuthorized(true)
    expect(session.audioTrack.enabled).toBe(true)
    session.setExternalPlaybackActive(true, true)
    expect(session.audioTrack.enabled).toBe(false)
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started', item_id: 'denied' }))
    session.setExternalPlaybackActive(false)
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'denied', transcript: '扬声器回声' }))
    expect(start).not.toHaveBeenCalled()
    expect(session.audioTrack.enabled).toBe(true)
    session.close()
  })
  it('never dispatches partial ASR or treats an empty model delta as a bypass', async () => {
    vi.useFakeTimers()
    const recover = vi.fn().mockResolvedValue(undefined)
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onUntooledTurn: recover } }) as unknown as { receive: (raw: string) => void; close: () => void }
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.delta', text: '删除' }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'early' } }))
    session.receive(JSON.stringify({ type: 'response.text.delta', response_id: 'early', delta: '直接回答' }))
    expect(recover).not.toHaveBeenCalled()
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '不要删除我的记录' }))
    expect(recover).toHaveBeenCalledExactlyOnceWith('不要删除我的记录')
    await Promise.resolve(); await Promise.resolve()
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'second', transcript: '连接高拍仪' }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'second-response' } }))
    session.receive(JSON.stringify({ type: 'response.text.delta', response_id: 'second-response', delta: ' ' }))
    expect(recover).toHaveBeenCalledOnce()
    session.close()
  })
  it('recovers direct model speech immediately from a committed transcript and ignores late tools', async () => {
    vi.useFakeTimers()
    const recover = vi.fn().mockResolvedValue(undefined), handle = vi.fn(), sent: string[] = []
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onToolCall: handle, onUntooledTurn: recover } }) as unknown as {
        channel: { readyState: string; send: (value: string) => void; close: () => void }; receive: (raw: string) => void; close: () => void
      }
    session.channel = { readyState: 'open', send: value => sent.push(JSON.parse(value).type), close: () => undefined }
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '请给我讲一个故事' }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'bypass' } }))
    session.receive(JSON.stringify({ type: 'response.audio_transcript.delta', response_id: 'bypass', delta: '从前' }))
    expect(recover).toHaveBeenCalledExactlyOnceWith('请给我讲一个故事')
    expect(sent).toContain('response.cancel')
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'bypass', name: QWEN_REALTIME_TOOL_NAME, call_id: 'late', arguments: '{}' }))
    await vi.advanceTimersByTimeAsync(9000)
    expect(recover).toHaveBeenCalledOnce(); expect(handle).not.toHaveBeenCalled()
    session.close()
  })
  it('cancels an active inference only once and keeps late cancellation receipts out of the next response', () => {
    const sent: string[] = []
    const session = newProtocolSession({ childName: 'test', callbacks: { onStatus: () => undefined } }) as unknown as {
      channel: { readyState: string; send: (value: string) => void }; receive: (raw: string) => void
      interrupt: () => void; responseActive: boolean
    }
    session.channel = { readyState: 'open', send: value => sent.push(JSON.parse(value).type) }
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'old' } }))
    session.interrupt(); session.interrupt()
    expect(sent.filter(type => type === 'response.cancel')).toHaveLength(1)
    expect(session.responseActive).toBe(false)
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'new' } }))
    session.receive(JSON.stringify({ type: 'response.done', response: { id: 'old', status: 'cancelled' } }))
    expect(session.responseActive).toBe(true)
    session.receive(JSON.stringify({ type: 'response.done', response: { id: 'new', status: 'completed' } }))
    expect(session.responseActive).toBe(false)
  })
  it('never turns a local-only or silent tool result into a remote playback receipt', async () => {
    const end = vi.fn()
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onToolCall: async () => ({ responseText: 'local_reply_already_delivered', speak: false, onPlaybackEnd: end }) } }) as unknown as {
        channel: { readyState: string; send: (value: string) => void }; receive: (value: string) => void
      }
    session.channel = { readyState: 'open', send: () => undefined }
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '读一下原作答' }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'local-response' } }))
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'local-response', name: QWEN_REALTIME_TOOL_NAME, call_id: 'local', arguments: '{}' }))
    await Promise.resolve(); await Promise.resolve()
    session.receive(JSON.stringify({ type: 'response.audio.done', response_id: 'local-response' }))
    expect(end).not.toHaveBeenCalled()
  })
  it.each([true, false, undefined])('uses duplex only with reported browser AEC=%s and still respects external audio ownership', async aec => {
    const end = vi.fn(); const abandoned = vi.fn()
    const session = newProtocolSession({ childName: 'test', fullDuplex: true,
      callbacks: { onStatus: () => undefined } }) as unknown as {
        audioTrack: { enabled: boolean; getSettings: () => { echoCancellation?: boolean } }
        microphoneReleased: boolean; pendingPlayback: unknown
        remoteAudio: { muted: boolean; play: () => Promise<void> }
        setRemotePlaybackAuthorized: (value: boolean) => void
        setExternalPlaybackActive: (value: boolean) => void; receive: (raw: string) => void
      }
    session.audioTrack = { enabled: false, getSettings: () => ({ echoCancellation: aec }) }
    session.remoteAudio = { muted: true, play: async () => undefined }
    session.microphoneReleased = true
    session.pendingPlayback = { responseText: 'reply', onPlaybackEnd: end, onPlaybackAbandoned: abandoned }
    session.setRemotePlaybackAuthorized(true)
    expect(session.audioTrack.enabled).toBe(aec === true)
    session.setExternalPlaybackActive(true); expect(session.audioTrack.enabled).toBe(false)
    session.setExternalPlaybackActive(false); expect(session.audioTrack.enabled).toBe(aec === true)
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))
    expect(session.remoteAudio.muted).toBe(aec === true)
    if (aec === true) expect(abandoned).toHaveBeenCalledOnce()
    else expect(abandoned).not.toHaveBeenCalled()
    expect(end).not.toHaveBeenCalled()
    expect(session.audioTrack.enabled).toBe(aec === true)
  })
  it('deduplicates retried audio items but preserves the same words in a new utterance', async () => {
    const handle = vi.fn().mockResolvedValue({ responseText: 'saved', speak: false })
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onToolCall: handle } }) as unknown as {
        channel: { readyState: string; send: (value: string) => void }; receive: (value: string) => void
      }
    session.channel = { readyState: 'open', send: () => undefined }
    const commit = (id: string) => session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', item_id: id, transcript: '开始学习' }))
    commit('audio-1'); commit('audio-1')
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'response-1' } }))
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'response-1', name: QWEN_REALTIME_TOOL_NAME, call_id: 'call-1', arguments: '{}' }))
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    commit('audio-2')
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'response-2' } }))
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'response-1', name: QWEN_REALTIME_TOOL_NAME, call_id: 'stale', arguments: '{}' }))
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'response-2', name: QWEN_REALTIME_TOOL_NAME, call_id: 'call-2', arguments: '{}' }))
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    expect(handle.mock.calls).toEqual([['开始学习'], ['开始学习']])
  })
  it('strict production tool routing never executes model-only fabricated utterances', async () => {
    const handle = vi.fn().mockResolvedValue({ responseText: 'saved', speak: false })
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onToolCall: handle } }) as unknown as {
        channel: { readyState: string; send: (value: string) => void }; receive: (value: string) => void
      }
    session.channel = { readyState: 'open', send: () => undefined }
    const tool = { type: 'response.function_call_arguments.done', response_id: 'strict-response', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'model-only', arguments: JSON.stringify({ utterance: '删除所有数据' }) }
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'strict-response' } }))
    session.receive(JSON.stringify(tool)); await Promise.resolve()
    expect(handle).not.toHaveBeenCalled()
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '讲一下这道题' }))
    await Promise.resolve(); await Promise.resolve()
    expect(handle).toHaveBeenCalledExactlyOnceWith('讲一下这道题')
    session.receive(JSON.stringify({ ...tool, call_id: 'duplicate-with-new-id' }))
    await Promise.resolve(); expect(handle).toHaveBeenCalledOnce()
  })

  it('strict routing recovers an ASR turn once even when the model never requests a tool', async () => {
    vi.useFakeTimers()
    const recover = vi.fn().mockResolvedValue(undefined)
    const handle = vi.fn()
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onToolCall: handle, onUntooledTurn: recover } }) as unknown as {
        channel: { readyState: string; send: (value: string) => void; close: () => void }; receive: (value: string) => void; close: () => void
      }
    session.channel = { readyState: 'open', send: () => undefined, close: () => undefined }
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '开始学习' }))
    await vi.advanceTimersByTimeAsync(8001)
    expect(recover).toHaveBeenCalledExactlyOnceWith('开始学习')
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'late-response' } }))
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'late-response', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'late-tool', arguments: JSON.stringify({ utterance: '开始学习' }) }))
    await vi.advanceTimersByTimeAsync(16000)
    expect(handle).not.toHaveBeenCalled(); expect(recover).toHaveBeenCalledOnce()
    session.close()
  })

  it('an interruption during audio drain abandons delivery and never later records success', async () => {
    vi.useFakeTimers()
    const end = vi.fn(), abandoned = vi.fn()
    const session = newProtocolSession({ childName: 'test', requireCommittedTranscript: true,
      callbacks: { onStatus: () => undefined, onToolCall: async () => ({ responseText: '请先列式。', onPlaybackEnd: end, onPlaybackAbandoned: abandoned }) } }) as unknown as {
        channel: { readyState: string; send: (value: string) => void; close: () => void }; receive: (value: string) => void; interrupt: () => void; close: () => void
      }
    session.channel = { readyState: 'open', send: () => undefined, close: () => undefined }
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '怎么做' }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'drain-response' } }))
    session.receive(JSON.stringify({ type: 'response.function_call_arguments.done', response_id: 'drain-response', name: QWEN_REALTIME_TOOL_NAME, call_id: 'tool', arguments: '{}' }))
    await Promise.resolve(); await Promise.resolve()
    session.receive(JSON.stringify({ type: 'response.audio.done', response_id: 'drain-response' }))
    session.interrupt()
    await vi.advanceTimersByTimeAsync(100000)
    expect(end).not.toHaveBeenCalled(); expect(abandoned).toHaveBeenCalledOnce()
    session.close()
  })
  it('configures semantic VAD, audio transcription and the single durable runtime tool', () => {
    const event = qwenRealtimeSessionUpdate('小明') as { type: string; session: Record<string, unknown> }
    expect(event.type).toBe('session.update')
    expect(event.session).toMatchObject({
      modalities: ['text', 'audio'],
      turn_detection: { type: 'semantic_vad', threshold: 0.5, silence_duration_ms: 800 },
      input_audio_transcription: { model: 'qwen3-asr-flash-realtime' },
      audio: { output: { voice: 'Tina' } },
      temperature: 0,
    })
    expect(event.session).not.toHaveProperty('tool_choice')
    const tools = event.session.tools as Array<{ function: { name: string } }>
    expect(tools).toHaveLength(1)
    expect(tools[0].function.name).toBe(QWEN_REALTIME_TOOL_NAME)
  })

  it('uses a transcription-only realtime session for deterministic production routing', () => {
    const event = qwenRealtimeSessionUpdate('小明', 'Tina', true) as { session: Record<string, unknown> }
    // The durable Runtime owns semantic admission. A second semantic filter
    // at the transcription front must not silently swallow a complete turn.
    expect(event.session.turn_detection).toMatchObject({ type: 'server_vad', threshold: 0.5, silence_duration_ms: 800 })
    expect(event.session).not.toHaveProperty('tools')
    expect(event.session.instructions).toContain('只负责语音活动检测和完整转写')
    expect(event.session.instructions).toContain('不回答问题')
  })

  it('requires all meaningful child turns to pass through the existing Agent Runtime', () => {
    const prompt = qwenRealtimeInstructions('小明')
    expect(prompt).toContain('每一个有意义的孩子话语')
    expect(prompt).toContain(QWEN_REALTIME_TOOL_NAME)
    expect(prompt).toContain('不得自己解题')
    expect(prompt).toContain('逐字播报 response_text')
    expect(prompt).toContain('键盘声')
  })

  it('uses complete tool arguments and preserves a transcript fallback', () => {
    expect(parseQwenToolCall({
      type: 'response.function_call_arguments.done',
      name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-1',
      arguments: JSON.stringify({ utterance: '我先问数学，然后再连接高拍仪' }),
    })).toEqual({ callId: 'call-1', utterance: '我先问数学，然后再连接高拍仪' })
    expect(parseQwenToolCall({
      type: 'response.function_call_arguments.done', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-2', arguments: '{bad json',
    }, '保留完整转写')).toEqual({ callId: 'call-2', utterance: '保留完整转写' })
    expect(parseQwenToolCall({
      type: 'response.function_call_arguments.done', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-3', arguments: JSON.stringify({ utterance: '模型改写后的短句' }),
    }, '孩子连续说出的完整原话')).toEqual({ callId: 'call-3', utterance: '孩子连续说出的完整原话' })
  })

  it('rejects unrelated tool events and empty utterances', () => {
    expect(parseQwenToolCall({ type: 'response.function_call_arguments.done', name: 'other', call_id: 'x', arguments: '{}' })).toBeNull()
    expect(parseQwenToolCall({ type: 'response.function_call_arguments.done', name: QWEN_REALTIME_TOOL_NAME, call_id: 'x', arguments: '{}' })).toBeNull()
  })

  it('normalizes the Answer SDP to WebRTC CRLF line endings', () => {
    expect(normalizeQwenAnswerSdp('v=0\no=- 1 1 IN IP4 127.0.0.1\nm=audio 9 UDP/TLS/RTP/SAVPF 111\n'))
      .toBe('v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n')
  })

  it('updates the session before releasing the microphone media gate', async () => {
    const sent: Array<Record<string, unknown>> = []
    const order: string[] = []
    const track = { enabled: false }
    const session = newProtocolSession({
      childName: '小明',
      callbacks: { onStatus: () => undefined, onToolCall: async () => ({ responseText: 'unused' }) },
    }) as unknown as {
      channel: { readyState: string; send: (value: string) => void }
      sender: { replaceTrack: (value: unknown) => Promise<void> }
      audioTrack: { enabled: boolean }
      microphoneReleased: boolean
      receive: (value: string) => void
    }
    session.channel = {
      readyState: 'open',
      send: (value) => { sent.push(JSON.parse(value) as Record<string, unknown>); order.push('session.update') },
    }
    session.sender = { replaceTrack: async () => { order.push('replaceTrack') } }
    session.audioTrack = track
    session.microphoneReleased = false

    session.receive(JSON.stringify({ type: 'session.created' }))
    await Promise.resolve()
    expect(order).toEqual(['session.update'])
    expect(track.enabled).toBe(false)

    session.receive(JSON.stringify({ type: 'session.updated' }))
    await Promise.resolve(); await Promise.resolve()
    expect(order).toEqual(['session.update', 'replaceTrack'])
    expect(track.enabled).toBe(true)
    expect(sent[0]).toMatchObject({ type: 'session.update' })
  })

  it('acknowledges a local-only tool result without creating remote speech', async () => {
    const sent: Array<Record<string, unknown>> = []
    const session = newProtocolSession({
      childName: '小明',
      callbacks: {
        onStatus: () => undefined,
        onToolCall: async () => ({ responseText: 'local_reply_already_delivered', speak: false }),
      },
    }) as unknown as {
      channel: { readyState: string; send: (value: string) => void }
      receive: (value: string) => void
      playbackAuthorized: boolean
    }
    session.channel = { readyState: 'open', send: (value) => sent.push(JSON.parse(value) as Record<string, unknown>) }
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'local-response' } }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'local-response', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-local', arguments: JSON.stringify({ utterance: '读一下这段本地内容' }),
    }))
    await Promise.resolve(); await Promise.resolve()
    expect(sent.map((event) => event.type)).toEqual(['conversation.item.create'])
    expect(session.playbackAuthorized).toBe(false)
  })

  it('authorizes audio only after the durable tool returns a remote-safe reply', async () => {
    const sent: Array<Record<string, unknown>> = []
    const session = newProtocolSession({
      childName: '小明',
      callbacks: { onStatus: () => undefined, onToolCall: async () => ({ responseText: '先看题目条件。' }) },
    }) as unknown as {
      channel: { readyState: string; send: (value: string) => void }
      receive: (value: string) => void
      playbackAuthorized: boolean
    }
    session.channel = { readyState: 'open', send: (value) => sent.push(JSON.parse(value) as Record<string, unknown>) }
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'agent-response' } }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'agent-response', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-agent', arguments: JSON.stringify({ utterance: '这道题怎么做' }),
    }))
    await Promise.resolve(); await Promise.resolve()
    expect(sent).toEqual([
      expect.objectContaining({ type: 'conversation.item.create' }),
      { type: 'response.create' },
    ])
    expect(session.playbackAuthorized).toBe(true)
  })

  it('queues concurrent realtime tool calls and executes every child turn exactly once in order', async () => {
    const sent: Array<Record<string, unknown>> = []
    const handled: string[] = []
    let releaseFirst!: () => void
    const firstPending = new Promise<void>((resolve) => { releaseFirst = resolve })
    const session = newProtocolSession({
      childName: '小明',
      callbacks: {
        onStatus: () => undefined,
        onToolCall: async (utterance) => {
          handled.push(utterance)
          if (utterance === '第一句话') await firstPending
          return { responseText: `已处理：${utterance}`, speak: false }
        },
      },
    }) as unknown as {
      channel: { readyState: string; send: (value: string) => void }
      receive: (value: string) => void
    }
    session.channel = { readyState: 'open', send: (value) => sent.push(JSON.parse(value) as Record<string, unknown>) }

    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'queue-first' } }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'queue-first', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-1', arguments: JSON.stringify({ utterance: '第一句话' }),
    }))
    await Promise.resolve()
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'queue-second' } }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'queue-second', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-2', arguments: JSON.stringify({ utterance: '第二句话' }),
    }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'queue-second', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-2', arguments: JSON.stringify({ utterance: '第二句话' }),
    }))
    await Promise.resolve()

    expect(handled).toEqual(['第一句话'])
    releaseFirst()
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve()

    expect(handled).toEqual(['第一句话', '第二句话'])
    expect(sent.filter((event) => event.type === 'conversation.item.create')).toHaveLength(2)
  })

  it('binds overlapping completed transcripts to tool calls in arrival order instead of reusing the latest sentence', async () => {
    const handled: string[] = []
    const session = newProtocolSession({
      childName: '小明',
      callbacks: {
        onStatus: () => undefined,
        onToolCall: async (utterance) => {
          handled.push(utterance)
          return { responseText: `已处理：${utterance}`, speak: false }
        },
      },
    }) as unknown as {
      channel: { readyState: string; send: (value: string) => void }
      receive: (value: string) => void
    }
    session.channel = { readyState: 'open', send: () => undefined }

    session.receive(JSON.stringify({
      type: 'conversation.item.input_audio_transcription.completed', transcript: '第一句话',
    }))
    session.receive(JSON.stringify({
      type: 'conversation.item.input_audio_transcription.completed', transcript: '第二句话',
    }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'original-first' } }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'original-first', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-first', arguments: JSON.stringify({ utterance: '模型错误地改成第二句话' }),
    }))
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    session.receive(JSON.stringify({ type: 'response.done', response: { id: 'original-first', status: 'completed' } }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'original-second' } }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'original-second', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'call-second', arguments: JSON.stringify({ utterance: '模型错误地重复第二句话' }),
    }))
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()

    expect(handled).toEqual(['第一句话', '第二句话'])
  })

  it('explicitly resumes the remote WebRTC audio element when playback is authorized', async () => {
    const play = vi.fn().mockResolvedValue(undefined)
    const audio = { muted: true, play }
    const track = { enabled: true }
    const session = newProtocolSession({
      childName: '小明',
      callbacks: { onStatus: () => undefined, onToolCall: async () => ({ responseText: 'unused' }) },
    }) as unknown as {
      remoteAudio: { muted: boolean; play: () => Promise<void> }
      audioTrack: { enabled: boolean }
      microphoneReleased: boolean
      setRemotePlaybackAuthorized: (authorized: boolean) => void
    }
    session.remoteAudio = audio
    session.audioTrack = track
    session.microphoneReleased = true

    session.setRemotePlaybackAuthorized(true)
    await Promise.resolve()

    expect(audio.muted).toBe(false)
    expect(play).toHaveBeenCalledOnce()
    expect(track.enabled).toBe(false)

    session.setRemotePlaybackAuthorized(false)
    expect(audio.muted).toBe(true)
    expect(track.enabled).toBe(true)

    ;(session as unknown as QwenRealtimeVoiceSession).setExternalPlaybackActive(true)
    expect(track.enabled).toBe(false)
    ;(session as unknown as QwenRealtimeVoiceSession).setExternalPlaybackActive(false)
    expect(track.enabled).toBe(true)
  })

  it('does not expose an untooled model answer to the child UI', () => {
    const assistant: string[] = []
    const errors: string[] = []
    const session = newProtocolSession({
      childName: '小明', callbacks: {
        onStatus: () => undefined, onToolCall: async () => ({ responseText: 'unused' }),
        onAssistantTranscript: (value) => assistant.push(value), onError: (value) => errors.push(value),
      },
    }) as unknown as { receive: (value: string) => void }
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'unrequested-response' } }))
    session.receive(JSON.stringify({ type: 'response.audio_transcript.delta', response_id: 'unrequested-response', delta: '现在讲一元二次方程' }))
    session.receive(JSON.stringify({ type: 'response.done', response: { id: 'unrequested-response', status: 'completed' } }))
    expect(assistant).toEqual([])
    expect(errors).toEqual([expect.stringContaining('绕过了学习运行时')])
  })

  it('recovers an untooled response through the durable runtime and gates the microphone', async () => {
    const recovered: string[] = []
    const track = { enabled: true }
    const session = newProtocolSession({
      childName: '小明', callbacks: {
        onStatus: () => undefined,
        onToolCall: async () => ({ responseText: 'unused' }),
        onUntooledTurn: async (utterance) => { recovered.push(utterance); expect(track.enabled).toBe(false) },
      },
    }) as unknown as {
      audioTrack: { enabled: boolean }
      microphoneReleased: boolean
      receive: (value: string) => void
    }
    session.audioTrack = track
    session.microphoneReleased = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '小知，你能听到吗' }))
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'recover-response' } }))
    session.receive(JSON.stringify({ type: 'response.audio_transcript.delta', response_id: 'recover-response', delta: '能听到' }))
    session.receive(JSON.stringify({ type: 'response.done', response: { id: 'recover-response', status: 'completed' } }))
    await Promise.resolve(); await Promise.resolve()

    expect(recovered).toEqual(['小知，你能听到吗'])
    expect(track.enabled).toBe(true)
  })

  it('queues every completed transcript in order while the previous turn is still processing', async () => {
    const committed: string[] = []
    const track = { enabled: true }
    let finish!: () => void
    const pending = new Promise<void>((resolve) => { finish = resolve })
    const session = newProtocolSession({
      childName: '小明', callbacks: {
        onStatus: () => undefined,
        onCommittedTurn: async (utterance) => { committed.push(utterance); await pending },
      },
    }) as unknown as {
      audioTrack: { enabled: boolean }
      microphoneReleased: boolean
      receive: (value: string) => void
    }
    session.audioTrack = track
    session.microphoneReleased = true
    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '请讲解这道题' }))
    await Promise.resolve()
    expect(committed).toEqual(['请讲解这道题'])
    expect(track.enabled).toBe(true)

    session.receive(JSON.stringify({ type: 'conversation.item.input_audio_transcription.completed', transcript: '我还有第二句没说完' }))
    await Promise.resolve()
    expect(committed).toEqual(['请讲解这道题'])

    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'ignored-response' } }))
    session.receive(JSON.stringify({
      type: 'response.function_call_arguments.done', response_id: 'ignored-response', name: QWEN_REALTIME_TOOL_NAME,
      call_id: 'should-be-ignored', arguments: JSON.stringify({ utterance: '请讲解这道题' }),
    }))
    await Promise.resolve()
    expect(committed).toEqual(['请讲解这道题'])
    finish()
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    expect(committed).toEqual(['请讲解这道题', '我还有第二句没说完'])
    expect(track.enabled).toBe(true)
  })

  it('falls back to the computer default microphone when a stale bound device cannot open', async () => {
    const defaultStream = { id: 'default-stream' } as unknown as MediaStream
    const getUserMedia = vi.fn()
      .mockRejectedValueOnce(Object.assign(new Error('bound device disappeared'), { name: 'NotFoundError' }))
      .mockResolvedValueOnce(defaultStream)

    const opened = await openQwenRealtimeMicrophone('stale-device-id', getUserMedia)

    expect(opened).toEqual({ stream: defaultStream, usedDefaultFallback: true, failureName: 'NotFoundError' })
    expect(getUserMedia).toHaveBeenNthCalledWith(1, expect.objectContaining({
      audio: expect.objectContaining({ deviceId: { exact: 'stale-device-id' } }),
    }))
    expect(getUserMedia).toHaveBeenNthCalledWith(2, expect.objectContaining({
      audio: expect.not.objectContaining({ deviceId: expect.anything() }),
    }))
  })

  it('does not mask microphone permission denial by retrying another device', async () => {
    const denied = Object.assign(new Error('permission denied'), { name: 'NotAllowedError' })
    const getUserMedia = vi.fn().mockRejectedValue(denied)

    await expect(openQwenRealtimeMicrophone('bound-device', getUserMedia)).rejects.toBe(denied)
    expect(getUserMedia).toHaveBeenCalledOnce()
  })

  it('does not mask a bridged plain-object permission denial', async () => {
    const denied = { name: 'NotAllowedError', message: 'permission denied' }
    const getUserMedia = vi.fn().mockRejectedValue(denied)

    await expect(openQwenRealtimeMicrophone('bound-device', getUserMedia)).rejects.toBe(denied)
    expect(getUserMedia).toHaveBeenCalledOnce()
  })

  it('does not cancel a completed response when the child starts the next turn', () => {
    const sent: Array<Record<string, unknown>> = []
    const session = newProtocolSession({
      childName: '小明',
      callbacks: { onStatus: () => undefined, onToolCall: async () => ({ responseText: 'unused' }) },
    }) as unknown as {
      channel: { readyState: string; send: (value: string) => void }
      receive: (value: string) => void
    }
    session.channel = { readyState: 'open', send: (value) => sent.push(JSON.parse(value) as Record<string, unknown>) }
    session.receive(JSON.stringify({ type: 'response.created', response: { id: 'finished-response' } }))
    session.receive(JSON.stringify({ type: 'response.audio_transcript.delta', response_id: 'finished-response', delta: '上一轮已经说完' }))
    session.receive(JSON.stringify({ type: 'response.done', response: { id: 'finished-response', status: 'completed' } }))
    session.receive(JSON.stringify({ type: 'input_audio_buffer.speech_started' }))

    expect(sent).toEqual([])
  })
})
