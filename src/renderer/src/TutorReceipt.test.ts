import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {localApi} from './api'
import {ordinaryTutorPlayback,ordinaryResponseGrant,registerOrdinarySession,registerOrdinaryCapture,revokeOrdinaryResponses,type TutorVoicePlaybackReceipt} from './ordinaryResponseGrant'
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
it('no-board actualstart/naturalend chains durable tuple and confirms only finished ACK',async()=>{
 const p=prepare();expect(await p.options.beforePlayback!(new AbortController().signal)).toBe(true);start(p);await end(p)
 expect(p.grant.claim('s','a',capture)).toBe(true);expect(await p.grant.beforeSubmit(new AbortController().signal)).toBe(true)
 const calls=vi.mocked(localApi.tutorVoicePlayback).mock.calls;expect(calls.map(x=>x[1])).toEqual(['reserve','start','natural-end'])
 expect(calls[1][2]).toMatchObject({playback_id:'p',generation:1,lease_id:'actual',lease_generation:3,lease_kind:'local_sapi'})
 expect(p.grant.binding).toMatchObject({source_tutor_turn_id:'t',source_tutor_voice_playback_id:'p'})
})
it('claimed early memory waits for final ACK within one submission deadline',async()=>{
 let release!:(value:TutorVoicePlaybackReceipt)=>void;const original=localApi.tutorVoicePlayback
 vi.mocked(original).mockImplementation(async(_s,op,values)=>op==='natural-end'?await new Promise(resolve=>{release=resolve}):({...row,status:op==='start'?'started':'reserved',...(op==='start'?{lease_id:'actual',lease_generation:3,lease_kind:'local_sapi'}:{})})as TutorVoicePlaybackReceipt)
 const p=prepare();await p.options.beforePlayback!(new AbortController().signal);start(p);await end(p);p.grant.claim('s','a',capture)
 let submitted=false;const pending=p.grant.beforeSubmit(new AbortController().signal).then(value=>{submitted=value})
 await vi.advanceTimersByTimeAsync(7999);expect(submitted).toBe(false);release({...row,status:'finished',lease_id:'actual',lease_generation:3,lease_kind:'local_sapi'});await pending;expect(submitted).toBe(true)
})
it('late finished ACK cannot reopen expired submission lease',async()=>{
 let release!:(value:TutorVoicePlaybackReceipt)=>void
 vi.mocked(localApi.tutorVoicePlayback).mockImplementation(async(_s,op)=>op==='natural-end'?await new Promise(resolve=>{release=resolve}):({...row,status:op==='start'?'started':op==='cancel'?'cancelled':'reserved',...(op==='reserve'?{}:{lease_id:'actual',lease_generation:3,lease_kind:'local_sapi'})})as TutorVoicePlaybackReceipt)
 const p=prepare();await p.options.beforePlayback!(new AbortController().signal);start(p);await end(p);p.grant.claim('s','a',capture)
 const pending=p.grant.beforeSubmit(new AbortController().signal);await vi.advanceTimersByTimeAsync(30000);expect(await pending).toBe(false)
 release({...row,status:'finished',lease_id:'actual',lease_generation:3,lease_kind:'local_sapi'});await Promise.resolve();expect(ordinaryResponseGrant('s','a')).toBeNull()
})
it.each(['owner','tuple','start'])('no-board %s failure cancels rather than authorizing ASR',async(kind)=>{
 const p=prepare();await p.options.beforePlayback!(new AbortController().signal)
 if(kind==='start')vi.mocked(localApi.tutorVoicePlayback).mockRejectedValueOnce(Error('offline'))
 start(p);if(kind==='owner')current=false
 if(kind==='tuple')vi.mocked(localApi.tutorVoicePlayback).mockImplementation(async()=>({...row,status:'finished',lease_id:'wrong',lease_generation:3,lease_kind:'local_sapi'}))
 await end(p);await vi.advanceTimersByTimeAsync(1);expect(p.grant.claim('s','a',capture)).toBe(false);expect(ordinaryResponseGrant('s','a')).toBeNull()
})
it('reserve denial or text fallback cannot manufacture a natural end',async()=>{
 vi.mocked(localApi.tutorVoicePlayback).mockRejectedValueOnce(Error('denied'));const p=prepare();expect(await p.options.beforePlayback!(new AbortController().signal)).toBe(false)
 p.options.onNotDelivered?.('unavailable');expect(ordinaryResponseGrant('s','a')).toBeNull();expect(vi.mocked(localApi.tutorVoicePlayback).mock.calls.map(x=>x[1])).toEqual(['reserve'])
})
