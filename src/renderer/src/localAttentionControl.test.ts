import {afterEach,expect,it,vi} from 'vitest'
import {LocalAttentionControl,MAX_ATTENTION_WINDOWS} from './localAttentionControl'
import {PlaybackControlAdmission} from './playbackControlAdmission'
import {ProtectedPlaybackBarrier} from './protectedPlaybackBarrier'
import * as encoding from './protectedPlaybackControl'
import * as wake from './wakeWordRuntime'

afterEach(()=>{vi.restoreAllMocks();vi.useRealTimers()})
const flush=async()=>{for(let i=0;i<6;i++)await Promise.resolve()}
function fixture(){
 let now=0,word:string|undefined='小知'
 let capture:any={sessionId:'public-session',childId:'public-child',inputKind:'segmented',localTrack:{readyState:'live',enabled:true,getSettings:()=>({echoCancellation:true})},captureEpoch:Symbol(),submissionBlocked:true}
 let owner:any={...capture,lease:{id:'00000000-0000-4000-8000-000000000001',generation:1,sessionId:'public-session',kind:'local_sapi',text:'公开讲解仍在继续。'}}
 const ledger=new PlaybackControlAdmission(owner.lease.id,1,'public-session'),barrier=new ProtectedPlaybackBarrier(),woke=vi.fn(),hold=vi.fn()
 const cancel=vi.fn(()=>{owner=null;return true})
 const recognize=vi.fn(async(_s:string,p:any):Promise<any>=>({status:'not_control',control:null,playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false}))
 const build=()=>new LocalAttentionControl({current:()=>owner,capture:()=>capture,word:()=>word,recognize,cancelExact:cancel,holdInput:hold,onWake:woke,barrier,now:()=>now,admission:()=>ledger})
 let tap=build()
 const frames=(n:number,value=.05)=>{for(let i=0;i<n;i++){now+=100;tap.processFrame(new Float32Array(1600).fill(value),16000)}}
 return {get tap(){return tap},frames,recognize,cancel,woke,hold,barrier,ledger,setWord:(x:string|undefined)=>word=x,
  owner:()=>owner,capture:()=>capture,setOwner:(x:any)=>owner=x,
  reconnect:()=>{tap.dispose();capture={...capture,localTrack:{...capture.localTrack},captureEpoch:Symbol()};if(owner)owner={...owner,...capture};tap=build()}}
}
const result=(p:any,control='wake_companion')=>({status:'recognized',control,playback_lease_id:p.playback_lease_id,playback_generation:p.playback_generation,capture_epoch:p.capture_epoch,cloud_used:false,persisted:false,learning_evidence:false})

it('recognition that ignores abort and never settles cannot permanently occupy the attention window',async()=>{
 vi.useFakeTimers()
 const f=fixture(),pending:Array<()=>void>=[]
 f.recognize.mockImplementation((_s,p)=>new Promise(resolve=>{pending.push(()=>resolve(result(p)))}))
 f.frames(24);f.frames(50);expect(f.recognize).toHaveBeenCalledOnce()
 await vi.advanceTimersByTimeAsync(5000);f.frames(1);await flush()
 expect(f.recognize).toHaveBeenCalledTimes(2)
 pending[0]();await flush();expect(f.cancel).not.toHaveBeenCalled();expect(f.woke).not.toHaveBeenCalled()
 pending[1]();await flush();expect(f.cancel).toHaveBeenCalledOnce();expect(f.woke).toHaveBeenCalledOnce();f.tap.dispose()
})

it('an old timeout and late old result cannot clear or retire the newer current request',async()=>{
 vi.useFakeTimers()
 const f=fixture(),pending:Array<()=>void>=[]
 f.recognize.mockImplementation((_s,p)=>new Promise(resolve=>{pending.push(()=>resolve(result(p)))}))
 f.frames(24);await vi.advanceTimersByTimeAsync(2000)
 f.setWord('小伴');f.frames(24);expect(f.recognize).toHaveBeenCalledTimes(2)
 await vi.advanceTimersByTimeAsync(3000);f.frames(10);await flush()
 expect(f.recognize).toHaveBeenCalledTimes(2)
 pending[0]();await flush();expect(f.cancel).not.toHaveBeenCalled()
 pending[1]();await flush();expect(f.cancel).toHaveBeenCalledOnce();expect(f.woke).toHaveBeenCalledOnce();f.tap.dispose()
})

it('continuous voiced frames start local windows without any quiet and acknowledge once with the normal-input barrier held',async()=>{
 const f=fixture();f.recognize.mockImplementation(async(_s,p)=>result(p));f.woke.mockImplementation(()=>expect(f.barrier.held()).toBe(true))
 f.frames(23);expect(f.recognize).not.toHaveBeenCalled();f.frames(1);await flush()
 expect(f.recognize).toHaveBeenCalledOnce();expect(f.recognize.mock.calls[0][1].attention_wake_word).toBe('小知')
 expect(f.cancel).toHaveBeenCalledOnce();expect(f.woke).toHaveBeenCalledOnce();expect(f.barrier.held()).toBe(true)
 f.frames(30);await flush();expect(f.woke).toHaveBeenCalledOnce();expect(f.hold).toHaveBeenLastCalledWith(true);f.tap.dispose()
})

