// @vitest-environment node
import {classifyQwenRealtimeError} from './QwenRealtimeVoiceInput'
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


it('accepts an identical repeat ACK without reopening the microphone or retiring the connection',async()=>{
 const f=await open();const statusCount=f.callbacks.onStatus.mock.calls.length
 const sends=f.sent.length,replaceCount=f.replaceTrack.mock.calls.length
 f.receive({type:'session.updated',session:{id:'supplier-session',turn_detection:null}})
 expect(f.callbacks.onError).not.toHaveBeenCalled()
 expect(f.session.failureReported).toBe(false);expect(f.session.sessionReady).toBe(true)
 expect(f.callbacks.onStatus).toHaveBeenCalledTimes(statusCount)
 expect(f.replaceTrack).toHaveBeenCalledTimes(replaceCount);expect(f.sent).toHaveLength(sends)
 f.frames();f.frames(0,13)
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.commit')).toHaveLength(1)
})
it('canonicalizes ACK property ordering and ignores unrelated supplier counters',async()=>{
 const f=await open();const tools=structuredClone(f.sent.find(e=>e.type==='session.update').session.tools)
 tools[0].function=Object.fromEntries(Object.entries(tools[0].function).reverse())
 f.receive({type:'session.updated',session:{tools,turn_detection:null,id:'supplier-session',supplier_counter:23}})
 expect(f.callbacks.onError).not.toHaveBeenCalled();expect(f.session.failureReported).toBe(false)
})
it.each(['model','instructions','mode','tool_schema','audio','temperature'] as const)('rejects a repeated ACK that changes confirmed %s',async field=>{
 const f=fixture()
 f.receive({type:'session.created',session:{id:'supplier-session'}})
 const audio={input:{format:{type:'pcm',sample_rate:16000}},output:{voice:'Synthetic',format:{type:'pcm',sample_rate:24000}}}
 f.receive({type:'session.updated',session:{id:'supplier-session',turn_detection:null,audio,temperature:0}})
 await Promise.resolve();await Promise.resolve();expect(f.session.sessionReady).toBe(true)
 const changed:any={id:'supplier-session',turn_detection:null,audio:structuredClone(audio),temperature:0}
 if(field==='model')changed.model='unapproved-model'
 if(field==='instructions')changed.instructions='changed private instructions'
 if(field==='mode')changed.turn_detection={type:'server_vad'}
 if(field==='tool_schema'){
  changed.tools=structuredClone(f.sent.find(e=>e.type==='session.update').session.tools)
  changed.tools[0].function.parameters.additionalProperties=true
 }
 if(field==='audio')changed.audio.output.format.sample_rate=48000
 if(field==='temperature')changed.temperature=1
 f.receive({type:'session.updated',session:changed})
 expect(f.session.failureReported).toBe(true)
 expect(classifyQwenRealtimeError(f.callbacks.onError.mock.calls.at(-1)![0])).toBe('QWEN_NATIVE_SESSION_CONFIG_UNCONFIRMED')
 expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('a foreign IPC owner cannot replay a valid ACK into this connection',async()=>{
 const f=await open();const before=f.sent.length
 f.session.receiveNativePacket({hostConnectionId:'retired-host',data:JSON.stringify({type:'session.updated',session:{id:'supplier-session'}})})
 expect(f.callbacks.onError).not.toHaveBeenCalled();expect(f.sent).toHaveLength(before)
 f.owner(false);f.receive({type:'session.updated',session:{id:'supplier-session',turn_detection:null}})
 expect(f.session.nativeAppendAllowed()).toBe(false);expect(f.runtime.admit).not.toHaveBeenCalled()
})
it('native transcription failure has the existing recoverable lost-input reason and never executes an operation',async()=>{
 const f=await open();f.frames();f.frames(0,13)
 f.receive({type:'input_audio_buffer.committed',item_id:'public-failed-input'})
 f.receive({type:'conversation.item.input_audio_transcription.failed',item_id:'public-failed-input'})
 expect(f.callbacks.onError).toHaveBeenCalledExactlyOnceWith('这句话的云端转写未完成，没有执行请求；恢复后请重说。')
 expect(classifyQwenRealtimeError(f.callbacks.onError.mock.calls[0][0])).toBe('QWEN_REALTIME_INPUT_TRANSCRIPTION_FAILED')
 expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
})

it('the real Household identity deadline reports a fixed reason and never replays an operation',async()=>{
 vi.useFakeTimers();const f=fixture()
 const entry:any={transcript:'公开模拟问题',entry_receipt:{issued_at_ms:Date.now()}}
 f.session.options.householdEntry=entry
 f.runtime.admitHousehold=vi.fn(async()=>({voice_input_id:'public-household-original',source_kind:'household_local_transcript',raw_audio_available:false,entry_state:'unprocessed'}))as any
 f.receive({type:'session.created',session:{id:'supplier-session'}})
 f.receive({type:'session.updated',session:{id:'supplier-session',turn_detection:null}})
 let frame:((frame:Float32Array,rate:number)=>void)|undefined
 vi.spyOn(ProtectedPlaybackControl.prototype,'attach').mockImplementation(async(_stream,callback)=>{frame=callback})
 await f.session.attachOwnedControlFanout({getTracks:()=>[f.root]},f.root,f.cloud)
 expect(frame).toBeTypeOf('function');frame!(new Float32Array(1024),16000)
 await vi.waitFor(()=>expect(f.sent.filter(e=>e.type==='conversation.item.create')).toHaveLength(1))
 await vi.advanceTimersByTimeAsync(12001)
 expect(f.callbacks.onError).toHaveBeenCalledExactlyOnceWith('首次语音原话的供应商身份尚未确认，没有重复操作。')
 expect(classifyQwenRealtimeError(f.callbacks.onError.mock.calls[0][0])).toBe('QWEN_NATIVE_HOUSEHOLD_INPUT_UNCONFIRMED')
 expect(f.runtime.admitHousehold).toHaveBeenCalledTimes(1);expect(f.runtime.propose).not.toHaveBeenCalled()
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(0)
 await vi.advanceTimersByTimeAsync(12001);expect(f.sent.filter(e=>e.type==='conversation.item.create')).toHaveLength(1)
})
it('the real restored-input identity deadline leaves the canonical original untouched and never dispatches a tool',async()=>{
 vi.useFakeTimers();const f=fixture()
 const original={voice_input_id:'public-saved-original',client_action_id:'public-saved-action',created_at:new Date().toISOString(),text:'公开尚未处理的问题'}
 const pending={...context,pending_inputs:[original]}
 f.session.options.nativeActivation.canRecoverPersisted=()=>true
 f.runtime.loadContext=vi.fn(async()=>pending);f.session.nativeContext=pending
 f.receive({type:'session.created',session:{id:'supplier-session'}})
 f.receive({type:'session.updated',session:{id:'supplier-session',turn_detection:null}})
 let frame:((frame:Float32Array,rate:number)=>void)|undefined
 vi.spyOn(ProtectedPlaybackControl.prototype,'attach').mockImplementation(async(_stream,callback)=>{frame=callback})
 await f.session.attachOwnedControlFanout({getTracks:()=>[f.root]},f.root,f.cloud)
 expect(frame).toBeTypeOf('function');frame!(new Float32Array(1024),16000)
 await vi.waitFor(()=>expect(f.sent.filter(e=>e.type==='conversation.item.create')).toHaveLength(1))
 await vi.advanceTimersByTimeAsync(12001)
 expect(f.callbacks.onError).toHaveBeenCalledExactlyOnceWith('已保存原话的恢复身份尚未确认，没有重新执行。')
 expect(classifyQwenRealtimeError(f.callbacks.onError.mock.calls[0][0])).toBe('QWEN_NATIVE_RESTORED_INPUT_UNCONFIRMED')
 expect(pending.pending_inputs).toEqual([original]);expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(0)
})
