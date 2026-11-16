import { afterEach, expect, it, vi } from 'vitest'
import { SpeechArbiter } from './speechArbiter'
import { playListenReadyTone } from './listenReadyTone'
import type { ListenReadyAttempt } from './listenReadyCue'

afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers()})
function fixture(pending=false,closeFails=false){
 vi.useFakeTimers();vi.setSystemTime(10000);let valid=true,resume:()=>void=()=>{}
 const source={buffer:null as AudioBuffer|null,connect:vi.fn(),disconnect:vi.fn(),start:vi.fn(),onended:null as null|((e:Event)=>void),stop:vi.fn(()=>source.onended?.({isTrusted:true}as Event))}
 const context={sampleRate:16000,currentTime:0,state:'suspended',destination:{},createBuffer:vi.fn((_c:number,n:number,r:number)=>({duration:n/r,getChannelData:()=>new Float32Array(n)})),createBufferSource:()=>source,
  resume:vi.fn(()=>pending?new Promise<void>(resolve=>{resume=()=>{context.state='running';resolve()}}):Promise.resolve().then(()=>{context.state='running'})),
  close:vi.fn(async()=>{if(closeFails)throw Error('synthetic');context.state='closed'})}
 const construct=vi.fn();vi.stubGlobal('AudioContext',class{constructor(){construct();return context}})
 const arbiter=new SpeechArbiter(vi.fn())
 const attempt:ListenReadyAttempt={key:'owned-cue',sessionId:'s',childId:'a',sourceStamp:{},captureEpoch:Symbol(),guidanceRequired:false,repeatRequired:false,deadline:18000,current:()=>valid}
 const result=playListenReadyTone(arbiter,attempt,'s')
 return {source,context,construct,result,resume:()=>resume(),invalid:()=>{valid=false},cancel:()=>arbiter.cancel(attempt.key),end:(trusted=true)=>{context.currentTime=.12;source.onended?.({isTrusted:trusted}as Event)}}
}
it('actual trusted natural tone end closes its own context and is 120ms non-language PCM',async()=>{
 const x=fixture();await vi.advanceTimersByTimeAsync(1);expect(x.source.start).toHaveBeenCalledOnce();expect(x.source.buffer?.duration).toBe(.12)
 x.end();await vi.advanceTimersByTimeAsync(1);expect(await x.result).toBe(true);expect(x.context.close).toHaveBeenCalledOnce();expect(x.source.stop).not.toHaveBeenCalled()
})
it('cancel before native resume stops owned source and late resume never starts it',async()=>{
 const x=fixture(true);await vi.advanceTimersByTimeAsync(1);x.cancel();await vi.advanceTimersByTimeAsync(1);x.resume();await vi.advanceTimersByTimeAsync(1)
 expect(await x.result).toBe(false);expect(x.source.start).not.toHaveBeenCalled();expect(x.source.stop).toHaveBeenCalledOnce();expect(x.context.close).toHaveBeenCalledOnce()
})
it('actual stop-generated trusted end cannot be mistaken for natural end',async()=>{
 const x=fixture();await vi.advanceTimersByTimeAsync(1);x.cancel();await vi.advanceTimersByTimeAsync(1)
 expect(await x.result).toBe(false);expect(x.source.stop).toHaveBeenCalledOnce();expect(x.context.close).toHaveBeenCalledOnce()
})
it.each(['untrusted','owner_changed','close_failed'])('%s native end fails closed with independent owned cleanup',async(kind)=>{
 const x=fixture(false,kind==='close_failed');await vi.advanceTimersByTimeAsync(1);if(kind==='owner_changed')x.invalid();x.end(kind!=='untrusted');await vi.advanceTimersByTimeAsync(1)
 expect(await x.result).toBe(false);expect(x.context.close).toHaveBeenCalledOnce();expect(x.source.disconnect).toHaveBeenCalledOnce()
})
it('missing native end expires once and still closes owned resources',async()=>{
 const x=fixture();await vi.advanceTimersByTimeAsync(8000);expect(await x.result).toBe(false);expect(x.source.stop).toHaveBeenCalledOnce();expect(x.context.close).toHaveBeenCalledOnce()
})
