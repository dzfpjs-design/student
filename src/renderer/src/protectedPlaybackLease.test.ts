import { protectedPlaybackBarrier } from './protectedPlaybackBarrier'
import {afterEach,describe,it,expect,vi} from 'vitest'
import {getCompanionPlaybackLease,getCompanionPlaybackControlAdmission,speakCompanion,stopCompanionSpeech,companionPlaybackLeaseMatches} from './companionVoice'
import {cancelExactLocalSpeech} from './LocalInputs'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'
import {ProtectedPlaybackControl} from './protectedPlaybackControl'
afterEach(()=>{const token=protectedPlaybackBarrier.token();if(token)protectedPlaybackBarrier.rollback(token);stopCompanionSpeech();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers()})
function sapi(){
 const utterances:any[]=[],synthesis={getVoices:()=>[{localService:true,lang:'zh-CN',name:'Local Chinese'}],cancel:vi.fn(),speak:(u:any)=>{utterances.push(u);u.onstart?.()},addEventListener:vi.fn(),removeEventListener:vi.fn()}
 Object.defineProperty(window,'speechSynthesis',{value:synthesis,configurable:true});vi.stubGlobal('SpeechSynthesisUtterance',class {constructor(public text:string){} onstart?:()=>void;onend?:()=>void});return {utterances,synthesis}
}
describe('actual playback lease and owned single-source RTP split',()=>{
 function tailConnection(attachFails:boolean){
  vi.useFakeTimers();let now=1000;vi.spyOn(performance,'now').mockImplementation(()=>now)
  const clone:any={enabled:true,readyState:'live',getSettings:()=>({echoCancellation:true}),stop:vi.fn(),addEventListener:vi.fn()}
  const track:any={enabled:true,readyState:'live',getSettings:()=>({echoCancellation:true}),getCapabilities:()=>({echoCancellation:[]}),clone:vi.fn(()=>clone),stop:vi.fn(),addEventListener:vi.fn()}
  const stream:any={getTracks:()=>[track],getAudioTracks:()=>[track]},getUserMedia=vi.fn(async()=>stream),sender:any={track:null,replaceTrack:vi.fn(async x=>{sender.track=x})},peerClose=vi.fn()
  Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia},configurable:true});const channel={readyState:'open',addEventListener:vi.fn(),send:vi.fn(),close:vi.fn()}
  vi.stubGlobal('RTCPeerConnection',class {connectionState='connected';iceGatheringState='complete';localDescription={sdp:'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'};addTrack=(x:any)=>{sender.track=x;return sender};addEventListener=vi.fn();createDataChannel=()=>channel;createOffer=async()=>this.localDescription;setLocalDescription=async()=>{};setRemoteDescription=async()=>{};close=peerClose})
  Object.defineProperty(window,'desktop',{value:{negotiateQwenRealtime:vi.fn(async()=>({answerSdp:'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'}))},configurable:true})
  const scope={sessionId:'public-session',childId:'public-child'};protectedPlaybackBarrier.begin(scope,'prior-accepted-cancellation',1,now)
  const old=new ProtectedPlaybackControl({current:()=>null,recognize:vi.fn(),cancelExact:()=>false,holdCloud:()=>{},failClosed:()=>{}});old.dispose()
  const attach=vi.spyOn(ProtectedPlaybackControl.prototype,'attach');if(attachFails)attach.mockRejectedValue(new Error('Public synthetic audio attach failed'));else attach.mockResolvedValue()
  const statuses:any[]=[],session:any=new QwenRealtimeVoiceSession({childName:'public',callbacks:{onStatus:x=>statuses.push(x)},protectedPlaybackControl:{...scope,isCurrent:()=>true,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false}})
  return {session,track,clone,stream,getUserMedia,peerClose,channel,statuses,scope,setNow:(n:number)=>{now=n}}
 }
 it('held-tail reconnect rejects failed actual tap attachment and closes owned source without clearing the barrier',async()=>{
  const f=tailConnection(true)
  try{await expect(f.session.connect()).rejects.toThrow('播放尾部静音');expect(f.statuses.at(-1)?.phase).toBe('error');expect(f.track.stop).toHaveBeenCalledOnce();expect(f.clone.stop).toHaveBeenCalledOnce();expect(protectedPlaybackBarrier.held()).toBe(true)}finally{f.session.close()}
 })
 it('successful held-tail attachment with no PCM frames still closes actual source and RTC at the original deadline',async()=>{
  const f=tailConnection(false)
  try{
   await f.session.connect();f.session.sessionReady=true;await f.session.releaseMicrophoneGate();expect(f.clone.enabled).toBe(false)
   f.setNow(5999);await vi.advanceTimersByTimeAsync(4999);expect(f.track.stop).not.toHaveBeenCalled();f.setNow(6000);await vi.advanceTimersByTimeAsync(1)
   expect(f.statuses.at(-1)?.phase).toBe('error');expect(f.track.stop).toHaveBeenCalledOnce();expect(f.clone.stop).toHaveBeenCalledOnce();expect(f.peerClose).toHaveBeenCalledOnce();expect(f.channel.close).toHaveBeenCalledOnce();expect(protectedPlaybackBarrier.held()).toBe(true);expect(protectedPlaybackBarrier.mayOpen(f.scope,6000)).toBe(true)
  }finally{f.session.close()}
 })
 it('held-tail deadline applies before GUM completes and the late actual source is stopped without starting RTC',async()=>{
  const f=tailConnection(false);let resolve:(x:any)=>void=()=>{};f.getUserMedia.mockImplementation(()=>new Promise(r=>{resolve=r}))
  try{
   const connecting=f.session.connect();f.setNow(6000);await vi.advanceTimersByTimeAsync(5000);expect(f.statuses.at(-1)?.phase).toBe('error');expect(protectedPlaybackBarrier.held()).toBe(true)
   resolve(f.stream);await connecting;expect(f.track.stop).toHaveBeenCalledOnce();expect(f.track.clone).not.toHaveBeenCalled();expect(f.peerClose).not.toHaveBeenCalled();expect(f.getUserMedia).toHaveBeenCalledTimes(1)
  }finally{f.session.close()}
 })
 it('same speechKey on a newer actual SAPI prompt cannot be canceled by old lease',async()=>{
  const f=sapi();await speakCompanion('第一条公开提示。',null,undefined,undefined,{localOnly:true,speechKey:'same',localPlaybackSessionId:'public-session'});const old=getCompanionPlaybackLease()!;expect(old.kind).toBe('local_sapi')
  await speakCompanion('第二条公开提示。',null,undefined,undefined,{localOnly:true,speechKey:'same',localPlaybackSessionId:'public-session'});const next=getCompanionPlaybackLease()!;expect(next.id).not.toBe(old.id);expect(next.generation).toBeGreaterThan(old.generation)
  expect(cancelExactLocalSpeech(old.id,old.generation)).toBe(false);expect(getCompanionPlaybackLease()?.id).toBe(next.id)
  expect(cancelExactLocalSpeech(next.id,next.generation)).toBe(true);expect(getCompanionPlaybackLease()).toBeNull();expect(f.synthesis.cancel).toHaveBeenCalled()
 })
 it('actual onend and invalid source lease forbid a late control result',async()=>{
  const f=sapi();let current=true;await speakCompanion('公开恢复提示。',null,undefined,undefined,{localOnly:true,localPlaybackSessionId:'public-session',isCurrent:()=>current});const lease=getCompanionPlaybackLease()!
  current=false;expect(companionPlaybackLeaseMatches(lease.id,lease.generation)).toBe(false);current=true;f.utterances[0].onend?.();expect(getCompanionPlaybackLease()).toBeNull();expect(cancelExactLocalSpeech(lease.id,lease.generation)).toBe(false)
 })
 it('actual current SAPI lease owns one scoped ledger that ends with the lease and cannot be resurrected',async()=>{
  const f=sapi();await speakCompanion('当前公开播报。',null,undefined,undefined,{localOnly:true,localPlaybackSessionId:'public-session'});const lease=getCompanionPlaybackLease()!
  const get=(child='public-child',session='public-session')=>getCompanionPlaybackControlAdmission(lease.id,lease.generation,session,child)
  const ledger=get()!;expect(ledger).toBeTruthy();expect(get()).toBe(ledger);expect(get('other-child')).toBeNull();expect(get('public-child','other-session')).toBeNull()
  const a=Symbol(),b=Symbol();expect(ledger.admitFrame(a,true,64,1000)).toBe(true);expect(ledger.consume(a,1000)).toBe(true)
  expect(get()!.admitFrame(b,true,64,5000)).toBe(false);f.utterances[0].onend?.();expect(get()).toBeNull();expect(ledger.bind(lease.id,lease.generation,'public-session','public-child')).toBe(false)
  await speakCompanion('下一条公开播报。',null,undefined,undefined,{localOnly:true,localPlaybackSessionId:'public-session'});const next=getCompanionPlaybackLease()!;expect(next.id).not.toBe(lease.id);expect(get()).toBeNull();const fresh=getCompanionPlaybackControlAdmission(next.id,next.generation,'public-session','other-child')!;expect(fresh).not.toBe(ledger);expect(fresh.admitFrame(Symbol(),true,64,6000)).toBe(true)
 })
 it('exhausted shared acoustic recovery prevents the actual Qwen connect from calling getUserMedia',async()=>{
  const scope={sessionId:'public-session',childId:'public-child'},token=protectedPlaybackBarrier.begin(scope,'ended-protected-lease',1,0)!;protectedPlaybackBarrier.tick(5000);protectedPlaybackBarrier.claim(scope,Symbol(),5000);protectedPlaybackBarrier.tick(10000);protectedPlaybackBarrier.claim(scope,Symbol(),10000);protectedPlaybackBarrier.tick(15000)
  Object.defineProperty(window,'desktop',{value:{negotiateQwenRealtime:vi.fn()},configurable:true})
  const getUserMedia=vi.fn(),statuses:any[]=[];Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia},configurable:true})
  const session=new QwenRealtimeVoiceSession({childName:'public',callbacks:{onStatus:x=>statuses.push(x)},protectedPlaybackControl:{...scope,isCurrent:()=>true,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false}})
  await expect(session.connect()).rejects.toThrow('家长');expect(getUserMedia).not.toHaveBeenCalled();expect(statuses.at(-1)?.phase).toBe('error');expect(statuses.at(-1)?.detail).toContain('家长');session.close();protectedPlaybackBarrier.rollback(token)
 })
 it('one source per Qwen connection stays local, and a new child sender cannot bypass or borrow the shared tail',async()=>{
  const clone:any={enabled:true,readyState:'live',getSettings:()=>({echoCancellation:true}),stop:vi.fn(),addEventListener:vi.fn()}
  const track:any={enabled:true,readyState:'live',getSettings:()=>({echoCancellation:true}),getCapabilities:()=>({echoCancellation:['all']}),applyConstraints:vi.fn(async()=>{throw new DOMException('Public synthetic rejected driver mode','OverconstrainedError')}),clone:vi.fn(()=>clone),stop:vi.fn(),addEventListener:vi.fn()}
  const stream:any={getTracks:()=>[track],getAudioTracks:()=>[track]},getUserMedia=vi.fn(async()=>stream),sender:any={track:null,replaceTrack:vi.fn(async x=>{sender.track=x})},addTrack=vi.fn((x:any)=>{sender.track=x;return sender})
  Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia},configurable:true})
  const channel={readyState:'open',addEventListener:vi.fn(),send:vi.fn(),close:vi.fn()}
  vi.stubGlobal('RTCPeerConnection',class {connectionState='connected';iceGatheringState='complete';localDescription={sdp:'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'};addTrack=addTrack;addEventListener=vi.fn();createDataChannel=()=>channel;createOffer=async()=>this.localDescription;setLocalDescription=async()=>{};setRemoteDescription=async()=>{};close=vi.fn()})
  Object.defineProperty(window,'desktop',{value:{negotiateQwenRealtime:vi.fn(async()=>({answerSdp:'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n'}))},configurable:true})
  const attach=vi.spyOn(ProtectedPlaybackControl.prototype,'attach').mockResolvedValue(),dispose=vi.spyOn(ProtectedPlaybackControl.prototype,'dispose')
  const policies:any[]=[];const session:any=new QwenRealtimeVoiceSession({childName:'public',fullDuplex:true,callbacks:{onStatus:()=>{},onEchoCancellationPolicy:p=>policies.push(p)},protectedPlaybackControl:{sessionId:'public-session',childId:'public-child',isCurrent:()=>true,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false}})
  await session.connect();expect(policies).toEqual([{mode:'browser-selected',negotiation:'rejected',acoustic_quality_verified:false}]);expect(getUserMedia).toHaveBeenCalledTimes(1);expect(track.clone).toHaveBeenCalledTimes(1);expect(addTrack.mock.calls[0][0]).toBe(clone);expect(attach).toHaveBeenCalledExactlyOnceWith(stream,expect.any(Function))
  session.sessionReady=true;await session.releaseMicrophoneGate();session.setExternalPlaybackActive(true,true);expect(clone.enabled).toBe(false);expect(track.enabled).toBe(true)
  session.setExternalPlaybackActive(false);expect(clone.enabled).toBe(true);
  let now=1000;vi.spyOn(performance,'now').mockImplementation(()=>now);protectedPlaybackBarrier.begin({sessionId:'public-session',childId:'public-child'},'already-cancelled-lease',1,now)
  session.setExternalPlaybackActive(false);expect(clone.enabled).toBe(false)
  const tap=attach.mock.contexts[0] as ProtectedPlaybackControl;for(let i=0;i<8;i++){now+=64;tap.processFrame(new Float32Array(1024),16000)}
  session.close();expect(protectedPlaybackBarrier.held()).toBe(true);expect(track.stop).toHaveBeenCalled();expect(clone.stop).toHaveBeenCalled()
  const nextClone:any={...clone,enabled:true,stop:vi.fn()},nextTrack:any={...track,enabled:true,stop:vi.fn(),clone:vi.fn(()=>nextClone)}
  getUserMedia.mockResolvedValueOnce({getTracks:()=>[nextTrack],getAudioTracks:()=>[nextTrack]})
  const next:any=new QwenRealtimeVoiceSession({childName:'other-public',fullDuplex:true,callbacks:{onStatus:()=>{}},protectedPlaybackControl:{sessionId:'other-session',childId:'other-child',isCurrent:()=>true,getLease:()=>null,recognize:vi.fn(),cancelExact:()=>false}})
  await next.connect();next.sessionReady=true;await next.releaseMicrophoneGate();expect(getUserMedia).toHaveBeenCalledTimes(2);expect(addTrack.mock.calls[1][0]).toBe(nextClone);expect(nextClone.enabled).toBe(false)
  const nextTap=attach.mock.contexts[1] as ProtectedPlaybackControl;for(let i=0;i<8;i++){now+=64;nextTap.processFrame(new Float32Array(1024),16000)}expect(protectedPlaybackBarrier.held()).toBe(true);expect(nextClone.enabled).toBe(false)
  for(let i=0;i<8;i++){now+=64;nextTap.processFrame(new Float32Array(1024),16000)}expect(protectedPlaybackBarrier.held()).toBe(false);expect(nextClone.enabled).toBe(true);next.close();expect(dispose).toHaveBeenCalledTimes(2);expect(nextTrack.stop).toHaveBeenCalled();expect(nextClone.stop).toHaveBeenCalled()
 })
})