it('pending decoder retains only the latest2.4s window; the next request uses fresh audio with no concurrent request',async()=>{
 const f=fixture();let resolve:(x:any)=>void=()=>{}
 f.recognize.mockImplementationOnce((_s,p)=>new Promise(r=>{resolve=x=>r({...result(p),...x})}))
 f.frames(24,.05);f.frames(40,.2)
 expect(f.recognize).toHaveBeenCalledOnce();expect((f.tap as any).samples).toBeLessThanOrEqual(38400)
 resolve({status:'not_control',control:null});await flush();f.frames(1,.3);await flush()
 expect(f.recognize).toHaveBeenCalledTimes(2)
 const bytes=Uint8Array.from(atob(f.recognize.mock.calls[1][1].wav_base64),c=>c.charCodeAt(0)),view=new DataView(bytes.buffer)
 expect(bytes.length).toBe(76844);expect(view.getInt16(44,true)).toBeGreaterThan(6000);expect(view.getInt16(bytes.length-2,true)).toBeGreaterThan(9000)
 expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
})

it('one-second fresh strides overlap1.4s and continuous noise never consumes the original stop quiet counter',async()=>{
 const f=fixture(),clock:number[]=[];f.recognize.mockImplementation(async(_s,p)=>{clock.push(Date.now());return {...result(p),status:'not_control',control:null}})
 f.frames(24);await flush();f.frames(9);await flush();expect(f.recognize).toHaveBeenCalledOnce()
 f.frames(1);await flush();expect(f.recognize).toHaveBeenCalledTimes(2)
 const epoch=Symbol();expect(f.ledger.admitFrame(epoch,true,100,10000)).toBe(true);expect(f.ledger.consume(epoch,10000)).toBe(true)
 expect(f.ledger.admitFrame(epoch,true,100,12000)).toBe(false);f.tap.dispose()
})

it('same lease has512 windows total across new name and track/tap; no reset or extra decoder after exhaustion',async()=>{
 vi.spyOn(encoding,'encodeControlWav').mockReturnValue('SYNTHETIC_BUDGET_ONLY')
 const f=fixture();expect(MAX_ATTENTION_WINDOWS).toBe(512)
 for(let i=0;i<512;i++){
  if(i===256){f.setWord('小伴');f.reconnect()}
  f.frames(i===0||i===256?24:10);await flush()
 }
 expect(f.recognize).toHaveBeenCalledTimes(512);f.frames(100);await flush();expect(f.recognize).toHaveBeenCalledTimes(512)
 expect(f.cancel).not.toHaveBeenCalled();f.tap.dispose()
})

it.each(['word','lease','child','track','dispose','deadline'])('late window after %s change cannot cancel or acknowledge',async kind=>{
 const f=fixture();let resolve:(x:any)=>void=()=>{}
 f.recognize.mockImplementationOnce((_s,p)=>new Promise(r=>{resolve=x=>r({...result(p),...x})}))
 f.frames(24);const old=f.owner()
 if(kind==='word')f.setWord('小伴');else if(kind==='dispose')f.tap.dispose();else if(kind==='deadline')f.frames(60);else f.setOwner({...old,...(kind==='child'?{childId:'other'}:kind==='track'?{localTrack:{...old.localTrack}}:{}),lease:{...old.lease,...(kind==='lease'?{id:'new-lease'}:{})}})
 resolve({});await flush();expect(f.cancel).not.toHaveBeenCalled();expect(f.woke).not.toHaveBeenCalled();f.tap.dispose()
})

it('a fixed stop result cancels only its current playback and never emits the wake acknowledgement',async()=>{
 const f=fixture();f.recognize.mockImplementation(async(_s,p)=>result(p,'stop_playback'))
 f.frames(24);await flush();expect(f.cancel).toHaveBeenCalledOnce();expect(f.woke).not.toHaveBeenCalled();expect(f.barrier.held()).toBe(true);f.tap.dispose()
})

it('exact cancellation failure and replacement output never receive an old acknowledgement',async()=>{
 const f=fixture();f.recognize.mockImplementation(async(_s,p)=>result(p));f.cancel.mockReturnValue(false)
 f.frames(24);await flush();expect(f.woke).not.toHaveBeenCalled();expect(f.barrier.held()).toBe(false)
 const old=f.owner();f.cancel.mockImplementation(()=>{f.setOwner({...old,lease:{...old.lease,id:'replacement-output',generation:2}});return true})
 f.frames(10);await flush();expect(f.woke).not.toHaveBeenCalled();expect(f.owner().lease.id).toBe('replacement-output');expect(f.barrier.held()).toBe(true);f.tap.dispose()
})

it('self-name echo cache scans long text only once per unchanged lease/text/name tuple',()=>{
 const f=fixture(),spy=vi.spyOn(wake,'matchWakeWord'),old=f.owner()
 f.setOwner({...old,lease:{...old.lease,text:'公开说明'.repeat(1000)+'小知'}});f.frames(100)
 expect(spy).toHaveBeenCalledOnce();expect(f.recognize).not.toHaveBeenCalled()
 f.setWord('小伴');f.frames(1);expect(spy).toHaveBeenCalledTimes(2)
 f.setOwner(null);f.frames(1);expect((f.tap as any).echoCache).toBeNull();f.tap.dispose()
})

it.each(['unregistered','pcm','normal_open','disabled_aec'])('%s cannot open an attention window',async kind=>{
 const f=fixture(),old=f.owner()
 if(kind==='unregistered')f.setWord(undefined)
 else if(kind==='pcm')f.setOwner({...old,lease:{...old.lease,kind:'local_pcm'}})
 else if(kind==='normal_open')f.setOwner({...old,submissionBlocked:false})
 else f.setOwner({...old,localTrack:{...old.localTrack,getSettings:()=>({echoCancellation:false})}})
 f.frames(40);await flush();expect(f.recognize).not.toHaveBeenCalled();expect(f.woke).not.toHaveBeenCalled();f.tap.dispose()
})
