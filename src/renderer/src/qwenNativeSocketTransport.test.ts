// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'
import { QwenNativePcmDownlink } from './qwenNativePcmDownlink'
import { PLAYBACK_TAIL_FAILURE, protectedPlaybackBarrier } from './protectedPlaybackBarrier'
import type { QwenNativeBoundRuntime, QwenNativeRuntimeContext } from './qwenNativeProtocol'
import type { QwenNativeSocketEvent } from '../../shared/qwenNativeSocket'
const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'learning.prepare_request',tool_name:'learning_prepare_request',description:'Prepare a proposed learning goal.',required_arguments:[],arguments_schema:{type:'object',additionalProperties:false,properties:{}}}]}
const context:QwenNativeRuntimeContext={version:1,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[]}
const sessions:any[]=[]
function fixture(model = 'qwen3.8-omni-flash-realtime'){
  let listener:((packet:QwenNativeSocketEvent)=>void)|null=null,allowed=true,owner=true,witness=false,active=true
  const sent:any[]=[],order:string[]=[]
  const emit=(event:unknown,host='host-current')=>listener?.({hostConnectionId:host,data:JSON.stringify(event)})
  const desktop={onQwenNativeSocketEvent:vi.fn((fn:typeof listener)=>{order.push('subscribe');listener=fn;return ()=>{listener=null}}),
    openQwenNativeSocket:vi.fn(async()=>{order.push('open');emit({type:'session.created',session:{id:'supplier-current'}});return {nativeHostConnectionId:'host-current',model,voice:'Tina'}}),
    sendQwenNativeSocket:vi.fn(async(_host:string,raw:string)=>{sent.push(JSON.parse(raw))}),
    closeQwenNativeSocket:vi.fn(async()=>undefined),recordRendererDiagnostic:vi.fn(async(_diagnostic:{event:string;details?:unknown;level?:string})=>undefined)}
  vi.stubGlobal('window',{desktop,setInterval,clearInterval,setTimeout,clearTimeout})
  const makeTrack=()=>Object.assign(new EventTarget(),{kind:'audio',id:crypto.randomUUID(),enabled:true,readyState:'live',muted:false,
    getSettings:()=>({echoCancellation:true}),stop:vi.fn(),clone:()=>makeTrack()}) as unknown as MediaStreamTrack
  const root=makeTrack(),stream={getAudioTracks:()=>[root],getTracks:()=>[root]} as unknown as MediaStream
  const getUserMedia=vi.fn(async()=>stream);vi.stubGlobal('navigator',{mediaDevices:{getUserMedia}})
  const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(async()=>context),
    admit:vi.fn(async()=>({voice_input_id:'canonical-current'})),propose:vi.fn(),recordDialogue:vi.fn(async()=>undefined),readReceipt:vi.fn()}
  const downlinkStart=vi.spyOn(QwenNativePcmDownlink.prototype,'start').mockResolvedValue({getAudioTracks:()=>[makeTrack()]} as unknown as MediaStream)
  const downlinkDispose=vi.spyOn(QwenNativePcmDownlink.prototype,'dispose').mockImplementation(()=>undefined)
  const downlinkCancel=vi.spyOn(QwenNativePcmDownlink.prototype,'cancel')
  const session=new QwenRealtimeVoiceSession({childName:'Synthetic',nativeRuntime:runtime,nativeAudioAllowed:()=>allowed,
    nativeActivation:{isActive:()=>active,canRecoverPersisted:()=>witness,word:()=> '小知',onWake:vi.fn(),recognizeStandby:vi.fn()},
    protectedPlaybackControl:{sessionId:'session-current',childId:'child-current',isCurrent:()=>owner,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false},
    callbacks:{onStatus:vi.fn(),captureInputContext:()=>({sessionId:'session-current',reference:{child_id:'child-current'},carryoverConfirmationId:null})}}) as any
  vi.spyOn(session,'attachOwnedControlFanout').mockResolvedValue(undefined)
  vi.spyOn(session,'attachRemoteStream').mockImplementation(()=>undefined)
  sessions.push(session)
  const ready=async()=>{await session.connect();await session.nativeSendTail
    const update=sent.find(e=>e.type==='session.update').session
    emit({type:'session.updated',session:{id:'supplier-current',model,instructions:update.instructions,tools:update.tools}})
    await Promise.resolve();return update}
  return {session,desktop,sent,order,emit,ready,runtime,root,getUserMedia,downlinkStart,downlinkDispose,downlinkCancel,
    allow:(v:boolean)=>{allowed=v;session.syncNativeAuthority()},owner:(v:boolean)=>{owner=v},witness:()=>{witness=true},active:(v:boolean)=>{active=v;session.syncConversationActivation()}}
}
afterEach(()=>{for(const s of sessions.splice(0))s.close();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers()})

