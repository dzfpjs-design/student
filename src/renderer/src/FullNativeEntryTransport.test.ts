// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'
import { QwenNativePcmDownlink } from './qwenNativePcmDownlink'
import type { QwenNativeBoundRuntime, QwenNativeRuntimeContext } from './qwenNativeProtocol'
import type { QwenNativeSocketEvent } from '../../shared/qwenNativeSocket'
const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'learning.prepare_request',tool_name:'learning_prepare_request',description:'Prepare a proposed learning goal.',required_arguments:[],arguments_schema:{type:'object',additionalProperties:false,properties:{}}}]}
const context:QwenNativeRuntimeContext={version:1,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[]}
const sessions:any[]=[]
it('records only a bounded connection stage and category when the local downlink fails',async()=>{
 const f=fixture(false)
 f.downlinkStart.mockRejectedValue(new Error('PRIVATE_TOKEN_PRIVATE_UTTERANCE'))
 await expect(f.session.connect()).rejects.toThrow()
 const records=f.desktop.recordRendererDiagnostic.mock.calls.map(([record]:any)=>record).filter((record:any)=>record.event==='voice.realtime.native_connect_failed')
 expect(records).toEqual([{event:'voice.realtime.native_connect_failed',level:'warn',details:{native_connect_stage:'downlink',native_connect_failure:'other'}}])
 expect(JSON.stringify(records)).not.toContain('PRIVATE_')
})
it('classifies microphone permission rejection without recording its private message',async()=>{
 const f=fixture(false), error=Object.assign(new Error('PRIVATE_DEVICE_PRIVATE_MESSAGE'),{name:'NotAllowedError'})
 f.getUserMedia.mockRejectedValue(error)
 await expect(f.session.connect()).rejects.toThrow()
 const records=f.desktop.recordRendererDiagnostic.mock.calls.map(([record]:any)=>record).filter((record:any)=>record.event==='voice.realtime.native_connect_failed')
 expect(records).toEqual([{event:'voice.realtime.native_connect_failed',level:'warn',details:{native_connect_stage:'microphone',native_connect_failure:'permission_denied'}}])
 expect(JSON.stringify(records)).not.toContain('PRIVATE_')
})
const entry={entry_receipt:{version:1 as const,origin_scope:'unassigned_household' as const,storage_generation:'public-storage',receipt_id:'receipt-public',client_action_id:'local-public',issued_at_ms:1000,transcript_sha256:'a'.repeat(64),model_version:'local-public',source_kind:'household_local_transcript' as const},transcript:'小知，帮我打开错题本'}
function fixture(includeHouseholdEntry=true){
  let listener:((packet:QwenNativeSocketEvent)=>void)|null=null,allowed=true,owner=true,witness=false,active=true
  const sent:any[]=[],order:string[]=[]
  const emit=(event:unknown,host='host-current')=>listener?.({hostConnectionId:host,data:JSON.stringify(event)})
  const desktop={onQwenNativeSocketEvent:vi.fn((fn:typeof listener)=>{order.push('subscribe');listener=fn;return ()=>{listener=null}}),
    openQwenNativeSocket:vi.fn(async()=>{order.push('open');emit({type:'session.created',session:{id:'supplier-current'}});return {nativeHostConnectionId:'host-current',model:'qwen3.8-omni-flash-realtime',voice:'Tina'}}),
    sendQwenNativeSocket:vi.fn(async(_host:string,raw:string)=>{sent.push(JSON.parse(raw))}),
    closeQwenNativeSocket:vi.fn(async()=>undefined),recordRendererDiagnostic:vi.fn(async()=>undefined)}
  vi.stubGlobal('window',{desktop,setInterval,clearInterval,setTimeout,clearTimeout})
  const makeTrack=()=>Object.assign(new EventTarget(),{kind:'audio',id:crypto.randomUUID(),enabled:true,readyState:'live',muted:false,
    getSettings:()=>({echoCancellation:true}),stop:vi.fn(),clone:()=>makeTrack()}) as unknown as MediaStreamTrack
  const root=makeTrack(),stream={getAudioTracks:()=>[root],getTracks:()=>[root]} as unknown as MediaStream
  const getUserMedia=vi.fn(async()=>stream);vi.stubGlobal('navigator',{mediaDevices:{getUserMedia}})
  const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(async()=>context),
    admitHousehold:vi.fn(async()=>({voice_input_id:'entry-voice',source_kind:'household_local_transcript' as const,raw_audio_available:false as const,entry_state:'unprocessed' as const})),admit:vi.fn(async()=>({voice_input_id:'canonical-current'})),propose:vi.fn(async()=>({version:1 as const,proposal_status:'admitted' as const,execution:{status:'completed' as const,location:'server'},public_result:{status:'saved'}})),recordDialogue:vi.fn(async()=>undefined),readReceipt:vi.fn()}
  const downlinkStart=vi.spyOn(QwenNativePcmDownlink.prototype,'start').mockResolvedValue({getAudioTracks:()=>[makeTrack()]} as unknown as MediaStream)
  const downlinkDispose=vi.spyOn(QwenNativePcmDownlink.prototype,'dispose').mockImplementation(()=>undefined)
  const downlinkCancel=vi.spyOn(QwenNativePcmDownlink.prototype,'cancel')
  const session=new QwenRealtimeVoiceSession({childName:'Synthetic',householdEntry:includeHouseholdEntry?entry:undefined,nativeRuntime:runtime,nativeAudioAllowed:()=>allowed,
    nativeActivation:{isActive:()=>active,canRecoverPersisted:()=>witness,word:()=> '小知',onWake:vi.fn(),recognizeStandby:vi.fn()},
    protectedPlaybackControl:{sessionId:'session-current',childId:'child-current',isCurrent:()=>owner,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false},
    callbacks:{onLocalSafety:vi.fn(),onStatus:vi.fn(),captureInputContext:()=>({sessionId:'session-current',reference:{child_id:'child-current'},carryoverConfirmationId:null})}}) as any
  vi.spyOn(session,'attachOwnedControlFanout').mockResolvedValue(undefined)
  vi.spyOn(session,'attachRemoteStream').mockImplementation(()=>undefined)
  sessions.push(session)
  const ready=async()=>{await session.connect();await session.nativeSendTail
    const update=sent.find(e=>e.type==='session.update').session
    emit({type:'session.updated',session:{id:'supplier-current',model:'qwen3.8-omni-flash-realtime',instructions:update.instructions,tools:update.tools}})
    await Promise.resolve();return update}
  return {session,desktop,sent,order,emit,ready,runtime,root,getUserMedia,downlinkStart,downlinkDispose,downlinkCancel,
    allow:(v:boolean)=>{allowed=v;session.syncNativeAuthority()},owner:(v:boolean)=>{owner=v},witness:()=>{witness=true},active:(v:boolean)=>{active=v;session.syncConversationActivation()}}
}
afterEach(()=>{for(const s of sessions.splice(0))s.close();vi.restoreAllMocks();vi.unstubAllGlobals()})

