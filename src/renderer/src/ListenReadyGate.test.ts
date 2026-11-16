import { afterEach, expect, it, vi } from 'vitest'
import { cancelListenReady, clearListenReadyGuidance, configureListenReadyPlayback, listenReadyPending,
  observeListenReady, requestListenReady, type ListenReadyScope, type ListenReadyNoticeEnd, type ListenReadyAttempt } from './listenReadyCue'
import { claimVoiceCapture, resetVoiceCaptureCoordinatorForTests } from './voiceCaptureCoordinator'
import * as voice from './companionVoice'

afterEach(() => { cancelListenReady();resetVoiceCaptureCoordinatorForTests();vi.restoreAllMocks();vi.useRealTimers() })
function fixture() {
  vi.useFakeTimers();vi.setSystemTime(10000)
  const owner=crypto.randomUUID();claimVoiceCapture(owner)
  let state:ReturnType<typeof voice.getCompanionVoiceState>='idle',valid=true,mode:unknown=true
  vi.spyOn(voice,'getCompanionVoiceState').mockImplementation(()=>state)
  const track={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:mode})}as unknown as MediaStreamTrack
  const capture={owner,track,epoch:Symbol(),rejectUtterance:vi.fn()},ready=vi.fn(),cancel=vi.fn()
  let endGuide:(value:ListenReadyNoticeEnd|false)=>void=()=>{},endTone:(value:boolean)=>void=()=>{}
  const guidance=vi.fn((_attempt:ListenReadyAttempt)=>new Promise<ListenReadyNoticeEnd|false>(resolve=>{endGuide=resolve})),tone=vi.fn((_attempt:ListenReadyAttempt)=>new Promise<boolean>(resolve=>{endTone=resolve}))
  configureListenReadyPlayback({guidance,tone,cancel})
  const scope:ListenReadyScope={sessionId:'s',childId:'a',prompt:{},endedAt:Date.now(),capture,sourceStamp:{},isCurrent:()=>valid,onReady:ready}
  requestListenReady(scope)
  const frame=async(voiced=false,ms=20)=>{await vi.advanceTimersByTimeAsync(ms);return observeListenReady('s','a',capture,voiced,ms,Date.now())}
  const quiet=async(n=96)=>{for(let i=0;i<n;i++)await frame()}
  return {scope,capture,ready,cancel,guidance,tone,frame,quiet,state:(value:typeof state)=>{state=value},mode:(value:unknown)=>{mode=value},invalidate:()=>{valid=false},
    endGuide:async(value=true)=>{endGuide(value?{naturalEnd:true,endedAt:Date.now(),sessionId:'s',leaseId:'actual-test-lease',leaseGeneration:1,leaseKind:'local_sapi'}:false);await vi.advanceTimersByTimeAsync(1)},endTone:async(value=true)=>{endTone(value);await vi.advanceTimersByTimeAsync(1)}}
}
it('elapsed time without actual quiet frames cannot admit a tone',async()=>{
 const x=fixture();await vi.advanceTimersByTimeAsync(2500);expect(x.guidance).not.toHaveBeenCalled()
 await x.quiet(30);await vi.advanceTimersByTimeAsync(501);await x.frame();await x.quiet(22)
 expect(x.guidance).not.toHaveBeenCalled();await x.quiet(30);expect(x.guidance).toHaveBeenCalledOnce()
})
it.each(['speaking','preparing'])('current %s resets the real quiet interval',async(state)=>{
 const x=fixture();await vi.advanceTimersByTimeAsync(850);await x.quiet(40);x.state(state as never);await x.frame();x.state('idle')
 await x.quiet(40);expect(x.guidance).not.toHaveBeenCalled();await x.quiet(13);expect(x.guidance).toHaveBeenCalledOnce()
})
it('160ms requires fresh quiet frames wholly after the actual tone end',async()=>{
 const x=fixture();await x.quiet();await x.endGuide();await x.quiet();expect(x.tone).toHaveBeenCalledOnce();await x.endTone()
 await vi.advanceTimersByTimeAsync(1);observeListenReady('s','a',x.capture,false,32,Date.now())
 await x.quiet(7);expect(x.ready).not.toHaveBeenCalled();await x.frame();expect(x.ready).toHaveBeenCalledOnce()
})
it('foreign owner or old capture callback cannot close the current cue',async()=>{
 const x=fixture();const old={...x.capture,track:{...x.capture.track}as MediaStreamTrack,epoch:Symbol()}
 cancelListenReady('s',x.capture.owner,old);expect(listenReadyPending('s','a',x.capture.owner)).toBe(true)
 expect(observeListenReady('s','b',old,false,20,Date.now()).blocked).toBe(true);expect(x.cancel).not.toHaveBeenCalled()
 await x.quiet();expect(x.guidance).toHaveBeenCalledOnce()
})
it('late natural end from the replaced prompt never authorizes or clears a fresh cue',async()=>{
 const x=fixture();await x.quiet();const oldEnd=x.endGuide
 const next={...x.scope,prompt:{},endedAt:Date.now()};requestListenReady(next)
 await oldEnd();expect(x.ready).not.toHaveBeenCalled();expect(x.tone).not.toHaveBeenCalled()
 expect(listenReadyPending('s','a',x.capture.owner)).toBe(true);await x.quiet();expect(x.guidance).toHaveBeenCalledTimes(2)
})
it('8s attempt timeout rejects and cleans feedback without permanently retaining listening',async()=>{
 const x=fixture();await vi.advanceTimersByTimeAsync(8000)
 expect(x.capture.rejectUtterance).toHaveBeenCalledOnce();expect(x.cancel).toHaveBeenCalledOnce()
 expect(listenReadyPending('s','a',x.capture.owner)).toBe(false)
 requestListenReady({...x.scope,endedAt:Date.now()});await x.quiet();expect(x.guidance).not.toHaveBeenCalled()
})
it('failed guidance is not remembered as taught, while new owner/session release clears it',async()=>{
 const x=fixture();await x.quiet();await x.endGuide(false)
 requestListenReady({...x.scope,prompt:{},endedAt:Date.now()});await x.quiet();expect(x.guidance).toHaveBeenCalledTimes(2)
 clearListenReadyGuidance('s','a',x.capture.owner);expect(listenReadyPending('s','a',x.capture.owner)).toBe(false)
})
it('capability or source change cancels feedback with no grant or ASR authorization',async()=>{
 const x=fixture();await x.quiet();x.mode('all');await x.endGuide()
 expect(x.tone).not.toHaveBeenCalled();expect(x.ready).not.toHaveBeenCalled();expect(x.capture.rejectUtterance).toHaveBeenCalledOnce()
})
it('notice creation owns one 12s window and only natural end starts independent 8s feedback',async()=>{
 const x=fixture();await x.quiet();const attempt=x.guidance.mock.calls[0][0]
 expect(attempt.deadline-Date.now()).toBe(11980);const originalEnd=x.scope.endedAt
 await vi.advanceTimersByTimeAsync(9000);expect(listenReadyPending('s','a',x.capture.owner)).toBe(true)
 await x.endGuide();expect(x.scope.endedAt).toBe(originalEnd)
 await x.quiet();expect(x.tone).toHaveBeenCalledOnce();const tone=x.tone.mock.calls[0][0]
 expect(tone.deadline).toBeGreaterThan(attempt.deadline-3000)
 await vi.advanceTimersByTimeAsync(8000);expect(listenReadyPending('s','a',x.capture.owner)).toBe(false)
 expect(x.capture.rejectUtterance).toHaveBeenCalledOnce()
})
it('speech timeout or late duplicate natural end cannot renew feedback or remember guidance',async()=>{
 const x=fixture();await x.quiet();const deadline=x.guidance.mock.calls[0][0].deadline
 await vi.advanceTimersByTimeAsync(deadline-Date.now());expect(Date.now()).toBe(deadline)
 await x.endGuide();await x.endGuide();expect(x.tone).not.toHaveBeenCalled()
 expect(x.capture.rejectUtterance).toHaveBeenCalledOnce()
 requestListenReady({...x.scope,prompt:{},endedAt:Date.now()});await x.quiet();expect(x.guidance).toHaveBeenCalledTimes(2)
})
it('source invalidation during notice cleans its exact attempt and never admits a tone',async()=>{
 const x=fixture();await x.quiet();x.invalidate();await x.endGuide()
 expect(x.ready).not.toHaveBeenCalled();expect(x.tone).not.toHaveBeenCalled();expect(x.cancel).toHaveBeenCalledOnce()
})
