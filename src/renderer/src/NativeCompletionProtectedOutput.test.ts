// @vitest-environment node
import { afterEach, expect, it, vi } from 'vitest'
import { QwenRealtimeVoiceSession } from './qwenRealtimeVoice'
import { QwenNativePcmDownlink } from './qwenNativePcmDownlink'
import type { QwenNativeBoundRuntime, QwenNativeRuntimeContext } from './qwenNativeProtocol'
import type { QwenNativeSocketEvent } from '../../shared/qwenNativeSocket'
const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'learning.prepare_request',tool_name:'learning_prepare_request',description:'Prepare a proposed learning goal.',required_arguments:[],arguments_schema:{type:'object',additionalProperties:false,properties:{}}}]}
const context:QwenNativeRuntimeContext={version:1,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[]}
const sessions:any[]=[]
const entry={entry_receipt:{version:1 as const,origin_scope:'unassigned_household' as const,storage_generation:'public-storage',receipt_id:'receipt-public',client_action_id:'local-public',issued_at_ms:1000,transcript_sha256:'a'.repeat(64),model_version:'local-public',source_kind:'household_local_transcript' as const},transcript:'小知，帮我打开错题本'}
function fixture(){
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
  const session=new QwenRealtimeVoiceSession({childName:'Synthetic',nativeRuntime:runtime,nativeAudioAllowed:()=>allowed,
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


it('a completed exact tool output remains queued through protected speech and only flushes once the same response can speak again',async()=>{
 const f=fixture();await f.ready()
 expect(f.session.nativeConversation.startHouseholdInput('public-item','public-voice','公开软件请求',()=>true)).toBe(true)
 expect(f.session.manualAudio.requestRestoredResponse('public-item')).toBe(true);await f.session.nativeSendTail
 f.emit({type:'response.created',response:{id:'public-response'}})
 f.session.setExternalPlaybackActive(true,true)
 f.emit({type:'response.function_call_arguments.done',response_id:'public-response',name:'learning_prepare_request',call_id:'public-call',arguments:'{"args":{}}'})
 await vi.waitFor(()=>expect(f.runtime.propose).toHaveBeenCalledTimes(1))
 f.emit({type:'response.done',response:{id:'public-response',status:'completed'}});await f.session.nativeSendTail
 expect(f.session.nativeTools.has('public-response')).toBe(true)
 expect(f.sent.filter(e=>e.item?.type==='function_call_output')).toHaveLength(0)
 f.session.setExternalPlaybackActive(false,true)
 f.session.flushNativeTools('public-response');await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.item?.type==='function_call_output')).toHaveLength(1)
 expect(f.session.nativeTools.has('public-response')).toBe(false)
})
it('a retired owner cannot flush a retained tool output after protected playback ends',async()=>{
 const f=fixture();await f.ready();f.session.nativeConversation.startHouseholdInput('public-item','public-voice','公开请求',()=>true);f.session.manualAudio.requestRestoredResponse('public-item');await f.session.nativeSendTail;f.emit({type:'response.created',response:{id:'public-response'}})
 f.session.setExternalPlaybackActive(true,true);f.emit({type:'response.function_call_arguments.done',response_id:'public-response',name:'learning_prepare_request',call_id:'public-call',arguments:'{"args":{}}'});await vi.waitFor(()=>expect(f.runtime.propose).toHaveBeenCalledTimes(1));f.emit({type:'response.done',response:{id:'public-response',status:'completed'}})
 f.owner(false);f.session.setExternalPlaybackActive(false,true);f.session.flushNativeTools('public-response');await f.session.nativeSendTail
 expect(f.sent.filter(e=>e.item?.type==='function_call_output')).toHaveLength(0)
})

it('an actual-send protection change retains the public tool result rather than deleting an unsent group',async()=>{
 const f=fixture();await f.ready();f.session.nativeConversation.startHouseholdInput('public-item','public-voice','公开请求',()=>true);f.session.manualAudio.requestRestoredResponse('public-item');await f.session.nativeSendTail;f.emit({type:'response.created',response:{id:'public-response'}})
 f.session.setExternalPlaybackActive(true,true);f.emit({type:'response.function_call_arguments.done',response_id:'public-response',name:'learning_prepare_request',call_id:'public-call',arguments:'{"args":{}}'});await vi.waitFor(()=>expect(f.runtime.propose).toHaveBeenCalledTimes(1));f.emit({type:'response.done',response:{id:'public-response',status:'completed'}})
 f.session.setExternalPlaybackActive(false,true);f.session.flushNativeTools('public-response');f.session.setExternalPlaybackActive(true,true);await f.session.nativeSendTail;await Promise.resolve()
 expect(f.sent.filter(e=>e.item?.type==='function_call_output')).toHaveLength(0);expect(f.session.nativeTools.has('public-response')).toBe(true)
 await vi.waitFor(()=>expect(f.session.nativeTools.get('public-response')?.sending).toBe(false))
 f.session.setExternalPlaybackActive(false,true);f.session.flushNativeTools('public-response');await vi.waitFor(()=>expect(f.sent.filter(e=>e.item?.type==='function_call_output')).toHaveLength(1))
})
