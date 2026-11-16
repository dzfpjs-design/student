import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {localApi} from './api'
import {beginOrdinaryResponse,ordinaryTutorPlayback,ordinaryResponseGrant,registerOrdinarySession,registerOrdinaryCapture,revokeOrdinaryResponses,type TutorVoicePlaybackReceipt} from './ordinaryResponseGrant'
import {claimVoiceCapture,resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import * as voice from './companionVoice'
let state:ReturnType<typeof voice.getCompanionVoiceState>,lease:ReturnType<typeof voice.getCompanionPlaybackLease>,current:boolean,clean:Array<()=>void>
const row:TutorVoicePlaybackReceipt={playback_id:'p',turn_id:'t',child_id:'a',session_id:'s',source_version:'sv',generation:1,status:'reserved',lease_id:null,lease_generation:null,lease_kind:null}
const track={readyState:'live',enabled:true,getSettings:()=>({echoCancellation:'all'})}as MediaStreamTrack
const capture={owner:'test-owner',track,epoch:Symbol(),echoPolicy:{mode:'all',negotiation:'accepted',acoustic_quality_verified:false}as const}
beforeEach(()=>{
 vi.useFakeTimers();current=true;state='idle';lease=null;claimVoiceCapture(capture.owner);clean=[registerOrdinarySession({sessionId:'s',childId:'a',state:'study'}),registerOrdinaryCapture('s','a',capture,()=>current)]
 vi.spyOn(voice,'getCompanionVoiceState').mockImplementation(()=>state);vi.spyOn(voice,'getCompanionPlaybackLease').mockImplementation(()=>lease)
 vi.spyOn(localApi,'tutorVoicePlayback').mockImplementation(async(_s,op,values)=>({...row,status:op==='reserve'?'reserved':op==='start'?'started':op==='natural-end'?'finished':'cancelled',...(op==='reserve'?{}:{lease_id:values.lease_id as string,lease_generation:values.lease_generation as number,lease_kind:values.lease_kind as 'local_sapi'})}))
})
afterEach(()=>{revokeOrdinaryResponses();clean.forEach(fn=>fn());resetVoiceCaptureCoordinatorForTests();vi.restoreAllMocks();vi.useRealTimers()})
function prepare(){return ordinaryTutorPlayback({sessionId:'s',childId:'a',state:'study',text:'公开完整讲解',prompt:{},sourceTutorTurnId:'t',isCurrent:()=>current},{localOnly:true})}
function start(p:ReturnType<typeof prepare>){state='speaking';lease={id:'actual',generation:3,sessionId:'s',kind:'local_sapi',text:'公开完整讲解'};p.started()}
async function end(p:ReturnType<typeof prepare>){state='idle';lease=null;p.options.onPlaybackEnd?.();await Promise.resolve();await Promise.resolve()}

function ended(){const p=beginOrdinaryResponse({sessionId:'s',childId:'a',state:'study',text:'公开完整讲解',prompt:{},isCurrent:()=>current});state='speaking';lease={id:'actual',generation:3,sessionId:'s',kind:'local_sapi',text:'公开完整讲解'};p.started();state='idle';lease=null;p.naturalEnd();return p}
it('ready expires at8s and late claim or submit cannot revive',async()=>{
 const p=ended();await vi.advanceTimersByTimeAsync(8000);expect(p.claim('s','a',capture)).toBe(false);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false);p.naturalEnd();expect(ordinaryResponseGrant('s','a')).toBeNull()
})
it('claim at7.9s has one12s capture deadline; frames cannot renew it',async()=>{
 const p=ended();await vi.advanceTimersByTimeAsync(7900);expect(p.claim('s','a',capture)).toBe(true)
 await vi.advanceTimersByTimeAsync(11999);expect(p.capturedCurrent('s','a',capture)).toBe(true);expect(p.claim('s','a',capture)).toBe(false)
 await vi.advanceTimersByTimeAsync(1);expect(p.capturedCurrent('s','a',capture)).toBe(false);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
})
it('one beforeSubmit freezes30s; duplicate calls and ACK do not renew',async()=>{
 const p=ended();p.claim('s','a',capture);await vi.advanceTimersByTimeAsync(11000);expect(await p.beforeSubmit(new AbortController().signal)).toBe(true)
 await vi.advanceTimersByTimeAsync(29999);expect(p.capturedCurrent('s','a',capture)).toBe(true);p.confirmDelivery();expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
 await vi.advanceTimersByTimeAsync(1);expect(p.capturedCurrent('s','a',capture)).toBe(false);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
})
it('accepted dispatch shares30s cutoff and cannot outlive it',async()=>{
 const p=ended();p.claim('s','a',capture);expect(await p.beforeSubmit(new AbortController().signal)).toBe(true);p.markAccepted();p.revoke()
 expect(p.handoffCurrent()).toBe(true);await vi.advanceTimersByTimeAsync(30000);expect(p.handoffCurrent()).toBe(false)
})
it('pending delivery has same30s cutoff and late ACK cannot authorize',async()=>{
 const p=beginOrdinaryResponse({sessionId:'s',childId:'a',state:'study',text:'公开完整讲解',prompt:{},deliveryPending:true,isCurrent:()=>current});state='speaking';lease={id:'actual',generation:3,sessionId:'s',kind:'local_sapi',text:'公开完整讲解'};p.started();state='idle';lease=null;p.naturalEnd();p.claim('s','a',capture)
 const pending=p.beforeSubmit(new AbortController().signal);await vi.advanceTimersByTimeAsync(30000);expect(await pending).toBe(false);p.confirmDelivery();expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
})
it('aborted submit cannot consume or extend existing capture deadline',async()=>{
 const p=ended();p.claim('s','a',capture);const c=new AbortController();c.abort();expect(await p.beforeSubmit(c.signal)).toBe(false)
 await vi.advanceTimersByTimeAsync(12000);expect(await p.beforeSubmit(new AbortController().signal)).toBe(false)
})
