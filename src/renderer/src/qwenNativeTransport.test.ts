// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession, type QwenRealtimeInputContext, type QwenRealtimeSessionCallbacks } from './qwenRealtimeVoice'
import { protectedPlaybackBarrier } from './protectedPlaybackBarrier'
import { ProtectedPlaybackControl } from './protectedPlaybackControl'
import { qwenNativeContextInstructions, recoverQwenNativeContext, type QwenNativeBoundRuntime, type QwenNativeRuntimeContext } from './qwenNativeProtocol'

const catalog = { version: 1 as const, protocol: 'qwen_runtime_capability_v1' as const, capabilities: [{
  name: 'learning.prepare_request', tool_name: 'learning_prepare_request', description: 'Save a requested learning goal.',
  required_arguments: ['subject'], arguments_schema: { type: 'object', additionalProperties: false, required: ['subject'],
    properties: { subject: { type: 'string' } } },
}] }
const context: QwenNativeRuntimeContext = { version: 1, revision: 1, recent_dialogue: [], runtime_facts: {}, pending_calls: [] }
const sessions: any[] = []
function fixture() {
  const sent: any[] = [], closeHost = vi.fn(async () => undefined)
  vi.stubGlobal('window', { setInterval,clearInterval,setTimeout,clearTimeout,desktop: { closeQwenNativeSocket: closeHost, recordRendererDiagnostic: vi.fn(async () => undefined) } })
  let active = true, owner = true, audioAllowed=true, lease: any = null, clock = 1000
  vi.spyOn(performance, 'now').mockImplementation(() => clock)
  const runtime: QwenNativeBoundRuntime = { catalog, loadCatalog: vi.fn(async () => catalog), loadContext: vi.fn(async () => context),
    admit: vi.fn(async () => ({ voice_input_id: 'canonical-voice' })),
    propose: vi.fn<QwenNativeBoundRuntime['propose']>(async proposal => ({ version: 1, proposal_status: 'admitted', action_id: 'action-' + proposal.call_id,
      agent_turn_id: 'server-turn-' + proposal.call_id, execution: { status: 'completed', location: 'server' },
      public_result: { status: 'saved', needs_textbook: true } })),
    recordDialogue: vi.fn(async () => undefined), readReceipt: vi.fn<QwenNativeBoundRuntime['readReceipt']>(async () => ({ version: 1, proposal_status: 'replayed',
      action_id: 'original-action', execution: { status: 'completed', location: 'server' }, public_result: { status: 'saved' } })) }
  const source: QwenRealtimeInputContext = { sessionId: 'local-session', reference: { child_id: 'child', question_reference: {
    kind: 'diagnostic', binding: { prompt_id: 'old-question', item_version: 'old-version' } } }, carryoverConfirmationId: null }
  const callbacks = { onStatus: vi.fn(), onInputCompleted: vi.fn(), onUserTranscript: vi.fn(), onAssistantTranscript: vi.fn(),
    onCurriculumTutor: vi.fn<NonNullable<QwenRealtimeSessionCallbacks['onCurriculumTutor']>>(() => true),
    onToolCall: vi.fn(async()=>({responseText:'LEGACY_NOT_ALLOWED',speak:false})), onCommittedTurn: vi.fn(async()=>undefined),
    onUntooledTurn: vi.fn(async()=>undefined), onError: vi.fn(), captureInputContext: () => source } satisfies QwenRealtimeSessionCallbacks
  const session = new QwenRealtimeVoiceSession({ childName: 'Synthetic', fullDuplex: true, nativeRuntime: runtime,
    nativeAudioAllowed:()=>audioAllowed,
    nativeActivation: { isActive: () => active, word: () => '小知', onWake: vi.fn(), recognizeStandby: vi.fn() },
    protectedPlaybackControl: { sessionId: 'local-session', childId: 'child', isCurrent: () => owner,
      getLease: () => lease, recognize: vi.fn(), cancelExact: vi.fn(() => false) }, callbacks }) as any
  const root = { enabled: true, muted: false, readyState: 'live', getSettings: () => ({ echoCancellation: true }), stop: vi.fn() } as unknown as MediaStreamTrack
  const cloud:MediaStreamTrack = { ...root, enabled: false }, replaceTrack = vi.fn(async () => undefined)
  session.localSourceTrack = root; session.audioTrack = cloud
  session.peer = { connectionState: 'connected', close: vi.fn() }
  session.sender = { track: null, replaceTrack }
  session.channel = { readyState: 'open', bufferedAmount: 0, send: (raw: string) => sent.push(JSON.parse(raw)), close: vi.fn() }
  // A protocol-state fixture; actual IPC lifecycle is covered separately.
  session.nativeSocketOpen=true;session.nativeSocketReady=true;session.nativeModel='qwen3.8-omni-flash-realtime';session.send=(event:any)=>{sent.push(event);return Promise.resolve(true)}
  session.nativeHostConnectionId = 'host-current'; session.nativeCatalog = catalog; session.nativeContext = context
  sessions.push(session)
  const receive = (event: any) => {if(event.type==='session.updated')event.session={model:session.nativeModel,instructions:session.nativeExpectedInstructions,tools:sent.find(e=>e.type==='session.update')?.session.tools,...event.session};session.receive(JSON.stringify(event))}
  const frames = (value = 0.04, count = 6) => { for (let i = 0; i < count; i++) {
    clock += 64; session.protectedControl?.processFrame(new Float32Array(1024).fill(value), 16000)
    session.manualAudio.processFrame(new Float32Array(1024).fill(value), 16000)
  } }
  return { session, runtime, sent, callbacks, receive, frames, root, cloud, replaceTrack, closeHost,
    permit:(v:boolean)=>{audioAllowed=v;session.syncNativeAuthority()},
    active: (v: boolean) => { active = v; session.syncConversationActivation() },
    owner: (v: boolean) => { owner = v }, lease: (v: any) => { lease = v }, tick: (ms: number) => { clock += ms } }
}
async function open() {
  const f = fixture()
  f.receive({ type: 'session.created', session: { id: 'supplier-session' } })
  f.receive({ type: 'session.updated', session: { id: 'supplier-session', turn_detection: null } })
  await Promise.resolve(); await Promise.resolve()
  return f
}
async function admitted(f: Awaited<ReturnType<typeof open>>, item = 'input-item') {
  f.frames(); f.frames(0, 13)
  expect(f.sent.filter(e => e.type === 'input_audio_buffer.commit')).toHaveLength(1)
  f.receive({ type: 'input_audio_buffer.committed', item_id: item })
  f.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: item, transcript: '帮我复习初三地理第一课' })
  await vi.waitFor(() => expect(f.sent.filter(e => e.type === 'response.create')).toHaveLength(1))
}
afterEach(() => {
  for (const session of sessions.splice(0)) session.close()
  const token = protectedPlaybackBarrier.token(); if (token) protectedPlaybackBarrier.rollback(token)
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()
})
it('manual native production sends raw append/automatic commit, never creates a microphone RTP uplink and never dispatches completed ASR to a classifier', async () => {
  const f = await open()
  const update = f.sent.find(event => event.type === 'session.update').session
  expect(update.turn_detection).toBeNull(); expect(update.tools[0].function.name).toBe('learning_prepare_request')
  expect(JSON.stringify(update)).not.toContain('handle_child_turn')
  expect(f.replaceTrack).not.toHaveBeenCalled(); expect(f.cloud.enabled).toBe(false)
  await admitted(f)
  expect(f.runtime.admit).toHaveBeenCalledTimes(1)
  expect(f.runtime.propose).not.toHaveBeenCalled()
  expect(f.callbacks.onCommittedTurn).not.toHaveBeenCalled(); expect(f.callbacks.onToolCall).not.toHaveBeenCalled()
  expect(f.callbacks.onUntooledTurn).not.toHaveBeenCalled()
})
it('a confirmed curriculum dispatch uses the owned Tutor handoff without a concurrent supplier response',async()=>{
  const f=await open()
  const dispatch={version:1 as const,subject:'化学',action_id:'curriculum-tutor-'+'a'.repeat(64)}
  vi.mocked(f.runtime.admit).mockResolvedValueOnce({voice_input_id:'curriculum-voice',curriculum_tutor:dispatch})
  f.frames();f.frames(0,13)
  f.receive({type:'input_audio_buffer.committed',item_id:'lesson-input'})
  f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'lesson-input',transcript:'这个现象是怎么产生的？'})
  await vi.waitFor(()=>expect(f.callbacks.onCurriculumTutor).toHaveBeenCalledTimes(1))
  const [text,capture,received,current]=f.callbacks.onCurriculumTutor.mock.calls[0]
  expect([text,capture.sessionId,received]).toEqual(['这个现象是怎么产生的？','local-session',dispatch])
  expect(current()).toBe(true)
  expect(f.sent.some(e=>e.type==='response.create')).toBe(false)
  expect(f.runtime.propose).not.toHaveBeenCalled()
  f.frames();f.frames(0,13)
  f.receive({type:'input_audio_buffer.committed',item_id:'new-input'})
  f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'new-input',transcript:'换一道题'})
  await vi.waitFor(()=>expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1))
  expect(current()).toBe(false)
})
it('ordinary native audio/text is unverified dialogue, not bypass or a scoring operation', async () => {
  const f = await open(); await admitted(f)
  f.receive({ type: 'response.created', response: { id: 'native-response' } })
  expect(f.session.playbackAuthorized).toBe(true)
  f.receive({ type: 'response.audio_transcript.delta', response_id: 'native-response', delta: '你用哪一版课本？' })
  f.receive({ type: 'response.done', response: { id: 'native-response', status: 'completed' } })
  await vi.waitFor(() => expect(f.runtime.recordDialogue).toHaveBeenCalledTimes(1))
  expect(vi.mocked(f.runtime.recordDialogue).mock.calls[0][0]).toMatchObject({ host_connection_id: 'host-current',
    voice_input_id: 'canonical-voice', input_item_id: 'input-item', text: '你用哪一版课本？', status: 'unverified' })
  expect(f.runtime.propose).not.toHaveBeenCalled(); expect(f.callbacks.onUntooledTurn).not.toHaveBeenCalled()
})
it('two concrete tools share the exact persisted input and emit only true public facts before one native continuation', async () => {
  const f = await open(); await admitted(f)
  f.receive({ type: 'response.created', response: { id: 'native-response' } })
  for (const call of ['first', 'second']) f.receive({ type: 'response.function_call_arguments.done', response_id: 'native-response',
    call_id: call, name: 'learning_prepare_request', arguments: JSON.stringify({ args: { subject: 'geography' } }) })
  f.receive({ type: 'response.done', response: { id: 'native-response', status: 'completed' } })
  await vi.waitFor(() => expect(f.sent.filter(e => e.type === 'conversation.item.create')).toHaveLength(2))
  expect(f.runtime.propose).toHaveBeenCalledTimes(2)
  expect(vi.mocked(f.runtime.propose).mock.calls[1][0]).toMatchObject({ voice_input_id: 'canonical-voice',
    host_connection_id: 'host-current', previous_action_id: 'action-first', call_id: 'second' })
  expect(f.sent.filter(e => e.type === 'response.create')).toHaveLength(2)
  const result = f.sent.filter(e => e.type === 'conversation.item.create').map(e => e.item.output).join('\n')
  expect(result).toContain('needs_textbook'); expect(result).not.toContain('host-current'); expect(result).not.toContain('server-turn')
})
it('an unsolicited response cannot borrow a recent transcript, execute a tool or play audio', async () => {
  const f = await open()
  f.receive({ type: 'response.created', response: { id: 'unsolicited' } })
  f.receive({ type: 'response.function_call_arguments.done', response_id: 'unsolicited', call_id: 'no-source',
    name: 'learning_prepare_request', arguments: JSON.stringify({ args: { subject: 'geography' } }) })
  expect(f.session.playbackAuthorized).toBe(false); expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('queued speech and protected marker frames never append after the gate closes and reopens', async () => {
  const f = await open()
  f.frames(0.04); f.frames(0, 13) // First explicit commit still awaits ASR.
  f.frames(0.25); f.frames(0, 13) // Fresh second utterance is buffered, never uploaded yet.
  f.session.setExternalPlaybackActive(true, true); f.frames(0.7, 30)
  f.session.setExternalPlaybackActive(false, true)
  f.receive({ type: 'input_audio_buffer.committed', item_id: 'old-item' })
  f.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'old-item', transcript: '原来的一句' })
  await vi.waitFor(() => expect(f.runtime.admit).toHaveBeenCalledTimes(1))
  f.frames(0.05); f.frames(0, 13)
  for (const event of f.sent.filter(e => e.type === 'input_audio_buffer.append')) {
    const raw=Uint8Array.from(atob(event.audio),char=>char.charCodeAt(0)),view=new DataView(raw.buffer)
    for (let i = 0; i < raw.length; i += 2) expect(Math.abs(view.getInt16(i,true))).toBeLessThan(4000)
  }
  expect(f.callbacks.onError).toHaveBeenCalledWith('播放保护期间的未确认语音没有补发，请在安静后重说。')
})
it('same owner SAPI natural end holds the original quiet barrier and does not reset an existing deadline', async () => {
  const f = await open()
  const lease = { id: 'real-synthetic-lease', generation: 1, sessionId: 'local-session', kind: 'local_sapi', text: '本机受保护讲解' }
  f.lease(lease); f.session.setExternalPlaybackActive(true, true)
  f.lease(null); f.session.setExternalPlaybackActive(false, true)
  const token = protectedPlaybackBarrier.token(); expect(token).not.toBeNull(); expect(f.session.nativeAppendAllowed()).toBe(false)
  f.session.protectedControl = new ProtectedPlaybackControl({ capture: () => ({ inputKind: 'realtime', sessionId: 'local-session', childId: 'child',
    localTrack: f.root, cloudTrack: f.cloud }), current: () => null, recognize: vi.fn(), cancelExact: () => false,
    holdCloud: held => { f.session.protectedControlHold = held; f.session.syncMicrophoneGate() }, failClosed: vi.fn(), now: () => performance.now() })
  f.frames(0.1, 5); expect(protectedPlaybackBarrier.held()).toBe(true)
  const remaining = protectedPlaybackBarrier.remainingMs(performance.now())
  f.lease({ ...lease, id: 'second-real-lease', generation: 2 }); f.session.setExternalPlaybackActive(true, true)
  f.lease(null); f.session.setExternalPlaybackActive(false, true)
  expect(protectedPlaybackBarrier.token()).toBe(token)
  expect(protectedPlaybackBarrier.remainingMs(performance.now())).toBeLessThanOrEqual(remaining)
  f.frames(0, 15); expect(protectedPlaybackBarrier.held()).toBe(false)
  expect(f.session.nativeAppendAllowed()).toBe(true)
})
it('fresh native context reads the old pending receipt with new host identity, never replays its tool or exports internal IDs', async () => {
  const f = fixture(), original = { version: 1 as const, voice_input_id: 'old-voice', supplier_session_id: 'old-supplier',
    input_item_id: 'old-item', response_id: 'old-response', call_id: 'old-call' }
  const pending = { ...context, pending_calls: [{ call_id: 'old-call', action_id: 'old-action', status: 'unknown' as const, identity: original }] }
  const restored = await recoverQwenNativeContext(f.runtime, 'host-current', pending, () => true)
  expect(f.runtime.readReceipt).toHaveBeenCalledExactlyOnceWith({ ...original, host_connection_id: 'host-current' })
  expect(f.runtime.propose).not.toHaveBeenCalled(); expect(restored).toEqual(context)
  const prompt = qwenNativeContextInstructions(pending)
  expect(prompt).not.toContain('old-action'); expect(prompt).not.toContain('old-voice'); expect(prompt).not.toContain('old-call')
})
it('observed consent revocation stops pure native chat without a tool; a fresh owner cannot replay old audio or proposals', async () => {
  const f = await open(); await admitted(f)
  f.receive({ type: 'response.created', response: { id: 'chat-before-revoke' } })
  expect(f.session.playbackAuthorized).toBe(true)
  f.frames(0.2)
  f.permit(false)
  const appends = f.sent.filter(event => event.type === 'input_audio_buffer.append').length
  f.session.manualAudio?.processFrame(new Float32Array(1024).fill(0.7), 16000)
  expect(f.session.closed).toBe(true); expect(f.session.playbackAuthorized).toBe(false)
  expect(f.closeHost).toHaveBeenCalledExactlyOnceWith('host-current')
  f.permit(true) // A retired transport cannot revive when a new grant appears.
  expect(f.session.closed).toBe(true)
  expect(f.sent.filter(event => event.type === 'input_audio_buffer.append')).toHaveLength(appends)
  expect(f.runtime.propose).not.toHaveBeenCalled()
  const fresh = await open()
  expect(fresh.runtime.propose).not.toHaveBeenCalled(); expect(fresh.sent.filter(event => event.type === 'input_audio_buffer.append')).toHaveLength(0)
})
it('a delayed first response stays eligible until the second input is confirmed, then stale events cannot replace it', async () => {
  const f = await open(); await admitted(f)
  f.frames(0.06); f.frames(0, 13)
  expect(f.sent.filter(event => event.type === 'input_audio_buffer.commit')).toHaveLength(1)
  f.receive({ type: 'response.created', response: { id: 'delayed-old-response' } })
  expect(f.session.playbackAuthorized).toBe(true)
  expect(f.sent.filter(event => event.type === 'input_audio_buffer.commit')).toHaveLength(2)
  vi.mocked(f.runtime.admit).mockResolvedValueOnce({ voice_input_id: 'second-canonical-voice' })
  f.receive({ type: 'input_audio_buffer.committed', item_id: 'second-input-item' })
  f.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'second-input-item', transcript: '第一课' })
  await vi.waitFor(() => expect(f.sent.filter(event => event.type === 'response.create')).toHaveLength(2))
  expect(f.sent.filter(event => event.type === 'response.cancel')).toHaveLength(1)
  expect(f.session.playbackAuthorized).toBe(false)
  f.receive({ type: 'response.created', response: { id: 'fresh-second-response' } })
  f.receive({ type: 'response.created', response: { id: 'delayed-old-response' } })
  expect(f.session.playbackAuthorized).toBe(true)
  f.receive({ type: 'response.function_call_arguments.done', response_id: 'delayed-old-response', call_id: 'obsolete-call',
    name: 'learning_prepare_request', arguments: JSON.stringify({ args: { subject: 'geography' } }) })
  f.receive({ type: 'response.function_call_arguments.done', response_id: 'fresh-second-response', call_id: 'fresh-call',
    name: 'learning_prepare_request', arguments: JSON.stringify({ args: { subject: 'geography' } }) })
  f.receive({ type: 'response.done', response: { id: 'fresh-second-response', status: 'completed' } })
  await vi.waitFor(() => expect(f.runtime.propose).toHaveBeenCalledTimes(1))
  expect(vi.mocked(f.runtime.propose).mock.calls[0][0]).toMatchObject({ input_item_id: 'second-input-item',
    voice_input_id: 'second-canonical-voice', response_id: 'fresh-second-response', call_id: 'fresh-call' })
})
it('missing explicit response identity times out without generating a guessed association or executing a tool', async () => {
  vi.useFakeTimers()
  const f = await open()
  f.frames(); f.frames(0, 13)
  f.receive({ type: 'input_audio_buffer.committed', item_id: 'missing-response-input' })
  f.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'missing-response-input', transcript: '帮我复习地理' })
  await vi.advanceTimersByTimeAsync(0)
  expect(f.sent.filter(event => event.type === 'response.create')).toHaveLength(1)
  await vi.advanceTimersByTimeAsync(12001)
  expect(f.session.failureReported).toBe(true)
  f.receive({ type: 'response.created', response: { id: 'late-unowned' } })
  expect(f.session.playbackAuthorized).toBe(false); expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('generation can finish before first PCM without cutting long RTP; observed quiet reopens unknown-AEC input without claiming delivery', async () => {
  vi.useFakeTimers()
  const f = await open()
  f.frames(); f.frames(0, 13)
  f.receive({ type: 'input_audio_buffer.committed', item_id: 'long-input' })
  f.receive({ type: 'conversation.item.input_audio_transcription.completed', item_id: 'long-input', transcript: '请解释地球运动' })
  await vi.advanceTimersByTimeAsync(0)
  f.root.getSettings = () => ({ echoCancellation: false })
  let mediaActive = true, mediaTimer: ReturnType<typeof setTimeout> | null = null
  const observer = { arm: (observed: () => void) => {
    if (mediaActive) mediaTimer = setTimeout(observed, 20)
  }, disarm: () => { if (mediaTimer !== null) clearTimeout(mediaTimer); mediaTimer = null }, dispose: vi.fn() }
  f.session.remotePlaybackObservation = observer
  f.session.remoteAudio = { muted: true, play: vi.fn(async () => undefined), pause: vi.fn(), remove: vi.fn(), srcObject: null }
  f.receive({ type: 'response.created', response: { id: 'long-response' } })
  f.receive({ type: 'response.audio_transcript.done', response_id: 'long-response', transcript: '先区分自转和公转。' })
  f.receive({ type: 'response.done', response: { id: 'long-response', status: 'completed' } })
  expect(f.session.nativeAppendAllowed()).toBe(false) // done is not a first-media/quiet receipt.
  await vi.advanceTimersByTimeAsync(500)
  expect(f.session.playbackAuthorized).toBe(true); expect(f.session.remoteAudio.muted).toBe(false)
  await vi.advanceTimersByTimeAsync(2000)
  expect(f.session.playbackAuthorized).toBe(true); expect(f.session.remoteAudio.muted).toBe(false)
  expect(f.session.nativeAppendAllowed()).toBe(false)
  mediaActive = false
  await vi.advanceTimersByTimeAsync(1600)
  expect(f.session.playbackAuthorized).toBe(true) // Quiet never hard-mutes pending RTP.
  expect(f.session.nativeAppendAllowed()).toBe(true) // No permanent unknown-AEC lock.
  expect(f.runtime.propose).not.toHaveBeenCalled()
  expect(vi.mocked(f.runtime.recordDialogue).mock.calls[0][0].status).toBe('unverified')
})
it('native generation and audio EOS without observed playback never claim a delivered learning result', async () => {
  const f = await open()
  await admitted(f)
  f.receive({ type: 'response.created', response: { id: 'unheard-response' } })
  f.receive({ type: 'response.audio_transcript.done', response_id: 'unheard-response', transcript: '先区分自转和公转。' })
  f.receive({ type: 'response.audio.done', response_id: 'unheard-response' })
  expect(f.runtime.recordDialogue).not.toHaveBeenCalled()
  expect(f.runtime.propose).not.toHaveBeenCalled()
  f.receive({ type: 'response.done', response: { id: 'unheard-response', status: 'completed' } })
  await vi.waitFor(() => expect(f.runtime.recordDialogue).toHaveBeenCalledTimes(1))
  expect(vi.mocked(f.runtime.recordDialogue).mock.calls[0][0]).toMatchObject({
    response_id: 'unheard-response', status: 'unverified', terminal: 'completed',
  })
  expect(f.runtime.propose).not.toHaveBeenCalled()
  expect(f.callbacks.onToolCall).not.toHaveBeenCalled()
})
it('PCM from a finished quiet response cannot cause a later unheard reply to be cancelled',async()=>{
 vi.useFakeTimers()
 const f=await open()
 f.session.nativeDownlink={begin:vi.fn(),append:vi.fn(()=>true),cancel:vi.fn(),dispose:vi.fn()}
 f.frames();f.frames(0,13)
 f.receive({type:'input_audio_buffer.committed',item_id:'first-input'})
 f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'first-input',transcript:'讲一下化学'})
 await vi.advanceTimersByTimeAsync(0)
 f.receive({type:'response.created',response:{id:'first-reply'}})
 f.receive({type:'response.audio.delta',response_id:'first-reply',delta:'AAAAAAAA'})
 f.receive({type:'response.done',response:{id:'first-reply',status:'completed'}})
 await vi.advanceTimersByTimeAsync(1201)
 expect(f.session.nativeRemoteMediaActive).toBe(false)
 expect(f.session.nativeAudioDeltaSeen).toBe(true)
 vi.mocked(f.runtime.admit).mockResolvedValueOnce({voice_input_id:'second-canonical-voice'})
 f.frames();f.frames(0,13)
 f.receive({type:'input_audio_buffer.committed',item_id:'second-input'})
 f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'second-input',transcript:'可以深入一点'})
 await vi.advanceTimersByTimeAsync(0)
 f.frames();f.frames(0,13)
 f.receive({type:'response.created',response:{id:'second-reply'}})
 f.receive({type:'input_audio_buffer.committed',item_id:'noise-input'})
 f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'noise-input',transcript:'。'})
 await vi.advanceTimersByTimeAsync(0)
 expect(f.session.playbackAuthorized).toBe(true)
 expect(f.sent.filter(e=>e.type==='response.cancel')).toHaveLength(0)
 expect(f.runtime.admit).toHaveBeenCalledTimes(2)
})
it('provider explicit server VAD is rejected despite a pending manual update', async () => {
  const f = fixture()
  f.receive({ type: 'session.created', session: { id: 'supplier-session' } })
  f.receive({ type: 'session.updated', session: { id: 'supplier-session', turn_detection: { type: 'server_vad' } } })
  f.session.manualAudio?.processFrame(new Float32Array(1024).fill(0.05), 16000)
  expect(f.session.failureReported).toBe(true); expect(f.session.microphoneReleased).toBe(false)
  expect(f.sent.filter(event => event.type === 'input_audio_buffer.append')).toHaveLength(0)
  expect(f.runtime.admit).not.toHaveBeenCalled(); expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('an already captured local-only original is rejected before raw append rather than stripped and upgraded', async () => {
  const f = await open()
  vi.spyOn(f.callbacks, 'captureInputContext').mockReturnValue({ sessionId: 'local-session',
    reference: { child_id: 'child', local_recovery_only: true }, carryoverConfirmationId: null })
  f.frames()
  expect(f.session.failureReported).toBe(true)
  expect(f.sent.filter(event => event.type === 'input_audio_buffer.append')).toHaveLength(0)
  expect(f.runtime.admit).not.toHaveBeenCalled(); expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('cloud exhaustion may retain only an owned existing local tap, while cloud activity and old host capabilities stay retired', async () => {
  const f = await open()
  // Existing tap attachment is a precondition, not another capture or sampler.
  f.session.protectedControl = { dispose: vi.fn() }
  const rearm = vi.fn(() => {
    expect(f.session.nativeHostConnectionId).toBeNull(); expect(f.session.nativeSocketOpen).toBe(false)
    expect(f.cloud.enabled).toBe(false); expect(f.session.nativeConversation).toBeNull(); return true
  })
  f.session.standbyWake = { dispose: vi.fn(), resumeAfterTransportRetired: rearm }
  f.session.failureReported = true; f.session.inputDeliveryFailed = true; f.session.microphoneEnded = true
  expect(f.session.enterLocalWakeRecovery()).toBe(true)
  expect(rearm).toHaveBeenCalledExactlyOnceWith()
  expect(f.session.closed).toBe(false); expect(f.session.localWakeRecovery).toBe(true)
  expect(f.session.manualAudio).toBeNull(); expect(f.session.nativeConversation).toBeNull()
  expect(f.session.nativeHostConnectionId).toBeNull(); expect(f.cloud.enabled).toBe(false)
  expect(f.session.nativeAppendAllowed()).toBe(false)
  expect(f.closeHost).toHaveBeenCalledExactlyOnceWith('host-current')
  f.receive({ type: 'response.created', response: { id: 'late-retired-cloud-response' } })
  expect(f.runtime.propose).not.toHaveBeenCalled(); expect(f.session.playbackAuthorized).toBe(false)
  f.permit(false); expect(f.session.closed).toBe(true)
})

it('standby retires already captured response eligibility, so a late native tool cannot revive on reactivation',async()=>{
  const f=await open();await admitted(f)
  f.active(false);f.active(true)
  f.receive({type:'response.created',response:{id:'late-after-standby'}})
  f.receive({type:'response.function_call_arguments.done',response_id:'late-after-standby',call_id:'late-call',name:'learning_prepare_request',arguments:JSON.stringify({args:{subject:'geography'}})})
  expect(f.session.playbackAuthorized).toBe(false);expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('exact privacy stop in current canonical provider input closes native capture before admission or any model response',async()=>{
  const f=await open();f.frames();f.frames(0,13)
  f.receive({type:'input_audio_buffer.committed',item_id:'privacy-input'})
  f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'privacy-input',transcript:'小知，别听了'})
  expect(f.session.closed).toBe(true);expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
  expect(f.sent.some(e=>e.type==='response.create')).toBe(false)
})

it('retired owner cannot rearm the old local standby tap', async () => {
  const f=await open();const rearm=vi.fn(()=>true)
  f.session.protectedControl={dispose:vi.fn()};f.session.standbyWake={dispose:vi.fn(),resumeAfterTransportRetired:rearm}
  f.owner(false);expect(f.session.enterLocalWakeRecovery()).toBe(false);expect(rearm).not.toHaveBeenCalled()
})

it('a disposed standby tap cannot be advertised as recoverable after cloud retirement', async () => {
  const f=await open();f.session.protectedControl={dispose:vi.fn()}
  f.session.standbyWake={dispose:vi.fn(),resumeAfterTransportRetired:vi.fn(()=>false)}
  expect(f.session.enterLocalWakeRecovery()).toBe(false);expect(f.session.localWakeRecovery).toBe(false)
  expect(f.session.nativeHostConnectionId).toBeNull();expect(f.session.nativeAppendAllowed()).toBe(false)
})

it('observes valid late cancellation without admitting it or changing the new response',async()=>{
 const f=await open();await admitted(f)
 f.receive({type:'response.created',response:{id:'old-diagnostic-response'}})
 f.receive({type:'response.audio_transcript.done',response_id:'old-diagnostic-response',transcript:'。'})
 f.frames();f.frames(0,13)
 f.receive({type:'input_audio_buffer.committed',item_id:'second-diagnostic-item'})
 f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'second-diagnostic-item',transcript:'继续公开模拟问题'})
 await vi.waitFor(()=>expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(2))
 f.receive({type:'response.created',response:{id:'new-diagnostic-response'}})
 const activeBefore=f.session.responseActive,authorizedBefore=f.session.playbackAuthorized
 f.receive({type:'response.done',response:{id:'old-diagnostic-response',status:'cancelled',status_details:{reason:'client_cancelled',private:'PRIVATE_MARKER'}}})
 expect(f.session.activeResponseId).toBe('new-diagnostic-response')
 expect(f.session.responseActive).toBe(activeBefore);expect(f.session.playbackAuthorized).toBe(authorizedBefore)
 const records=()=>((window.desktop!.recordRendererDiagnostic as any).mock.calls.map((c:any)=>c[0]).filter((r:any)=>r.event==='voice.realtime.native_response'))
 expect(records()).toHaveLength(1)
 expect(records()[0].details).toMatchObject({response_status:'cancelled',response_reason:'client_cancelled',cancel_source:'confirmed_input',response_text_chars:1,response_text_kind:'punctuation',audio_chunk_count:0,pcm_chunk_count:0,new_input_before_pcm:true,terminal_current:false,physical_audibility_verified:false})
 expect(JSON.stringify(records())).not.toMatch(/old-diagnostic-response|new-diagnostic-response|PRIVATE_MARKER/)
 f.receive({type:'response.done',response:{id:'never-created-response',status:'cancelled',status_details:{reason:'client_cancelled'}}})
 expect(records()).toHaveLength(1)
 f.session.close()
 expect(f.session.nativeResponseDiagnostics.observe('new-diagnostic-response',{type:'response.done',response:{status:'failed'}},false)).toBeUndefined()
})
