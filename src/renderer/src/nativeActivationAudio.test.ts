// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {NativeActivationAudioBuffer,registeredWakePrefix,isExactStandbyPrivacyStop} from './nativeActivationAudio'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'
import {ProtectedPlaybackControl} from './protectedPlaybackControl'
import type {QwenNativeBoundRuntime} from './qwenNativeProtocol'
import type {StandbyVoiceInput} from './types'
const sessions:any[]=[]
function fixture(text='小知，帮我复习初三地理第一课'){
 let now=1000,active=false,epoch=0,permit=true,word='小知';vi.spyOn(performance,'now').mockImplementation(()=>now);vi.spyOn(Date,'now').mockImplementation(()=>1000000+now)
 const root={enabled:true,readyState:'live',getSettings:()=>({echoCancellation:true}),stop:vi.fn()} as unknown as MediaStreamTrack
 const cloud={enabled:false,readyState:'live',getSettings:()=>({echoCancellation:true}),stop:vi.fn()} as unknown as MediaStreamTrack
 const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'learning.prepare_request',tool_name:'learning_prepare_request',description:'Prepare an unconfirmed request',required_arguments:[],arguments_schema:{type:'object',properties:{},additionalProperties:false}}]}
 const context={version:1 as const,revision:1,recent_dialogue:[],runtime_facts:{},pending_calls:[]}
 const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(async()=>context),admit:vi.fn(async()=>({voice_input_id:'new-canonical-voice'})),propose:vi.fn(),recordDialogue:vi.fn(async()=>undefined),readReceipt:vi.fn()}
 let resolve!:(value:StandbyVoiceInput)=>void,key='';const recognize=vi.fn((_wav:string,_signal:AbortSignal,id:ReturnType<Crypto['randomUUID']>)=>{key=id;return new Promise<StandbyVoiceInput>(done=>{resolve=done})})
 const sent:any[]=[],onWake=vi.fn((announce?:boolean)=>{active=true}),onPrivacyStop=vi.fn(),onError=vi.fn()
 const old=Object.freeze({child_id:'child',local_recovery_only:true as const,pending_evaluation_turn_id:'old-turn'})
 const capture=vi.fn(()=>({sessionId:'session',reference:old,carryoverConfirmationId:'old-private-carryover'}))
 vi.stubGlobal('window',{desktop:{closeQwenNativeSocket:vi.fn(async()=>undefined),recordRendererDiagnostic:vi.fn(async()=>undefined)},setInterval,clearInterval,setTimeout,clearTimeout})
 const session=new QwenRealtimeVoiceSession({childName:'Synthetic',nativeRuntime:runtime,nativeAudioAllowed:()=>permit,fullDuplex:true,
  nativeActivation:{isActive:()=>active,word:()=>word,onWake,onPrivacyStop,recognizeStandby:recognize},
  protectedPlaybackControl:{sessionId:'session',childId:'child',isCurrent:()=>epoch===0,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false},
  callbacks:{onStatus:vi.fn(),onError,captureInputContext:capture}}) as any
 session.localSourceTrack=root;session.audioTrack=cloud;session.nativeSocketOpen=true;session.nativeSocketReady=true;session.nativeHostConnectionId='host';session.nativeCatalog=catalog;session.nativeContext=context
 session.send=(event:any)=>sent.push(event);session.prepareNativeSession('supplier');session.sessionReady=true;session.microphoneReleased=true
 let tap!:(frame:Float32Array,rate:number)=>void
 vi.spyOn(ProtectedPlaybackControl.prototype,'attach').mockImplementation(async(_stream,onFrame)=>{tap=onFrame!})
 sessions.push(session)
 const attach=()=>session.attachOwnedControlFanout({} as MediaStream,root,cloud)
 const frames=(value=.03,count=38)=>{for(let i=0;i<count;i++){now+=64;tap(new Float32Array(1024).fill(value),16000)}}
 const finish=async()=>{resolve({client_action_id:key,transcript:text,duration_ms:2400,status:'completed',model_version:'synthetic',cloud_used:false,persisted:false});await Promise.resolve();await Promise.resolve()}
 const receive=(event:unknown)=>session.receive(JSON.stringify(event))
 return {session,attach,frames,finish,receive,sent,runtime,onWake,onError,onPrivacyStop,capture,old,root,now:()=>now,privateGate:()=>{session.setExternalPlaybackActive(true,true);session.setExternalPlaybackActive(false,true)},changeOwner:()=>{epoch++},revoke:()=>{permit=false},changeWord:()=>{word='小伴'}}
}
afterEach(()=>{for(const s of sessions.splice(0))s.close();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('same actual fanout retains contiguous pre-wake and pending tail PCM under one original onset identity, then only cloud ASR admits the complete sentence',async()=>{
 const f=fixture();await f.attach();f.frames(.03);expect(f.sent).toEqual([]);const wakeStart=f.now()
 f.frames(.06,12);await f.finish()
 expect(f.onWake).toHaveBeenCalledExactlyOnceWith(false);expect(f.capture).not.toHaveBeenCalled();expect(f.old).toEqual({child_id:'child',local_recovery_only:true,pending_evaluation_turn_id:'old-turn'})
 expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.sent.some(e=>e.type==='input_audio_buffer.append')).toBe(true)
 f.frames(0,13);expect(f.sent.filter(e=>e.type==='input_audio_buffer.commit')).toHaveLength(1)
 const amplitudes=f.sent.filter(e=>e.type==='input_audio_buffer.append').map(e=>{const raw=atob(e.audio);return raw.charCodeAt(0)|(raw.charCodeAt(1)<<8)})
 expect(amplitudes.some(v=>v>900&&v<1100)).toBe(true);expect(amplitudes.some(v=>v>1900&&v<2100)).toBe(true)
 f.receive({type:'input_audio_buffer.committed',item_id:'cloud-actual-item'})
 f.receive({type:'conversation.item.input_audio_transcription.completed',item_id:'cloud-actual-item',transcript:'小知，帮我复习初三地理第一课'})
 await vi.waitFor(()=>expect(f.runtime.admit).toHaveBeenCalledTimes(1))
 const [text,capture,host]=vi.mocked(f.runtime.admit).mock.calls[0]
 expect(text).toBe('小知，帮我复习初三地理第一课');expect(host).toBe('host');expect(capture.reference).toEqual({child_id:'child'});expect(capture.carryoverConfirmationId).toBeNull()
 expect(capture.source).toBe('accepted_onset');expect(capture.capturedAt).toBeLessThan(1000000+wakeStart-1500)
 expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('bare wake keeps the short local acknowledgement and never uploads its activation PCM',async()=>{
 const f=fixture('小智，小知');await f.attach();f.frames();await f.finish()
 expect(f.onWake).toHaveBeenCalledExactlyOnceWith(true);expect(f.sent).toEqual([]);expect(f.runtime.admit).not.toHaveBeenCalled()
})
it('background speech without the registered wake name cannot activate or enter cloud admission',async()=>{
 const f=fixture('电视里正在说话');await f.attach();f.frames();await f.finish()
 expect(f.onWake).not.toHaveBeenCalled();expect(f.sent).toEqual([])
 expect(f.runtime.admit).not.toHaveBeenCalled();expect(f.runtime.propose).not.toHaveBeenCalled()
})
it.each(['private','owner','word','consent'])('late wake after %s change cannot upload the former activation buffer',async change=>{
 const f=fixture();await f.attach();f.frames()
 if(change==='private')f.privateGate();if(change==='owner')f.changeOwner();if(change==='word')f.changeWord();if(change==='consent')f.revoke()
 await f.finish();expect(f.sent.some(e=>e.type==='input_audio_buffer.append')).toBe(false);expect(f.runtime.admit).not.toHaveBeenCalled()
})
it('an overflow activates with explicit repeat guidance and no partial PCM or local transcript admission',async()=>{
 const f=fixture();await f.attach();f.frames(.03,200);await f.finish()
 expect(f.onWake).toHaveBeenCalledExactlyOnceWith(true);expect(f.onError).toHaveBeenCalledWith('唤醒前这句话没有保留完整，请再说一次。')
 expect(f.sent).toEqual([]);expect(f.runtime.admit).not.toHaveBeenCalled()
})
it.each(['小知，隐私模式','小知，不要听了','小智，请帮我关闭摄像头一下'])('registered privacy control %s stops locally before activation or any upload',async text=>{
 const f=fixture(text);await f.attach();f.frames();await f.finish()
 expect(f.onPrivacyStop).toHaveBeenCalledTimes(1);expect(f.onWake).not.toHaveBeenCalled();expect(f.sent.filter(e=>e.type!=='session.finish')).toEqual([]);expect(f.runtime.admit).not.toHaveBeenCalled()
})
it('name inside an ordinary phrase cannot activate; a real prefixed question containing privacy words remains ordinary cloud dialogue',async()=>{
 expect(registeredWakePrefix('电视里提到小知，复习地理','小知').matched).toBe(false)
 expect(registeredWakePrefix('喂，小智，为什么总说隐私模式','小知')).toEqual({matched:true,command:'为什么总说隐私模式'})
 expect(isExactStandbyPrivacyStop('为什么总说隐私模式')).toBe(false);expect(isExactStandbyPrivacyStop('别停止监听')).toBe(false)
 expect(registeredWakePrefix('你好，帮我复习','你好')).toEqual({matched:true,command:'帮我复习'})
 const f=fixture('小知，为什么总说隐私模式');await f.attach();f.frames();await f.finish()
 expect(f.onPrivacyStop).not.toHaveBeenCalled();expect(f.onWake).toHaveBeenCalledWith(false);expect(f.sent.some(e=>e.type==='input_audio_buffer.append')).toBe(true)
})
it('a second real utterance replaces its own token; an older inference can never take the newer PCM',()=>{
 let now=1000;const root={} as MediaStreamTrack,owner={sessionId:'s',childId:'c',root,word:'小知',epoch:1}
 const b=new NativeActivationAudioBuffer({current:()=>owner,standby:()=>true,now:()=>now,capture:(_owner,at)=>({sessionId:'s',reference:{child_id:'c'},carryoverConfirmationId:null,source:'accepted_onset',capturedAt:at,localInputId:crypto.randomUUID()})})
 const feed=(value:number,n:number)=>{for(let i=0;i<n;i++){now+=64;b.processFrame(new Float32Array(1024).fill(value),16000)}}
 feed(.03,5);const old=b.token();feed(0,13);feed(.06,5);expect(b.token()).not.toBe(old);expect(b.take(old)).toBeNull()
 const latest=b.take(b.token())!;expect(latest.capture.localInputId).toBeTruthy();expect(b.owns(latest)).toBe(true)
 now+=501;expect(b.owns(latest)).toBe(false);for(const frame of latest.frames)frame.fill(0);b.dispose()
})