async function keepaliveFixture(model?: string) {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance'] })
  const f = fixture(model)
  vi.spyOn(f.session, 'startAudioHealthPolling').mockImplementation(() => undefined)
  const update = await f.ready()
  const ack = (overrides: Record<string, unknown> = {}) => f.emit({ type: 'session.updated', session: {
    id: 'supplier-current', model: model ?? 'qwen3.8-omni-flash-realtime', instructions: update.instructions, tools: update.tools, ...overrides,
  } })
  return { ...f, ack, keepalives: () => f.sent.filter(e => e.type === 'session.update' && !Object.keys(e.session).length) }
}

it('uses only an empty manual control on each validated ACK interval, including standby', async () => {
  const f = await keepaliveFixture()
  f.active(false)
  await vi.advanceTimersByTimeAsync(179_999)
  expect(f.keepalives()).toHaveLength(0)
  await vi.advanceTimersByTimeAsync(1)
  expect(f.keepalives()).toEqual([{ type: 'session.update', session: {} }])
  await vi.advanceTimersByTimeAsync(12_000)
  f.ack(); f.ack()
  const acks = f.desktop.recordRendererDiagnostic.mock.calls.filter(([row]) => row.event === 'voice.realtime.keepalive_acknowledged')
  expect(acks).toEqual([[{ event: 'voice.realtime.keepalive_acknowledged', level: 'info',
    details: { runtime: 'qwen_omni_realtime', elapsed_ms: 12_000 } }]])
  await vi.advanceTimersByTimeAsync(179_999)
  expect(f.keepalives()).toHaveLength(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(f.keepalives()).toHaveLength(2)
  expect(f.sent).toHaveLength(3)
})

it('defers while busy beyond five minutes, then sends a single control once idle', async () => {
  const f = await keepaliveFixture()
  const busy = vi.spyOn(f.session.manualAudio, 'hasPendingWork').mockReturnValue(true)
  await vi.advanceTimersByTimeAsync(305_000)
  expect(f.keepalives()).toHaveLength(0)
  busy.mockReturnValue(false)
  await vi.advanceTimersByTimeAsync(4_999)
  expect(f.keepalives()).toHaveLength(0)
  await vi.advanceTimersByTimeAsync(20_001)
  expect(f.keepalives()).toHaveLength(1)
  expect(f.session.failureReported).toBe(false)
})

it.each(['owner', 'consent'] as const)('checks %s again at serialized control dispatch', async change => {
  const f = await keepaliveFixture()
  let release!: () => void
  f.session.nativeSendTail = new Promise<void>(done => { release = done })
  await vi.advanceTimersByTimeAsync(180_000)
  expect(f.session.nativePendingSendBytes).toBeGreaterThan(0)
  if (change === 'owner') f.owner(false)
  else f.allow(false)
  release()
  await vi.advanceTimersByTimeAsync(10_000)
  expect(f.keepalives()).toHaveLength(0)
  expect(f.session.nativePendingSendBytes).toBe(0)
})

it('does not duplicate an unacknowledged control or fail a turn from the control promise', async () => {
  const f = await keepaliveFixture()
  f.desktop.sendQwenNativeSocket.mockImplementationOnce(async (_host, raw) => {
    f.sent.push(JSON.parse(raw))
    throw Error('private keepalive transport detail')
  })
  await vi.advanceTimersByTimeAsync(600_000)
  expect(f.keepalives()).toHaveLength(1)
  expect(f.session.failureReported).toBe(false)
})

it('fails closed if a keepalive returns a changed session configuration', async () => {
  const f = await keepaliveFixture()
  await vi.advanceTimersByTimeAsync(180_000)
  f.ack({ turn_detection: { type: 'server_vad' } })
  expect(f.session.failureReported).toBe(true)
  expect(f.session.nativeAppendAllowed()).toBe(false)
  expect(f.desktop.recordRendererDiagnostic.mock.calls.some(([row]) => row.event === 'voice.realtime.keepalive_acknowledged')).toBe(false)
  await vi.advanceTimersByTimeAsync(600_000)
  expect(f.keepalives()).toHaveLength(1)
})

it('does not send a keepalive to a different native model', async () => {
  const f = await keepaliveFixture('qwen3-omni-flash-realtime')
  await vi.advanceTimersByTimeAsync(600_000)
  expect(f.keepalives()).toHaveLength(0)
})
it('preserves an exhausted playback-tail failure before microphone and socket acquisition', async () => {
  const f = fixture()
  const scope = { sessionId: 'session-current', childId: 'child-current' }
  const now = performance.now()
  const token = protectedPlaybackBarrier.begin(scope, 'synthetic-lease', 1, now - 20_000)!
  try {
    expect(protectedPlaybackBarrier.reserveOpen(scope, now - 11_000)).toBe(true)
    expect(protectedPlaybackBarrier.reserveOpen(scope, now - 5_500)).toBe(true)
    expect(protectedPlaybackBarrier.mayOpen(scope, now)).toBe(false)
    await expect(f.session.connect()).rejects.toThrow(PLAYBACK_TAIL_FAILURE)
    expect(f.session.options.callbacks.onStatus).toHaveBeenLastCalledWith(expect.objectContaining({ phase: 'error', detail: PLAYBACK_TAIL_FAILURE }))
    expect(f.desktop.recordRendererDiagnostic).toHaveBeenCalledWith(expect.objectContaining({ details: {
      native_connect_stage: 'playback_barrier', native_connect_failure: 'playback_tail_unconfirmed',
    } }))
    expect(f.getUserMedia).not.toHaveBeenCalled()
    expect(f.desktop.openQwenNativeSocket).not.toHaveBeenCalled()
    expect(f.sent).toEqual([])
    expect(protectedPlaybackBarrier.held()).toBe(true)
  } finally { protectedPlaybackBarrier.rollback(token) }
})

it('does not trust playback-tail text coming back from socket IPC', async () => {
  const f = fixture()
  f.desktop.openQwenNativeSocket.mockRejectedValue(new Error(PLAYBACK_TAIL_FAILURE))
  await expect(f.session.connect()).rejects.toThrow('自然语音连接没有完成，正在恢复。')
})

it('also preserves a protected tail failure in local microphone observation', async () => {
  const f = fixture()
  vi.mocked(f.session.attachOwnedControlFanout).mockRejectedValue(new Error(PLAYBACK_TAIL_FAILURE))
  await expect(f.session.connect()).rejects.toThrow(PLAYBACK_TAIL_FAILURE)
  expect(f.session.audioTrack.enabled).toBe(false)
  expect(f.desktop.openQwenNativeSocket).not.toHaveBeenCalled()
})

it.each(['QWEN_NATIVE_CREDENTIAL_REJECTED','QWEN_NATIVE_ACCESS_DENIED','QWEN_NATIVE_ACCOUNT_BALANCE'])('retains only the safe provider failure across a rejected IPC open: %s',async code=>{
  const f=fixture()
  f.desktop.openQwenNativeSocket.mockRejectedValue(new Error(`Error invoking remote method 'voice:native-socket-open': Error: ${code}`))
  await expect(f.session.connect()).rejects.toThrow(code)
  const failures=f.session.options.callbacks.onStatus.mock.calls.map((call:any[])=>call[0]).filter((value:any)=>value.phase==='error')
  expect(failures).toHaveLength(1)
  expect(failures[0].detail).toBe(code)
  expect(f.sent).toEqual([])
})
it('actual socket subscribes before open, drains only the returned host early event and accepts same configured manual ACK with omitted VAD',async()=>{
  const f=fixture();await f.ready()
  expect(f.order).toEqual(['subscribe','open']);expect(f.getUserMedia).toHaveBeenCalledTimes(1)
  expect(f.session.peer).toBeNull();expect(f.session.sender).toBeNull();expect(f.session.channel).toBeNull()
  expect(f.session.audioTrack.enabled).toBe(false);expect(f.session.nativeAppendAllowed()).toBe(true)
  expect(f.sent.filter(e=>e.type==='session.update')).toHaveLength(1)
  f.emit({type:'response.created',response:{id:'foreign-response'}},'foreign-host')
  expect(f.session.activeResponseId).toBeNull();expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('a foreign or explicit server VAD ACK cannot open raw upload',async()=>{
  const f=fixture();await f.session.connect();await f.session.nativeSendTail
  const update=f.sent[0].session
  f.emit({type:'session.updated',session:{id:'wrong-supplier',model:'qwen3.8-omni-flash-realtime',instructions:update.instructions,tools:[],turn_detection:null}})
  expect(f.session.microphoneReleased).toBe(false);expect(f.session.failureReported).toBe(true)
  expect(f.sent.some(e=>e.type==='input_audio_buffer.append')).toBe(false)
})
it('actual queued append/commit and response cannot borrow reopened SAPI permission, even before the IPC send microtask',async()=>{
  const f=fixture();await f.ready()
  f.session.send({type:'input_audio_buffer.append',audio:'PROTECTED_MARKER'})
  f.session.send({type:'input_audio_buffer.commit'})
  f.session.send({type:'response.create'})
  f.session.setExternalPlaybackActive(true,true);f.session.setExternalPlaybackActive(false,true)
  await f.session.nativeSendTail
  expect(f.sent.filter(e=>['input_audio_buffer.append','input_audio_buffer.commit','response.create'].includes(e.type))).toEqual([])
  f.session.send({type:'input_audio_buffer.append',audio:'FRESH_PUBLIC_MARKER'})
  await f.session.nativeSendTail
  expect(f.sent.filter(e=>e.type==='input_audio_buffer.append')).toEqual([{type:'input_audio_buffer.append',audio:'FRESH_PUBLIC_MARKER'}])
})
it('closing during open closes the late returned host and never loads its catalog or accepts its early event',async()=>{
  const f=fixture();let resolve!:(value:{nativeHostConnectionId:string;model:string;voice:string})=>void
  f.desktop.openQwenNativeSocket.mockImplementation(()=>new Promise(done=>{resolve=done}))
  const opening=f.session.connect();await vi.waitFor(()=>expect(f.desktop.openQwenNativeSocket).toHaveBeenCalledTimes(1))
  f.session.close();resolve({nativeHostConnectionId:'late-host',model:'qwen3.8-omni-flash-realtime',voice:'Tina'});await opening
  expect(f.desktop.closeQwenNativeSocket).toHaveBeenCalledExactlyOnceWith('late-host');expect(f.runtime.loadCatalog).not.toHaveBeenCalled()
  expect(f.downlinkDispose).toHaveBeenCalled();expect(f.sent).toEqual([])
})
it('consent revoke retires actual host and queued frames; re-enable cannot revive the old transport',async()=>{
  const f=fixture();await f.ready();f.session.send({type:'input_audio_buffer.append',audio:'OLD_MARKER'})
  f.allow(false);f.allow(true);await f.session.nativeSendTail
  expect(f.session.closed).toBe(true);expect(f.desktop.closeQwenNativeSocket).toHaveBeenCalledExactlyOnceWith('host-current')
  expect(f.sent.some(e=>e.audio==='OLD_MARKER')).toBe(false);expect(f.session.nativeAppendAllowed()).toBe(false)
})
it('idle stop never sends vendor cancel; current generating stop cancels once and stops all local scheduled output',async()=>{
  const f=fixture();await f.ready();f.session.interrupt();await f.session.nativeSendTail
  expect(f.sent.some(e=>e.type==='response.cancel')).toBe(false)
  f.session.responseActive=true;f.session.activeResponseId='current-response';f.session.nativeVendorGeneratingResponseId='current-response';f.session.interrupt();f.session.interrupt();await f.session.nativeSendTail
  expect(f.sent.filter(e=>e.type==='response.cancel')).toHaveLength(1);expect(f.downlinkCancel).toHaveBeenCalled()
  expect(f.sent.some(e=>e.type==='conversation.item.truncate')).toBe(false)
})
it('canonical orphan waits for human activation, exact controlled item ACK binds actual supplier ID and never re-admits or replays a function',async()=>{
  const f=fixture(),original={voice_input_id:'old-voice',client_action_id:'old-input',created_at:'2026-10-28T12:00:00Z',text:'继续复习水循环'}
  vi.mocked(f.runtime.loadContext).mockResolvedValue({...context,pending_inputs:[original]})
  await f.ready();await f.session.maybeRestorePersistedInput();expect(f.sent.some(e=>e.type==='conversation.item.create')).toBe(false)
  f.witness();await f.session.maybeRestorePersistedInput();await f.session.nativeSendTail
  const create=f.sent.find(e=>e.type==='conversation.item.create');expect(create.item).toEqual({type:'message',role:'user',content:[{type:'input_text',text:original.text}]})
  f.emit({type:'conversation.item.created',item:{id:'wrong-content-item',type:'message',role:'user',content:[{type:'input_text',text:'另外一句'}]}})
  expect(f.sent.some(e=>e.type==='response.create')).toBe(false)
  f.emit({type:'conversation.item.created',item:{id:'actual-supplier-restored-item',type:'message',role:'user',content:[{type:'input_text',text:original.text}]}})
  await f.session.nativeSendTail
  expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1);expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
  f.emit({type:'response.created',response:{id:'restored-response'}})
  f.emit({type:'response.text.done',response_id:'restored-response',text:'你想先回顾哪个过程？'})
  f.emit({type:'response.done',response:{id:'restored-response',status:'completed'}})
  await vi.waitFor(()=>expect(f.runtime.recordDialogue).toHaveBeenCalledTimes(1))
  expect(vi.mocked(f.runtime.recordDialogue).mock.calls[0][0]).toMatchObject({voice_input_id:'old-voice',input_item_id:'actual-supplier-restored-item',input_source:'persisted_native',host_connection_id:'host-current'})
})
it('reports only the model bound to the live native socket and clears it on close',async()=>{
  const f=fixture()
  expect(f.session.getConnectedModel()).toBeUndefined()
  await f.ready()
  expect(f.session.getConnectedModel()).toBe('qwen3.8-omni-flash-realtime')
  f.session.close()
  expect(f.session.getConnectedModel()).toBeUndefined()
})
