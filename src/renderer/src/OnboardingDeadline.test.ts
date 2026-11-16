import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {localApi} from './api'
import {beginOnboardingResponse, onboardingResponseGrant,registerOnboardingCapture,revokeOnboardingResponses} from './onboardingResponseGrant'
import {claimVoiceCapture,resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import * as voice from './companionVoice'
let state:ReturnType<typeof voice.getCompanionVoiceState>,lease:ReturnType<typeof voice.getCompanionPlaybackLease>,current:boolean,clean:Array<()=>void>
const track={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:'all'})}as MediaStreamTrack
const capture={owner:'test-owner',track,epoch:Symbol(),echoPolicy:{mode:'all',negotiation:'accepted',acoustic_quality_verified:false}as const}
beforeEach(()=>{
 vi.useFakeTimers();current=true;state='idle';lease=null;claimVoiceCapture(capture.owner);clean=[registerOnboardingCapture('s','a',capture,()=>current)]
 vi.spyOn(voice,'getCompanionVoiceState').mockImplementation(()=>state);vi.spyOn(voice,'getCompanionPlaybackLease').mockImplementation(()=>lease)
})
afterEach(()=>{revokeOnboardingResponses();clean.forEach(fn=>fn());resetVoiceCaptureCoordinatorForTests();vi.restoreAllMocks();vi.useRealTimers()})


function ended(){const p=beginOnboardingResponse({sessionId:'s',childId:'a',phase:'active',step:'study_practice',epoch:1,promptText:'公开完整讲解',prompt:{},isCurrent:()=>current});state='speaking';lease={id:'actual',generation:3,sessionId:'s',kind:'local_sapi',text:'公开完整讲解'};p.started();state='idle';lease=null;p.naturalEnd();return p}
it('onboarding ready8s remains strict and expired cannot revive',async()=>{
 const p=ended();await vi.advanceTimersByTimeAsync(8000);expect(p.claim('s','a',capture)).toBe(false);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false);p.naturalEnd();expect(onboardingResponseGrant('s','a')).toBeNull()
})
it('onboarding one claim gets12s, duplicate claim cannot reset it',async()=>{
 const p=ended();await vi.advanceTimersByTimeAsync(7900);expect(p.claim('s','a',capture)).toBe(true);await vi.advanceTimersByTimeAsync(11999)
 expect(p.capturedCurrent('s','a',capture)).toBe(true);expect(p.claim('s','a',capture)).toBe(false);await vi.advanceTimersByTimeAsync(1)
 expect(p.capturedCurrent('s','a',capture)).toBe(false);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
})
it('onboarding one submission30s cannot be renewed or resurrected',async()=>{
 const p=ended();p.claim('s','a',capture);await vi.advanceTimersByTimeAsync(11000);expect(await p.beforeSubmit(new AbortController().signal)).toBe(true)
 await vi.advanceTimersByTimeAsync(29999);expect(p.capturedCurrent('s','a',capture)).toBe(true);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
 await vi.advanceTimersByTimeAsync(1);expect(p.capturedCurrent('s','a',capture)).toBe(false);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
})
it('aborted onboarding submit cannot reset12s claim',async()=>{
 const p=ended();p.claim('s','a',capture);const c=new AbortController();c.abort();expect(await p.beforeSubmit(c.signal)).toBe(false)
 await vi.advanceTimersByTimeAsync(12000);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
})
