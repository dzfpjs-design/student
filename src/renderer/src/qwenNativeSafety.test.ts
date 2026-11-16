// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'
import {qwenNativeInstructions,isQwenNativeLocalSafetyControl,type QwenNativeBoundRuntime,type QwenNativeLocalSafetyControl} from './qwenNativeProtocol'
import {isExactStandbyPrivacyStop} from './nativeActivationAudio'
const sessions:any[]=[]
const control:QwenNativeLocalSafetyControl={kind:'child_safety',intent:'emotional_distress_support',response_text:'我愿意听你说。我们也可以找一位你信任的大人一起帮忙。',agent_turn_id:'safety-turn',safety_event_id:'safety-event'}
function fixture(safety=true){
 const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'learning.prepare_request',tool_name:'learning_prepare_request',description:'Prepare a goal',required_arguments:[],arguments_schema:{type:'object',properties:{}}}]}
 const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(),loadContext:vi.fn(),admit:vi.fn(async()=>({voice_input_id:'canonical-voice',...(safety?{local_control:control}:{})})),propose:vi.fn(),readReceipt:vi.fn(),recordDialogue:vi.fn()}
 const sent:any[]=[],closeHost=vi.fn(async()=>undefined),sendSocket=vi.fn(async(_id:string,raw:string)=>{sent.push(JSON.parse(raw))})
 vi.stubGlobal('window',{desktop:{closeQwenNativeSocket:closeHost,sendQwenNativeSocket:sendSocket},setInterval,clearInterval,setTimeout,clearTimeout})
 const statuses=vi.fn(),onSafety=vi.fn(),onRecoveryWake=vi.fn()
 const session=new QwenRealtimeVoiceSession({childName:'Synthetic',fullDuplex:true,nativeRuntime:runtime,nativeAudioAllowed:()=>true,
 nativeActivation:{isActive:()=>true,word:()=> '小知',onWake:vi.fn(),onRecoveryWake,recognizeStandby:vi.fn()},
 protectedPlaybackControl:{sessionId:'session',childId:'child',isCurrent:()=>true,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false},
 callbacks:{onStatus:statuses,onLocalSafety:onSafety,captureInputContext:()=>({sessionId:'session',reference:{child_id:'child'},carryoverConfirmationId:null})}}) as any
 const root={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:true}),stop:vi.fn()},cloud={...root,enabled:false,stop:vi.fn()}
 session.localSourceTrack=root;session.audioTrack=cloud;session.nativeSocketOpen=true;session.nativeSocketReady=true;session.nativeHostConnectionId='host';session.nativeCatalog=catalog;session.nativeContext={version:1,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[]}
 session.sessionReady=true;session.microphoneReleased=true;session.prepareNativeSession('supplier')
 const originalSend=session.send.bind(session);session.send=(event:unknown)=>sent.push(event)
 const tap={dispose:vi.fn()},standby={dispose:vi.fn(),resumeAfterTransportRetired:vi.fn(()=>true)};session.protectedControl=tap;session.standbyWake=standby
 const ledger=session.nativeConversation
 const receive=(event:unknown)=>session.receive(JSON.stringify(event))
 const input=async(text:string)=>{for(let i=0;i<6;i++)session.manualAudio.processFrame(new Float32Array(1024).fill(.04),16000);for(let i=0;i<13;i++)session.manualAudio.processFrame(new Float32Array(1024),16000)
 receive({type:'input_audio_buffer.committed',item_id:'input'});receive({type:'conversation.item.input_audio_transcription.completed',item_id:'input',transcript:text});await vi.waitFor(()=>expect(runtime.admit).toHaveBeenCalledTimes(1))}
 sessions.push(session);return {session,runtime,sent,statuses,onSafety,onRecoveryWake,root,cloud,tap,standby,ledger,receive,input,closeHost,sendSocket,realSend:()=>{session.send=originalSend}}
}
afterEach(()=>{for(const s of sessions.splice(0))s.close();vi.useRealTimers();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('durable safety admission stops before native response/proposals and keeps only the same original local recovery tap',async()=>{
 const f=fixture();await f.input('我很难过，想找人说说话。');await vi.waitFor(()=>expect(f.onSafety).toHaveBeenCalledExactlyOnceWith(control,true))
 expect(f.sent.some(e=>e.type==='input_audio_buffer.append')).toBe(true) // Already sent input is never falsely described as local-only.
 expect(f.sent.some(e=>e.type==='response.create')).toBe(false);expect(f.runtime.propose).not.toHaveBeenCalled();expect(f.runtime.recordDialogue).not.toHaveBeenCalled()
 expect(f.ledger.bindResponse('unwanted-response','input')).toBe(false);expect(f.session.nativeConversation).toBeNull();expect(f.session.manualAudio).toBeNull()
 expect(f.session.localWakeRecovery).toBe(true);expect(f.session.closed).toBe(false);expect(f.session.localSourceTrack).toBe(f.root);expect(f.root.stop).not.toHaveBeenCalled();expect(f.tap.dispose).not.toHaveBeenCalled();expect(f.standby.dispose).not.toHaveBeenCalled()
 expect(f.closeHost).toHaveBeenCalledExactlyOnceWith('host');expect(f.statuses.mock.calls.some(([status])=>status.phase==='error')).toBe(false);expect(f.onRecoveryWake).not.toHaveBeenCalled()
 f.receive({type:'response.created',response:{id:'late-cloud-response'}});expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('normal geography input retains free native response creation with no local safety/FAQ route',async()=>{
 const f=fixture(false);await f.input('帮我复习初三地理第一课。');await vi.waitFor(()=>expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1))
 expect(f.onSafety).not.toHaveBeenCalled();expect(f.session.localWakeRecovery).toBe(false);expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('a queued cancel after vendor EOS is dropped at the actual send point without granting source completion',async()=>{
 const f=fixture(false);f.realSend();let release!:()=>void
 f.session.nativeSendTail=new Promise<void>(done=>{release=done});f.session.nativeVendorGeneratingResponseId='ending-response';f.session.activeResponseId='ending-response';f.session.responseActive=true
 f.session.cancelActiveInference();f.receive({type:'response.done',response:{id:'ending-response',status:'completed'}})
 release();await f.session.nativeSendTail
 expect(f.sendSocket).not.toHaveBeenCalled();expect(f.session.nativeVendorGeneratingResponseId).toBeNull();expect(f.runtime.recordDialogue).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('finite safety control and current static child policy remain explicit while entering privacy mode is an exact control only',()=>{
 expect(isQwenNativeLocalSafetyControl(control)).toBe(true);expect(isQwenNativeLocalSafetyControl({...control,intent:'ordinary_chat'})).toBe(false)
 const policy=qwenNativeInstructions();for(const rule of ['不是孩子唯一的朋友','不承诺保密','不索要姓名','可信成人','不以陪伴延长预算','普通聊天和概念交流可以自然回应'])expect(policy).toContain(rule)
 expect(isExactStandbyPrivacyStop('进入隐私模式')).toBe(true);expect(isExactStandbyPrivacyStop('请进入隐私模式')).toBe(true)
 expect(isExactStandbyPrivacyStop('不要进入隐私模式')).toBe(false);expect(isExactStandbyPrivacyStop('为什么进入隐私模式')).toBe(false);expect(isExactStandbyPrivacyStop('进入关闭摄像头')).toBe(false)
})
