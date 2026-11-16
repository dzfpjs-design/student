import { ProtectedPlaybackBarrier } from './protectedPlaybackBarrier'
import { PlaybackControlAdmission } from './playbackControlAdmission'
import {describe,it,expect,vi} from 'vitest'
import {ProtectedPlaybackControl,encodeControlWav,MAX_LOCAL_CONTROL_SEGMENTS,type LocalPlaybackControlPayload,type LocalPlaybackControlResult} from './protectedPlaybackControl'
function fixture({mode='all',kind='local_sapi',text='现在继续原来保存的任务。',attention=undefined as string|undefined}={}){
 let now=1000
 const local={enabled:true,readyState:'live',getSettings:()=>({echoCancellation:mode})} as unknown as MediaStreamTrack
 const cloud={enabled:false,readyState:'live'} as MediaStreamTrack
 let owner:any={inputKind:'realtime',sessionId:'public-session',childId:'public-child',lease:{id:'00000000-0000-4000-8000-000000000001',generation:1,sessionId:'public-session',kind,text},localTrack:local,cloudTrack:cloud}
 const recognize=vi.fn(async (_session:string,payload:LocalPlaybackControlPayload):Promise<LocalPlaybackControlResult>=>({status:'recognized',control:'stop_playback',playback_lease_id:payload.playback_lease_id,playback_generation:payload.playback_generation,capture_epoch:payload.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false}))
 const hold=vi.fn(),cancel=vi.fn(()=>{owner=null;return true}),fail=vi.fn(),woke=vi.fn()
 let capture={inputKind:'realtime' as const,sessionId:owner.sessionId,childId:owner.childId,localTrack:local,cloudTrack:cloud}
 const barrier=new ProtectedPlaybackBarrier()
 const ledger=new PlaybackControlAdmission(owner.lease.id,owner.lease.generation,owner.sessionId)
 const build=()=>new ProtectedPlaybackControl({current:()=>owner,capture:()=>capture,barrier,recognize,cancelExact:cancel,holdCloud:hold,failClosed:fail,now:()=>now,
   attentionWakeWord:()=>attention,onAttentionWake:woke,
   admission:o=>ledger.bind(o.lease.id,o.lease.generation,o.sessionId,o.childId)?ledger:null})
 let tap=build()
 const reconnect=()=>{tap.dispose();capture={...capture,localTrack:{...capture.localTrack},cloudTrack:{...capture.cloudTrack}};if(owner)owner={...owner,...capture};tap=build()}
 const frames=(voiced:boolean,count:number)=>{for(let i=0;i<count;i++){tap.processFrame(new Float32Array(1024).fill(voiced ? 0.05 : 0),16000);now+=64}}
 const utterance=()=>{frames(true,3);frames(false,5)}
 return {get tap(){return tap},barrier,reconnect,recognize,cancel,hold,fail,woke,frames,utterance,local,cloud,setAttention:(x:string|undefined)=>attention=x,setOwner:(x:any)=>{owner=x;if(x)capture={...capture,...x}},getOwner:()=>owner,setNow:(x:number)=>now=x}
}
describe('protected local control, never an answer or cloud capture',()=>{
 it('attention acknowledges once immediately after exact cancel while the quiet barrier stays held',async()=>{
  const f=fixture({mode:true as unknown as string,attention:'小知'}),real=f.recognize.getMockImplementation()!
  f.recognize.mockImplementation(async(s,p)=>({...await real(s,p),control:'wake_companion'}))
  f.woke.mockImplementation(()=>{expect(f.barrier.held()).toBe(true);expect(f.hold).toHaveBeenLastCalledWith(true)})
  f.utterance();await Promise.resolve();await Promise.resolve()
  expect(f.recognize.mock.calls[0][1].attention_wake_word).toBe('小知');expect(f.cancel).toHaveBeenCalledOnce();expect(f.woke).toHaveBeenCalledOnce()
  expect(f.cloud.enabled).toBe(false);expect(f.barrier.held()).toBe(true)
  f.frames(true,15);expect(f.woke).toHaveBeenCalledOnce();expect(f.barrier.held()).toBe(true)
  f.frames(false,24);expect(f.barrier.held()).toBe(false);expect(f.woke).toHaveBeenCalledOnce();f.tap.dispose()
 })
 it.each(['word','lease','generation','child','session','track','dispose'])('stale attention result after %s cannot cancel or acknowledge',async(kind)=>{
  const f=fixture({attention:'小知'});let resolve:(v:any)=>void=()=>{}
  f.recognize.mockImplementation((_s,p)=>new Promise(r=>{resolve=x=>r({...x,playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch})}))
  f.utterance();const owner=f.getOwner()
  if(kind==='word')f.setAttention('小伴');else if(kind==='dispose')f.tap.dispose();else f.setOwner({...owner,...(kind==='child'?{childId:'other'}:kind==='session'?{sessionId:'other'}:kind==='track'?{localTrack:{...f.local}}:{}),lease:{...owner.lease,...(kind==='lease'?{id:'new'}:kind==='generation'?{generation:2}:{})}})
  resolve({status:'recognized',control:'wake_companion',cloud_used:false,persisted:false,learning_evidence:false});await Promise.resolve();await Promise.resolve()
  expect(f.cancel).not.toHaveBeenCalled();expect(f.woke).not.toHaveBeenCalled();f.tap.dispose()
 })
 it('a replacement output started by cancellation is never taken over by the old attention result',async()=>{
  const f=fixture({attention:'小知'}),real=f.recognize.getMockImplementation()!,owner=f.getOwner()
  f.recognize.mockImplementation(async(s,p)=>({...await real(s,p),control:'wake_companion'}))
  f.cancel.mockImplementation(()=>{f.setOwner({...owner,lease:{...owner.lease,id:'new-output',generation:2}});return true})
  f.utterance();await Promise.resolve();await Promise.resolve();expect(f.cancel).toHaveBeenCalledOnce();expect(f.woke).not.toHaveBeenCalled();expect(f.getOwner().lease.id).toBe('new-output');f.tap.dispose()
 })
 it.each(['小知，现在继续。','小智现在继续。'])('a reflected name in %s disables wake only and preserves original stop',async text=>{
  const f=fixture({attention:'小知',text});f.utterance();await Promise.resolve();await Promise.resolve()
  expect(f.recognize.mock.calls[0][1]).not.toHaveProperty('attention_wake_word');expect(f.cancel).toHaveBeenCalledOnce();expect(f.woke).not.toHaveBeenCalled();f.tap.dispose()
 })
 it('a forged attention result without a registered word never cancels playback',async()=>{
  const f=fixture(),real=f.recognize.getMockImplementation()!;f.recognize.mockImplementation(async(s,p)=>({...await real(s,p),control:'wake_companion'}))
  f.utterance();await Promise.resolve();await Promise.resolve();expect(f.cancel).not.toHaveBeenCalled();expect(f.woke).not.toHaveBeenCalled();f.tap.dispose()
 })
 it.each(['source','processor','gain','resume'])('partial attach %s failure closes its actual owned context exactly once',async(stage)=>{
  const f=fixture(),close=vi.fn(async()=>{}),source={connect:vi.fn(),disconnect:vi.fn()},processor={connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null},gain={gain:{value:0},connect:vi.fn(),disconnect:vi.fn()}
  const build=(at:string,value:any)=>{if(stage===at)throw new Error('Public synthetic '+at+' failure');return value}
  vi.stubGlobal('AudioContext',class {state=stage==='resume'?'suspended':'running';sampleRate=16000;destination={};close=close;createMediaStreamSource=()=>build('source',source);createScriptProcessor=()=>build('processor',processor);createGain=()=>build('gain',gain);resume=async()=>{throw new Error('Public synthetic resume failure')}})
  try{await expect(f.tap.attach({} as MediaStream)).rejects.toThrow('Public synthetic');expect(close).toHaveBeenCalledOnce();f.tap.dispose();expect(close).toHaveBeenCalledOnce();expect(f.cloud.enabled).toBe(false);expect(f.recognize).not.toHaveBeenCalled()}finally{f.tap.dispose();vi.unstubAllGlobals()}
 })
 it('segments locally, invokes bound control API once, cancels exact lease then holds acoustic tail',async()=>{
  const f=fixture();f.utterance();await Promise.resolve();await Promise.resolve()
  expect(f.recognize).toHaveBeenCalledTimes(1);expect(f.cancel).toHaveBeenCalledExactlyOnceWith('00000000-0000-4000-8000-000000000001',1);expect(f.hold).toHaveBeenCalledWith(true);expect(f.cloud.enabled).toBe(false);expect(f.local.enabled).toBe(true)
  const payload=f.recognize.mock.calls[0][1];expect(payload.current_playback_text).toBe('现在继续原来保存的任务。');expect(payload.source_kind).toBe('local_sapi');expect(payload).not.toHaveProperty('question_reference')
  f.frames(false,16);expect(f.hold).toHaveBeenLastCalledWith(false);f.tap.dispose()
 })
 it.each(['browser-selected',false,'unknown'])('disabled/unknown AEC=%s grants no local playback capture',async(mode)=>{
  const f=fixture({mode:mode as string});f.utterance();await Promise.resolve();expect(f.recognize).not.toHaveBeenCalled();expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
 it.each([true,'remote-only'])('effective AEC=%s permits bound local stop only while cloud remains disabled',async(mode)=>{
  const f=fixture({mode:mode as string});f.utterance();await Promise.resolve();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(1);expect(f.cancel).toHaveBeenCalledTimes(1);expect(f.cloud.enabled).toBe(false)
  const request=f.recognize.mock.calls[0][1];expect(request.source_kind).toBe('local_sapi');expect(request.current_playback_text).toBe('现在继续原来保存的任务。');expect(request).not.toHaveProperty('question_reference');f.tap.dispose()
 })
 it.each(['local_pcm','managed','unverified_sapi','preparing'])('no known verified reference for %s grants no ASR',async kind=>{
  const f=fixture({kind});f.utterance();await Promise.resolve();expect(f.recognize).not.toHaveBeenCalled();f.tap.dispose()
 })
 it.each(['lease','generation','child','session','track','dispose'])('late result cannot cancel after %s changes',async kind=>{
  const f=fixture();let resolve:(x:any)=>void=()=>{};f.recognize.mockImplementation((_s,p)=>new Promise(r=>{resolve=r}));f.utterance();const owner=f.getOwner(),payload=f.recognize.mock.calls[0][1]
  if(kind==='dispose')f.tap.dispose();else f.setOwner({...owner,...(kind==='child'?{childId:'other'}:kind==='session'?{sessionId:'other'}:kind==='track'?{localTrack:{...f.local}}:{}),lease:{...owner.lease,...(kind==='lease'?{id:'new'}:kind==='generation'?{generation:2}:{})}})
  resolve({status:'recognized',control:'stop_playback',playback_lease_id:payload.playback_lease_id,playback_generation:payload.playback_generation,capture_epoch:payload.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false});await Promise.resolve();await Promise.resolve();expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
 it.each(['not_control','echo_rejected','local_recognition_unavailable','timed_out'])('%s never stops or automatically replays a consumed segment',async status=>{
  const f=fixture();f.recognize.mockResolvedValue({status,control:null} as any);f.utterance();await Promise.resolve();await Promise.resolve();f.utterance();expect(f.recognize).toHaveBeenCalledTimes(1);expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
 it.each(['not_control','echo_rejected','busy','timed_out'])('%s first segment permits a later natural stop while keeping the same lease',async status=>{
  const f=fixture();const real=f.recognize.getMockImplementation()!;f.recognize.mockImplementationOnce(async(_s,p)=>({...await real(_s,p),status,control:null}))
  f.utterance();await Promise.resolve();await Promise.resolve();expect(f.cancel).not.toHaveBeenCalled()
  f.frames(false,18);f.frames(true,4);f.frames(false,5);await Promise.resolve();await Promise.resolve()
  expect(f.recognize).toHaveBeenCalledTimes(2);expect(f.cancel).toHaveBeenCalledTimes(1)
  const a=f.recognize.mock.calls[0][1],b=f.recognize.mock.calls[1][1];expect(b.playback_lease_id).toBe(a.playback_lease_id);expect(b.capture_epoch).toBeGreaterThan(a.capture_epoch);expect(b.wav_base64).not.toBe(a.wav_base64);f.tap.dispose()
 })
 it('a long truncated segment must become quiet before a fresh short stop is captured',async()=>{
  const f=fixture();f.frames(true,55);expect(f.recognize).not.toHaveBeenCalled();f.frames(true,20);expect(f.recognize).not.toHaveBeenCalled()
  f.frames(false,18);f.utterance();await Promise.resolve();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(1);expect(f.cancel).toHaveBeenCalledTimes(1);f.tap.dispose()
 })
 it('same lease has a finite natural segment budget and never starts concurrent decoders or replays old PCM',async()=>{
  const f=fixture();let resolve:(x:any)=>void=()=>{};f.recognize.mockImplementationOnce((_s,p)=>new Promise(r=>{resolve=x=>r({...x,playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch})}))
  f.utterance();f.frames(false,20);f.utterance();expect(f.recognize).toHaveBeenCalledTimes(1)
  resolve({status:'not_control',control:null,cloud_used:false,persisted:false,learning_evidence:false});await Promise.resolve();await Promise.resolve();f.recognize.mockResolvedValue({status:'not_control',control:null} as any)
  for(let i=1;i<MAX_LOCAL_CONTROL_SEGMENTS;i++){f.frames(false,18);f.utterance();await Promise.resolve();await Promise.resolve()}
  expect(f.recognize).toHaveBeenCalledTimes(MAX_LOCAL_CONTROL_SEGMENTS);f.frames(false,18);f.utterance();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(MAX_LOCAL_CONTROL_SEGMENTS);expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
 it('elapsed retry time with continuous sound never starts midway or replays the rejected segment',async()=>{
  const f=fixture();f.recognize.mockResolvedValue({status:'echo_rejected',control:null} as any);f.utterance();await Promise.resolve();await Promise.resolve();f.frames(true,40);expect(f.recognize).toHaveBeenCalledTimes(1)
  f.frames(false,18);f.utterance();await Promise.resolve();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(2);expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
 it('twelve consumed segments remain exhausted after same-lease reconnect with fresh tracks and tap',async()=>{
  const f=fixture();f.recognize.mockResolvedValue({status:'not_control',control:null} as any)
  for(let i=0;i<MAX_LOCAL_CONTROL_SEGMENTS;i++){if(i)f.frames(false,18);f.utterance();await Promise.resolve();await Promise.resolve()}
  expect(f.recognize).toHaveBeenCalledTimes(MAX_LOCAL_CONTROL_SEGMENTS);const old=f.getOwner();f.reconnect();expect(f.getOwner().lease).toBe(old.lease);expect(f.getOwner().localTrack).not.toBe(old.localTrack)
  f.frames(false,18);f.utterance();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(MAX_LOCAL_CONTROL_SEGMENTS);expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
 it('reconnecting during continuous sound preserves the consumed rearm and only later quiet permits new audio',async()=>{
  const f=fixture();f.recognize.mockResolvedValueOnce({status:'echo_rejected',control:null} as any);f.utterance();await Promise.resolve();await Promise.resolve();f.reconnect();f.frames(true,35)
  expect(f.recognize).toHaveBeenCalledTimes(1);f.frames(false,18);f.utterance();await Promise.resolve();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(2);expect(f.cancel).toHaveBeenCalledTimes(1);f.tap.dispose()
 })
 it('new source epoch cannot borrow partially observed quiet from the prior tap',async()=>{
  const f=fixture();f.recognize.mockResolvedValue({status:'not_control',control:null} as any);f.utterance();await Promise.resolve();await Promise.resolve();f.frames(false,4);f.reconnect();f.setNow(9000);f.frames(false,1);f.utterance();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(1)
  f.frames(false,18);f.utterance();await Promise.resolve();await Promise.resolve();expect(f.recognize).toHaveBeenCalledTimes(2);f.tap.dispose()
 })
 it('shared ledger preserves the minimum interval and permanently rejects a disposed lease',()=>{
  const ledger=new PlaybackControlAdmission('public-id',1,'public-session'),a=Symbol(),b=Symbol();expect(ledger.bind('public-id',1,'public-session','public-child')).toBe(true)
  expect(ledger.admitFrame(a,true,64,1000)).toBe(true);expect(ledger.consume(a,1000)).toBe(true)
  expect(ledger.admitFrame(b,false,320,1700)).toBe(false);expect(ledger.admitFrame(b,true,64,1701)).toBe(false);expect(ledger.consume(b,1701)).toBe(false)
  expect(ledger.admitFrame(b,false,320,1800)).toBe(false);expect(ledger.admitFrame(b,true,64,1801)).toBe(true)
  expect(ledger.bind('public-id',1,'public-session','other-child')).toBe(false);ledger.dispose();expect(ledger.bind('public-id',1,'public-session','public-child')).toBe(false);expect(ledger.admitFrame(b,true,64,9999)).toBe(false)
 })
 it('accepted cancellation tail survives tap replacement and requires a full new 850ms quiet observation',async()=>{
  const f=fixture();f.utterance();await Promise.resolve();await Promise.resolve();expect(f.barrier.held()).toBe(true);f.frames(false,8);f.reconnect();f.frames(false,6)
  expect(f.barrier.held()).toBe(true);expect(f.hold).toHaveBeenLastCalledWith(true);f.frames(false,9);expect(f.barrier.held()).toBe(false);expect(f.hold).toHaveBeenLastCalledWith(false);f.tap.dispose()
 })
 it('reconnection preserves original five-second tail deadline and timeout never releases input',async()=>{
  const f=fixture();f.utterance();await Promise.resolve();await Promise.resolve();f.setNow(6000);f.reconnect();f.frames(true,1);f.setNow(6513);f.frames(true,1)
  expect(f.fail).toHaveBeenCalledTimes(1);expect(f.barrier.held()).toBe(true);expect(f.hold).toHaveBeenLastCalledWith(true)
  f.reconnect();f.frames(false,15);expect(f.barrier.held()).toBe(false);expect(f.fail).toHaveBeenCalledTimes(1);expect(f.recognize).toHaveBeenCalledTimes(1);f.tap.dispose()
 })
 it('two failed quiet-only recovery windows exhaust automatic opening until an explicit retry, which still needs real quiet',async()=>{
  const f=fixture();f.utterance();await Promise.resolve();await Promise.resolve();f.setNow(7000);f.frames(true,1)
  for(const t of [13000,19000]){f.reconnect();f.frames(true,1);f.setNow(t);f.frames(true,1)}
  expect(f.fail).toHaveBeenCalledTimes(3);const scope={sessionId:'public-session',childId:'public-child'};expect(f.barrier.mayOpen(scope,19000)).toBe(false);expect(f.barrier.held()).toBe(true)
  f.barrier.explicitRetry(scope,19000);expect(f.barrier.held()).toBe(true);expect(f.barrier.mayOpen(scope,19000)).toBe(true);f.reconnect();f.frames(false,15);expect(f.barrier.held()).toBe(false);expect(f.recognize).toHaveBeenCalledTimes(1);f.tap.dispose()
 })
 it('unclaimed foreign or retired child frames cannot credit quiet after a new child owns observation',()=>{
  const barrier=new ProtectedPlaybackBarrier(),a={sessionId:'public-session',childId:'public-child'},b={sessionId:'other-session',childId:'other-child'},epochA=Symbol(),epochB=Symbol()
  barrier.begin(a,'owned',1,1000);barrier.claim(a,epochA,1000);barrier.observe(a,epochA,false,500,1500)
  expect(barrier.observe(b,epochB,false,900,1900)).toBe('held');expect(barrier.claim(b,epochB,1900)).toBe(true)
  expect(barrier.observe(a,epochA,false,900,2200)).toBe('held');expect(barrier.observe(b,epochB,false,500,2400)).toBe('held')
  expect(barrier.observe(b,epochB,false,500,2900)).toBe('held');expect(barrier.observe(b,epochB,false,64,3250)).toBe('released')
 })
 it('original expiry is recorded when the old tap is gone and another child opens bounded recovery',()=>{
  const original=Object.getOwnPropertyDescriptor(window,'desktop'),recordRendererDiagnostic=vi.fn(async()=>({}))
  Object.defineProperty(window,'desktop',{configurable:true,value:{recordRendererDiagnostic}})
  try{
   const barrier=new ProtectedPlaybackBarrier(),a={sessionId:'public-session',childId:'public-child'},b={sessionId:'other-session',childId:'other-child'}
   barrier.begin(a,'owned',1,1000);expect(barrier.reserveOpen(b,6000)).toBe(true);expect(barrier.held()).toBe(true)
   expect(recordRendererDiagnostic).toHaveBeenCalledExactlyOnceWith({event:'voice.capture.protected_tail_window_failed',level:'warn',details:{window:'initial',recovery_windows:0}})
   barrier.tick(11000);barrier.tick(12000);expect(recordRendererDiagnostic).toHaveBeenCalledTimes(2);expect(barrier.held()).toBe(true)
  }finally{if(original)Object.defineProperty(window,'desktop',original);else Reflect.deleteProperty(window,'desktop')}
 })
 it('failed exact cancellation rolls back only its newly created barrier',async()=>{
  const f=fixture();f.cancel.mockImplementation(()=>false);f.utterance();await Promise.resolve();await Promise.resolve();expect(f.barrier.held()).toBe(false);expect(f.hold).toHaveBeenLastCalledWith(false);f.tap.dispose()
 })
 it('a late recognized result cannot replace or clear another existing tail barrier',async()=>{
  const f=fixture();let resolve:(x:any)=>void=()=>{};f.recognize.mockImplementation((_s,p)=>new Promise(r=>{resolve=x=>r({...x,playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch})}));f.utterance()
  const token=f.barrier.begin({sessionId:'other-session',childId:'other-child'},'other-owned',1,1000)!;resolve({status:'recognized',control:'stop_playback',cloud_used:false,persisted:false,learning_evidence:false});await Promise.resolve();await Promise.resolve()
  expect(f.cancel).not.toHaveBeenCalled();expect(f.barrier.token()).toBe(token);f.barrier.rollback(token);f.tap.dispose()
 })
 it('only one claimed capture epoch credits quiet even when old and new processors still offer frames',()=>{
  const barrier=new ProtectedPlaybackBarrier(),scope={sessionId:'public-session',childId:'public-child'},a=Symbol(),b=Symbol();barrier.begin(scope,'owned',1,1000);expect(barrier.claim(scope,a,1000)).toBe(true)
  expect(barrier.observe(scope,a,false,500,1500)).toBe('held');expect(barrier.claim(scope,b,1500)).toBe(true)
  expect(barrier.observe(scope,a,false,850,2000)).toBe('held');expect(barrier.observe(scope,b,false,500,2100)).toBe('held');expect(barrier.observe(scope,b,false,350,2450)).toBe('held');expect(barrier.observe(scope,b,false,64,2950)).toBe('released')
 })
 it('model reply cannot manufacture learning/cloud receipt or borrow another epoch',async()=>{
  for(const delta of [{cloud_used:true},{persisted:true},{learning_evidence:true},{capture_epoch:2}]){
   const f=fixture();f.recognize.mockImplementation(async(_s,p)=>({status:'recognized',control:'stop_playback',playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false,...delta} as any));f.utterance();await Promise.resolve();await Promise.resolve();expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
  }
 })
 it('WAV is capped to 2.5 sec even with a nonadvancing wall clock',()=>{
  const f=fixture();for(let i=0;i<45;i++){f.setNow(1000);f.tap.processFrame(new Float32Array(1024).fill(.05),16000)}expect(f.recognize).not.toHaveBeenCalled();f.tap.dispose()
  const bytes=Uint8Array.from(atob(encodeControlWav([new Float32Array(100000)],16000)),c=>c.charCodeAt(0));expect(bytes.length).toBe(80044);expect(new DataView(bytes.buffer).getUint32(24,true)).toBe(16000)
 })
 it('no new microphone owner is acquired by fanout',async()=>{
  const f=fixture(),getUserMedia=vi.fn(),old=Object.getOwnPropertyDescriptor(navigator,'mediaDevices');Object.defineProperty(navigator,'mediaDevices',{value:{getUserMedia},configurable:true});f.utterance();await Promise.resolve();expect(getUserMedia).not.toHaveBeenCalled();f.tap.dispose();if(old)Object.defineProperty(navigator,'mediaDevices',old);else delete (navigator as any).mediaDevices
 })
 it('stalled local frames cannot leave the cloud lane held forever or reopen it by elapsed time',async()=>{
  vi.useFakeTimers();const f=fixture();f.utterance();await Promise.resolve();await Promise.resolve();expect(f.hold).toHaveBeenCalledWith(true)
  await vi.advanceTimersByTimeAsync(5000);expect(f.fail).toHaveBeenCalledTimes(1);expect(f.hold).not.toHaveBeenCalledWith(false);f.tap.dispose();vi.useRealTimers()
 })
 it('late completion cannot cancel even before an overdue timer callback gets its turn',async()=>{
  const f=fixture();let resolve:(x:any)=>void=()=>{};f.recognize.mockImplementation((_s,p)=>new Promise(r=>{resolve=r}));f.utterance();const p=f.recognize.mock.calls[0][1];f.setNow(7000)
  resolve({status:'recognized',control:'stop_playback',playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false});await Promise.resolve();await Promise.resolve();expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
 it('normal long SAPI references remain exact and are not truncated to observer bounds',async()=>{
  const text='继续核对当前保存的学习内容。'.repeat(500),f=fixture({text});f.utterance();await Promise.resolve();await Promise.resolve();expect(f.recognize.mock.calls[0][1].current_playback_text).toBe(text);expect(f.cancel).toHaveBeenCalledTimes(1);f.tap.dispose()
 })
})

const pcmRef=()=>Object.freeze({version:'pet_full_audio_control_reference_v1' as const,child_id:'public-child',session_id:'public-session',media_id:'media',prompt_id:'canonical-unit-prompt',item_version:'canonical-version',diagnostic_item_id:'leader-item',source_version:'source-v',audio_sha256:'a'.repeat(64),spoken_text_sha256:'b'.repeat(64),source_review_event_id:'review',generation:2,local_only:true as const,cloud_transmission_allowed:false as const})
describe('explicit reviewed child PCM reference',()=>{
 it('recognizes stop only for the exact child PCM lease and sends no script or question',async()=>{
  const f=fixture({kind:'local_pcm',text:null as unknown as string,attention:'小知'});const owner=f.getOwner();f.setOwner({...owner,lease:{...owner.lease,pcmReference:pcmRef()}})
  f.utterance();await Promise.resolve();await Promise.resolve();expect(f.cancel).toHaveBeenCalledTimes(1)
  const p=f.recognize.mock.calls[0][1];expect(p.source_kind).toBe('local_pcm');expect(p.pcm_reference?.prompt_id).toBe('canonical-unit-prompt');expect(p).not.toHaveProperty('current_playback_text');expect(p).not.toHaveProperty('question_reference');expect(p).not.toHaveProperty('attention_wake_word');f.tap.dispose()
 })
 it.each(['child','session','unknown','preview'])('rejects %s capsule without any recognition',async(kind)=>{
  const f=fixture({kind:'local_pcm',text:null as unknown as string}),owner=f.getOwner(),ref={...pcmRef(),...(kind==='child'?{child_id:'other'}:kind==='session'?{session_id:'other'}:kind==='unknown'?{version:'unknown'}:{purpose:'parent-preview'})}
  f.setOwner({...owner,lease:{...owner.lease,pcmReference:ref}});f.utterance();await Promise.resolve();expect(f.recognize).not.toHaveBeenCalled();f.tap.dispose()
 })
 it('late recognition cannot cancel a replaced PCM reference',async()=>{
  const f=fixture({kind:'local_pcm',text:null as unknown as string}),owner=f.getOwner();f.setOwner({...owner,lease:{...owner.lease,pcmReference:pcmRef()}})
  let resolve:(p:any)=>void=()=>{};f.recognize.mockImplementation((_s,p)=>new Promise(r=>{resolve=r}));f.utterance();const p=f.recognize.mock.calls[0][1],current=f.getOwner()
  f.setOwner({...current,lease:{...current.lease,pcmReference:Object.freeze({...pcmRef(),source_review_event_id:'new-review'})}})
  resolve({status:'recognized',control:'stop_playback',playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false});await Promise.resolve();await Promise.resolve();expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
 })
})
