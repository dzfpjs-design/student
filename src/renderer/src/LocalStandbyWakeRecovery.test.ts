// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {LocalStandbyWakeControl} from './localStandbyWakeControl'
import {ApiError} from './api'
import type {StandbyVoiceInput} from './types'
const taps:LocalStandbyWakeControl[]=[]
function fixture(){
 let now=1000,word='小知',eligible=true,child='child',token='immutable-token';const root={readyState:'live',enabled:true} as MediaStreamTrack,cloud={readyState:'live',enabled:false} as MediaStreamTrack
 const result=(id:string,text='普通环境人声'):StandbyVoiceInput=>({client_action_id:id,transcript:text,duration_ms:2400,status:'completed',cloud_used:false,persisted:false,model_version:'synthetic'})
 const recognize=vi.fn(async(_wav:string,_signal:AbortSignal,id:ReturnType<Crypto['randomUUID']>)=>result(id))
 const wake=vi.fn(),unavailable=vi.fn(),audio=vi.fn()
 const tap=new LocalStandbyWakeControl({capture:()=>({sessionId:'session',childId:child,inputKind:'realtime',localTrack:root,cloudTrack:cloud}),eligible:()=>eligible,word:()=>word,recognize,onWake:wake,onUnavailable:unavailable,onWakeAudio:audio,activationToken:()=>token,now:()=>now})
 taps.push(tap)
 const frames=(value=.04,n=38)=>{for(let i=0;i<n;i++){tap.processFrame(new Float32Array(1024).fill(value),16000);now+=64}}
 const settle=async()=>{await Promise.resolve();await Promise.resolve();await Promise.resolve()}
 return {tap,frames,settle,recognize,result,wake,unavailable,audio,root,cloud,setToken:(v:string)=>{token=v},setWord:(v:string)=>{word=v},changeChild:()=>{child='other'},setEligible:(v:boolean)=>{eligible=v}}
}
afterEach(()=>{for(const tap of taps.splice(0))tap.dispose();vi.useRealTimers();vi.restoreAllMocks()})
it('real backend no-human 503 is an exact soft result; subsequent fresh registered wake remains usable',async()=>{
 const f=fixture();f.recognize.mockRejectedValueOnce(new ApiError('没有检测到连续人声',503,'ASR_NO_HUMAN_SPEECH'))
 f.frames();await f.settle();expect(f.unavailable).not.toHaveBeenCalled()
 f.recognize.mockImplementation(async(_wav,_signal,id)=>f.result(id,'小知'));f.frames(.04,17);await f.settle()
 expect(f.wake).toHaveBeenCalledTimes(1);expect(f.recognize).toHaveBeenCalledTimes(2)
})
it('one earlier sound does not latch silence into repeated ASR requests after a consumed unrelated window',async()=>{
 const f=fixture();f.frames();await f.settle();expect(f.recognize).toHaveBeenCalledTimes(1)
 for(let i=0;i<20;i++){f.frames(0,17);await f.settle()}
 expect(f.recognize).toHaveBeenCalledTimes(1);expect(f.unavailable).not.toHaveBeenCalled()
})
it('a short bare wake that finishes before the 2.4-second window is still detected after a busy response',async()=>{
 const f=fixture();f.recognize.mockRejectedValueOnce(new ApiError('busy',503,'LOCAL_ASR_BUSY'))
 f.frames(.04,5);f.frames(0,33);await f.settle();expect(f.recognize).toHaveBeenCalledTimes(1)
 f.recognize.mockImplementation(async(_wav,_signal,id)=>f.result(id,'小智'))
 // The original voice is still in the overlapped window, with no new voiced frame.
 f.frames(0,16);await f.settle();expect(f.wake).toHaveBeenCalledTimes(1);expect(f.unavailable).not.toHaveBeenCalled()
})
it('old no-human result never clears newer pending speech or its immutable wake+command token',async()=>{
 const f=fixture();let reject!:(reason:unknown)=>void
 f.recognize.mockImplementationOnce(()=>new Promise((_yes,no)=>{reject=no}));f.frames()
 f.frames(.04,17);reject(new ApiError('no human',503,'ASR_NO_HUMAN_SPEECH'));await f.settle()
 f.recognize.mockImplementation(async(_wav,_signal,id)=>f.result(id,'小知，帮我复习地理'))
 f.frames(0,1);await f.settle();expect(f.audio).toHaveBeenCalledExactlyOnceWith('immutable-token');expect(f.unavailable).not.toHaveBeenCalled()
})
it('continuous eligible speech remains covered after 512 lifetime windows while time-window budget storage stays bounded',async()=>{
 const f=fixture();f.frames();await f.settle()
 for(let i=1;i<520;i++){f.frames(.04,16);await f.settle()}
 expect(f.recognize).toHaveBeenCalledTimes(520);expect((f.tap as any).windowStarts.length).toBeLessThanOrEqual(512);expect(f.unavailable).not.toHaveBeenCalled()
})
it.each(['timeout','late_failure','hard'] as const)('explicit transport-retired rearm recovers %s while ignoring the old late result and preserving budget',async kind=>{
 vi.useFakeTimers();const f=fixture();let old!: (value:StandbyVoiceInput)=>void,lateReject!:(cause:unknown)=>void,id=''
 f.recognize.mockImplementationOnce((_wav,_signal,key)=>{id=key;return kind!=='hard'?new Promise((done,no)=>{old=done;lateReject=no}):Promise.reject(new ApiError('model missing',503,'ASR_MODEL_NOT_READY'))})
 f.frames();if(kind!=='hard')await vi.advanceTimersByTimeAsync(5001);else await f.settle()
 expect(f.unavailable).toHaveBeenCalledExactlyOnceWith(kind!=='hard'?'timeout':'unavailable')
 const quota=[...(f.tap as any).windowStarts];expect(f.tap.resumeAfterTransportRetired()).toBe(true);expect((f.tap as any).windowStarts).toEqual(quota)
 if(kind!=='hard'){if(kind==='timeout')old(f.result(id,'小知'));else lateReject(new ApiError('late hard failure',500,'UNEXPECTED'));await f.settle();expect(f.wake).not.toHaveBeenCalled();expect(f.unavailable).toHaveBeenCalledTimes(1)}
 f.recognize.mockImplementation(async(_wav,_signal,key)=>f.result(key,'小知'));f.frames();await f.settle();expect(f.wake).toHaveBeenCalledTimes(1)
})
it('soft handling is not all-503 suppression, and retired child/disposed instances cannot rearm a late result',async()=>{
 const f=fixture();f.recognize.mockRejectedValueOnce(new ApiError('not ready',503,'ASR_MODEL_NOT_READY'));f.frames();await f.settle();expect(f.unavailable).toHaveBeenCalledWith('unavailable')
 f.tap.dispose();expect(f.tap.resumeAfterTransportRetired()).toBe(false)
 const g=fixture();let finish!:(value:StandbyVoiceInput)=>void,key='';g.recognize.mockImplementationOnce((_wav,_signal,id)=>{key=id;return new Promise(done=>{finish=done})});g.frames();g.changeChild();finish(g.result(key,'小知'));await g.settle();expect(g.wake).not.toHaveBeenCalled();expect(g.audio).not.toHaveBeenCalled()
})

it('a bounded busy retry retains the original WAV and token rather than borrowing the newest utterance token',async()=>{
 const f=fixture();f.recognize.mockRejectedValueOnce(new ApiError('busy',503,'LOCAL_ASR_BUSY'));f.frames();await f.settle()
 const originalWav=f.recognize.mock.calls[0][0];f.setToken('new-utterance-token')
 f.recognize.mockImplementation(async(_wav,_signal,id)=>f.result(id,'小知，帮我复习'))
 f.frames(0,16);await f.settle();expect(f.recognize.mock.calls[1][0]===originalWav).toBe(true);expect(f.audio).toHaveBeenCalledExactlyOnceWith('immutable-token');expect(f.wake).not.toHaveBeenCalled()
})