it('holds new microphone commits until the witnessed full text gets its exact real provider item ACK; no onset/ASR is fabricated',async()=>{
 const f=fixture();await f.ready()
 let admit!:(value:Awaited<ReturnType<NonNullable<typeof f.runtime.admitHousehold>>>)=>void
 vi.mocked(f.runtime.admitHousehold!).mockImplementation(()=>new Promise(done=>{admit=done}))
 const starting=f.session.maybeStartHouseholdInput();await Promise.resolve()
 expect(f.session.nativeAppendAllowed()).toBe(false)
 for(let n=0;n<12;n++)f.session.manualAudio.processFrame(new Float32Array(4096).fill(n<6?.1:0),16000)
 expect(f.sent.some(e=>['input_audio_buffer.append','input_audio_buffer.commit','response.create'].includes(e.type))).toBe(false)
 admit({voice_input_id:'entry-voice',source_kind:'household_local_transcript',raw_audio_available:false,entry_state:'unprocessed'});await starting;await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.type==='conversation.item.create')).toEqual([{type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:entry.transcript}]}}])
 f.emit({type:'conversation.item.created',item:{id:'wrong-item',type:'message',role:'user',content:[{type:'input_text',text:'别的句子'}]}})
 await f.session.nativeSendTail;expect(f.sent.some(e=>e.type==='response.create')).toBe(false)
 f.emit({type:'conversation.item.created',item:{id:'actual-provider-item',type:'message',role:'user',content:[{type:'input_text',text:entry.transcript}]}})
 await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
 f.emit({type:'response.created',response:{id:'entry-response'}})
 f.emit({type:'response.function_call_arguments.done',response_id:'entry-response',name:'learning_prepare_request',call_id:'entry-call',arguments:'{"args":{}}'})
 await vi.waitFor(()=>expect(f.runtime.propose).toHaveBeenCalledTimes(1))
 expect(f.runtime.propose).toHaveBeenCalledWith(expect.objectContaining({voice_input_id:'entry-voice',input_item_id:'actual-provider-item',input_source:'household_local_transcript'}))
 expect(f.runtime.admit).not.toHaveBeenCalled()
})
it.each(['processed','pending_read_only'] as const)('never replays an already %s household input as a fresh model operation',async state=>{
 const f=fixture();await f.ready();vi.mocked(f.runtime.admitHousehold!).mockResolvedValue({...await f.runtime.admitHousehold!(entry,'host-current'),entry_state:state})
 await f.session.maybeStartHouseholdInput();await f.session.nativeSendTail
 expect(f.sent.some(e=>['conversation.item.create','response.create'].includes(e.type))).toBe(false)
})
it('prefers context recovery of the same canonical voice and never sends initial entry plus restored entry twice',async()=>{
 const f=fixture();await f.ready()
 f.session.nativeContext={...context,pending_inputs:[{voice_input_id:'entry-voice',client_action_id:'original-public',created_at:new Date().toISOString(),text:entry.transcript,source_kind:'household_local_transcript'}]}
 await f.session.maybeStartHouseholdInput();await f.session.nativeSendTail
 expect(f.sent.some(e=>e.type==='conversation.item.create')).toBe(false)
 f.witness();vi.mocked(f.runtime.loadContext).mockResolvedValue(f.session.nativeContext)
 await f.session.maybeRestorePersistedInput();await f.session.nativeSendTail
 f.emit({type:'conversation.item.created',item:{id:'restored-real-item',type:'message',role:'user',content:[{type:'input_text',text:entry.transcript}]}});await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.type==='conversation.item.create')).toHaveLength(1);expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
 expect(f.runtime.admit).not.toHaveBeenCalled()
})
it('recovers only one witnessed persisted original per connection without admitting or replaying the next',async()=>{
 const f=fixture(false);await f.ready();f.witness()
 const first={voice_input_id:'canonical-first',client_action_id:'action-first',created_at:'2026-01-01T00:00:00Z',text:'第一句原话'}
 const second={voice_input_id:'canonical-second',client_action_id:'action-second',created_at:'2026-01-01T00:00:01Z',text:'第二句原话'}
 const pending={...context,pending_inputs:[first,second]}
 f.session.nativeContext=pending;vi.mocked(f.runtime.loadContext).mockResolvedValue(pending)
 expect(f.session.nativeAppendAllowed()).toBe(true)
 expect(f.session.nativeRestoreTried).toBe(false)
 expect(f.session.nativeLiveInputObserved).toBe(false)
 await f.session.maybeRestorePersistedInput();await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.type==='conversation.item.create')).toEqual([{type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:first.text}]}}])
 f.emit({type:'conversation.item.created',item:{id:'first-item',type:'message',role:'user',content:[{type:'input_text',text:first.text}]}});await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
 await f.session.maybeRestorePersistedInput();await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.type==='conversation.item.create')).toHaveLength(1)
 expect(pending.pending_inputs).toEqual([first,second]);expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
 // Simulate the durable service marking the first original handled; the new
 // owner receives a fresh context containing the second original only.
 f.session.close();const next=fixture(false);await next.ready();next.witness()
 const remaining={...context,pending_inputs:[second]}
 next.session.nativeContext=remaining;vi.mocked(next.runtime.loadContext).mockResolvedValue(remaining)
 await next.session.maybeRestorePersistedInput();await next.session.nativeSendTail
 expect(next.sent.filter(e=>e.type==='conversation.item.create')).toEqual([{type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:second.text}]}}])
 next.emit({type:'conversation.item.created',item:{id:'second-item',type:'message',role:'user',content:[{type:'input_text',text:second.text}]}});await next.session.nativeSendTail
 expect(next.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
 expect(next.runtime.admit).not.toHaveBeenCalled();expect(next.runtime.propose).not.toHaveBeenCalled()
})
it.each(['owner','consent'] as const)('late initial admission/ACK cannot borrow a changed %s',async mode=>{
 const f=fixture();await f.ready();let resolve!:(value:any)=>void
 vi.mocked(f.runtime.admitHousehold!).mockImplementation(()=>new Promise(done=>{resolve=done}))
 const waiting=f.session.maybeStartHouseholdInput();await Promise.resolve()
 if(mode==='owner')f.owner(false);else f.allow(false)
 resolve({voice_input_id:'entry-voice',source_kind:'household_local_transcript',raw_audio_available:false,entry_state:'unprocessed'});await waiting
 f.emit({type:'conversation.item.created',item:{id:'late-item',type:'message',role:'user',content:[{type:'input_text',text:entry.transcript}]}});await f.session.nativeSendTail
 expect(f.sent.some(e=>['conversation.item.create','response.create'].includes(e.type))).toBe(false)
})
it('handles the first canonical child-safety control locally before any provider text item or natural response',async()=>{
 const f=fixture();await f.ready()
 const control={kind:'child_safety' as const,intent:'urgent_safety_support' as const,response_text:'请先找身边可信任的大人。',agent_turn_id:'safety-turn',safety_event_id:'safety-event'}
 vi.mocked(f.runtime.admitHousehold!).mockResolvedValue({...await f.runtime.admitHousehold!(entry,'host-current'),local_control:control})
 await f.session.maybeStartHouseholdInput();await f.session.nativeSendTail
 expect(f.session.options.callbacks.onLocalSafety).toHaveBeenCalledWith(control,false)
 expect(f.sent.some(e=>['conversation.item.create','response.create'].includes(e.type))).toBe(false);expect(f.runtime.admit).not.toHaveBeenCalled()
})
