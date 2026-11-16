// @vitest-environment jsdom
import {act,cleanup,render} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceInput} from './QwenRealtimeVoiceInput'
import {resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import type {QwenNativeBoundRuntime,QwenNativeRuntimeContext} from './qwenNativeProtocol'
const h=vi.hoisted(()=>({instances:[] as any[],register:vi.fn(),release:vi.fn(),renew:vi.fn(),sent:[] as any[]}))
vi.mock('./qwenRealtimeVoice',async original=>{const actual=await original<typeof import('./qwenRealtimeVoice')>();return {...actual,QwenRealtimeVoiceSession:class extends actual.QwenRealtimeVoiceSession{
 currentOptions:ConstructorParameters<typeof actual.QwenRealtimeVoiceSession>[0]
 root={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:true}),stop:vi.fn()}
 constructor(options:ConstructorParameters<typeof actual.QwenRealtimeVoiceSession>[0]){super(options);this.currentOptions=options;h.instances.push(this)}
 async connect(){
  const state=this as any,options=this.currentOptions,runtime=options.nativeRuntime!
  state.localSourceTrack=this.root;state.stream={getTracks:()=>[this.root]};state.audioTrack={...this.root,enabled:false,stop:vi.fn()}
  state.nativeHostConnectionId='host-'+h.instances.length;state.nativeSocketOpen=true;state.nativeSocketReady=true;state.microphoneReleased=true;state.sessionReady=true
  state.nativeCatalog=await runtime.loadCatalog(state.nativeHostConnectionId);state.nativeContext=await runtime.loadContext(state.nativeHostConnectionId)
  state.prepareNativeSession('supplier-'+h.instances.length)
  await options.callbacks.onMicrophoneOpen?.(this.root as unknown as MediaStreamTrack)
  options.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'listening',speechActive:false,pendingUtterances:0,recoverable:true})
 }
}}})
vi.mock('./mediaDevices',()=>({mediaDeviceRegistration:vi.fn(async()=>({systemIdHash:'public-synthetic',capabilities:{}}))}))
vi.mock('./api',()=>({localApi:{registerDevice:h.register,releaseDeviceLease:h.release,renewDeviceLease:h.renew}}))
vi.mock('./companionVoice',async original=>({...await original<typeof import('./companionVoice')>(),getCompanionVoiceState:()=> 'idle',getCompanionPlaybackLocalOnly:()=>true,subscribeCompanionVoice:()=>()=>{},stopCompanionSpeech:vi.fn(),speakCompanion:vi.fn()}))
const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'workspace.navigate',tool_name:'workspace_navigate',description:'Navigate the existing workspace.',required_arguments:[],arguments_schema:{type:'object',properties:{}}}]}
const entry={entry_receipt:{version:1 as const,origin_scope:'unassigned_household' as const,storage_generation:'public-storage',receipt_id:'public-receipt',client_action_id:'local-asr',issued_at_ms:12345,transcript_sha256:'a'.repeat(64),model_version:'local',source_kind:'household_local_transcript' as const},transcript:'小知帮我打开错题本'}
const context:QwenNativeRuntimeContext={version:1,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[],pending_inputs:[{voice_input_id:'canonical-original',client_action_id:'original-asr-action',created_at:'2026-10-29T00:00:00Z',text:entry.transcript,source_kind:'household_local_transcript'}]}
const advance=async(ms:number)=>act(async()=>{await vi.advanceTimersByTimeAsync(ms)})
beforeEach(()=>{vi.useFakeTimers();vi.setSystemTime(100000);h.instances=[];h.sent=[];h.register.mockReset().mockResolvedValue({lease_id:'actual-component-lease'});h.release.mockReset().mockResolvedValue({});h.renew.mockReset().mockResolvedValue({});vi.stubGlobal('desktop',{closeQwenNativeSocket:vi.fn(async()=>undefined),sendQwenNativeSocket:vi.fn(async(_host:string,raw:string)=>{h.sent.push(JSON.parse(raw))}),recordRendererDiagnostic:vi.fn(async()=>undefined)});resetVoiceCaptureCoordinatorForTests()})
afterEach(()=>{cleanup();resetVoiceCaptureCoordinatorForTests();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals()})
function props(){
 const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(async()=>context),admit:vi.fn(),admitHousehold:vi.fn<NonNullable<QwenNativeBoundRuntime['admitHousehold']>>(async()=>({voice_input_id:'canonical-original',source_kind:'household_local_transcript',raw_audio_available:false,entry_state:'unprocessed'})),propose:vi.fn(),readReceipt:vi.fn(),recordDialogue:vi.fn()}
 return {active:true,audioAllowed:true,sessionId:'session',childId:'child',childName:'Synthetic',wakeWord:'小知',initialConversationActive:true,householdEntry:entry,nativeRuntime:runtime,onTurn:vi.fn(),onStatus:vi.fn(),onHouseholdEntryConsumed:vi.fn()}
}
it('actual canonical household consumption preserves current recovery authority across a real component transport replacement without a fabricated onset or manual witness',async()=>{
 const p=props();render(<QwenRealtimeVoiceInput {...p}/>);await advance(0)
 const first=h.instances[0]
 expect(first.currentOptions.nativeActivation.canRecoverPersisted()).toBe(false)
 await act(async()=>{await first.maybeStartHouseholdInput()})
 expect(p.nativeRuntime.admitHousehold).toHaveBeenCalledTimes(1)
 expect(p.onHouseholdEntryConsumed).toHaveBeenCalledTimes(1)
 expect(first.currentOptions.nativeActivation.canRecoverPersisted()).toBe(true)
 expect(h.sent.some(e=>e.type==='conversation.item.create')).toBe(false)
 await act(async()=>first.currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'连接失败'}));await advance(1001)
 const next=h.instances[1];expect(next.currentOptions.householdEntry).toBeUndefined()
 await act(async()=>{await next.maybeRestorePersistedInput();await next.nativeSendTail})
 expect(h.sent.filter(e=>e.type==='conversation.item.create')).toEqual([{type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:entry.transcript}]}}])
 expect(p.nativeRuntime.admitHousehold).toHaveBeenCalledTimes(1);expect(p.nativeRuntime.admit).not.toHaveBeenCalled();expect(p.nativeRuntime.propose).not.toHaveBeenCalled()
})
it.each(['child','session','consent','connection'] as const)('old consumed callback cannot grant recovery after %s changes',async change=>{
 const p=props(),view=render(<QwenRealtimeVoiceInput {...p}/>);await advance(0);const old=h.instances[0]
 if(change==='child')view.rerender(<QwenRealtimeVoiceInput {...p} childId='other-child'/>);if(change==='session')view.rerender(<QwenRealtimeVoiceInput {...p} sessionId='other-session'/>);if(change==='consent')view.rerender(<QwenRealtimeVoiceInput {...p} audioAllowed={false}/>);
 if(change==='connection'){await act(async()=>old.currentOptions.callbacks.onStatus({runtime:'qwen_omni_realtime',phase:'error',speechActive:false,pendingUtterances:0,recoverable:true,detail:'连接失败'}));await advance(1001)}else await advance(0)
 await act(async()=>old.currentOptions.onHouseholdEntryConsumed(true))
 expect(p.onHouseholdEntryConsumed).not.toHaveBeenCalled();expect(h.instances.at(-1).currentOptions.nativeActivation.canRecoverPersisted()).toBe(false)
})
it('safety/processed/readonly consumption does not turn into a new recovery execution grant',async()=>{
 const p=props();render(<QwenRealtimeVoiceInput {...p}/>);await advance(0);const current=h.instances[0]
 await act(async()=>current.currentOptions.onHouseholdEntryConsumed(false))
 expect(current.currentOptions.nativeActivation.canRecoverPersisted()).toBe(false);expect(p.onHouseholdEntryConsumed).toHaveBeenCalledTimes(1)
 expect(h.sent).toEqual([]);expect(p.nativeRuntime.propose).not.toHaveBeenCalled()
})
