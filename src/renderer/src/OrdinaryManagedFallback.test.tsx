import {act,cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import type {ComponentProps} from 'react'
import {readFileSync} from 'node:fs'
import {execFileSync} from 'node:child_process'
import {resolve} from 'node:path'
import {ordinaryPromptPlayback,ordinaryResponseGrant,ordinaryResponseMayOwnPrompt,ordinaryTutorPlayback,registerOrdinarySession,revokeOrdinaryResponses} from './ordinaryResponseGrant'
import * as inputs from './LocalInputs'
import * as voice from './companionVoice'
import {localApi,requestVoiceSynthesis} from './api'
import * as api from './api'
import {listenReadyPending,cancelListenReady} from './listenReadyCue'
import {onboardingResponseGrant,revokeOnboardingResponses} from './onboardingResponseGrant'
import {getVoiceCaptureOwner,resetVoiceCaptureCoordinatorForTests} from './voiceCaptureCoordinator'
import {clearVoiceCaptureContext} from './voiceCaptureContext'
import type {SessionWorkspaceState} from './types'
const fixtureVoiceCleanups:Array<()=>void>=[]
const media=Object.getOwnPropertyDescriptor(navigator,'mediaDevices')
afterEach(()=>{cleanup();fixtureVoiceCleanups.splice(0).forEach(fn=>fn());cancelListenReady();revokeOnboardingResponses();revokeOrdinaryResponses();inputs.cancelLocalSpeech();clearVoiceCaptureContext();resetVoiceCaptureCoordinatorForTests();localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals();vi.useRealTimers();if(media)Object.defineProperty(navigator,'mediaDevices',media);else Reflect.deleteProperty(navigator,'mediaDevices')})
const workspace:SessionWorkspaceState={session:{session_id:'public-session',child_id:'public-child',status:'active',state:'check_in',observation_status:'paused',snooze_until:null},planning_context:{energy_level:'medium',fixed_commitment_minutes:0,fixed_commitment_note:null,tomorrow_event_subject:null,tomorrow_event_title:null,tomorrow_event_minutes:0},family_rule:{observation_mode:'shadow'},recalls:[],homework:[],carryover_candidates:[],carryover_decisions:[],plan:null,plan_items:[],drafts:{},latest_tutor_turn:null,sensor_release:{opened_leases:0,active_leases:0,released_leases:0,by_device:{}},daily_report_ready:false}as unknown as SessionWorkspaceState
async function mount(step='wake_practice',saveDelay=0,playbackAmplitude=0,sampleRate=16000,partialManaged=false,nativeStartDelay=0){
 vi.useFakeTimers()
 const processors:Array<any>=[],utterances:Array<any>=[],tones:Array<any>=[],playbackKinds:Array<string>=[],nativeStarted:Array<any>=[]
 class Audio{
  state='running';sampleRate=sampleRate;destination={};get currentTime(){return Date.now()/1000}
  createMediaStreamSource=()=>({connect:vi.fn(),disconnect:vi.fn()})
  createScriptProcessor=(samples:number)=>{const p={samples,connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null};processors.push(p);return p}
  createBuffer=(_c:number,n:number,rate:number)=>({duration:n/rate,getChannelData:()=>new Float32Array(n)})
  createBufferSource=()=>{const listeners=new Set<()=>void>();let timer:any;const node:any={buffer:null,onended:null,connect:vi.fn(),disconnect:vi.fn(),addEventListener:(_type:string,fn:()=>void)=>listeners.add(fn),stop:()=>{clearTimeout(timer);listeners.forEach(fn=>fn())},start:(at?:number)=>{if(at===undefined)tones.push(node);timer=setTimeout(()=>{listeners.forEach(fn=>fn());node.onended?.({isTrusted:true})},Math.max(0,(at??this.currentTime)-this.currentTime)*1000+node.buffer.duration*1000)}};return node}
  resume=async()=>{this.state='running'};close=async()=>{this.state='closed'}
 }
 const track={readyState:'live',enabled:true,stop:vi.fn(),getSettings:()=>({deviceId:'public-mic',echoCancellation:true}),getCapabilities:()=>({echoCancellation:[true,false]}),addEventListener:vi.fn(),removeEventListener:vi.fn()}
 vi.stubGlobal('AudioContext',Audio)
 Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:vi.fn(async()=>({getAudioTracks:()=>[track],getTracks:()=>[track]})),enumerateDevices:vi.fn(async()=>[])}})
 class Utterance{onstart:any;onend:any;onerror:any;voice:any;lang='';rate=1;constructor(public text:string){}}
 const synthesis={getVoices:()=>[{name:'public-local',lang:'zh-CN',localService:true}],speak:(u:Utterance)=>{utterances.push(u);setTimeout(()=>{nativeStarted.push(u);u.onstart?.({isTrusted:true})},nativeStartDelay)},cancel:vi.fn(),speaking:false,pending:false,paused:false}
 vi.stubGlobal('speechSynthesis',synthesis);vi.stubGlobal('SpeechSynthesisUtterance',Utterance)
 let progress:any={childId:'public-child',version:1,step,updatedAt:'2026-10-10T12:00:00Z'}
 const desktop={getWakeWordSettings:vi.fn(async()=>({childId:'public-child',wakeWord:'小知',enabled:true,inactivitySeconds:60,updatedAt:'2026-10-10T12:00:00Z'})),saveWakeWordSettings:vi.fn(async x=>x),getVoiceOnboarding:vi.fn(async()=>progress),saveVoiceOnboarding:vi.fn(async x=>{if(saveDelay)await new Promise(r=>setTimeout(r,saveDelay));progress=x;return x})}
 vi.stubGlobal('desktop',desktop);vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('NETWORK_FORBIDDEN')))
 for (const method of ['readVoiceInteractionHandoff', 'invalidateVoiceInteractionHandoff'] as const) {
   vi.spyOn(localApi, method).mockImplementation(async (session_id, child_id) => ({ session_id, child_id, status: 'none',
     handoff_id: null, agent_action_id: null, deferred_progress: null, request: null, receipt: null, reason: null }))
 }
 vi.spyOn(api,'requestVoiceSynthesis').mockImplementation(async()=>{
   if(!partialManaged)throw new Error('MANAGED_ROUTE_UNAVAILABLE')
   const pcm=new Uint8Array(8+9600),view=new DataView(pcm.buffer);view.setUint32(0,16000,true);view.setUint32(4,9600,true)
   for(let i=0;i<4800;i++)view.setInt16(8+i*2,Math.trunc(2000*Math.sin(i*2*Math.PI*180/16000)),true)
   return new Response(pcm,{status:200,headers:{'Content-Type':'application/x-hsc-pcm16-frame-v2'}})
  })
 fixtureVoiceCleanups.push(voice.subscribeCompanionVoice(state=>{if(state==='speaking')playbackKinds.push(voice.getCompanionPlaybackLease()?.kind??'absent')}))
 vi.spyOn(localApi,'agentExecutionRecoveryIndex').mockImplementation(async(session,child)=>({session_id:session,child_id:child,local_only:true,cloud_transmission_allowed:false,replay_allowed:false,has_more:false,entries:[]}))
 vi.spyOn(localApi,'registerDevice').mockResolvedValue({lease_id:'public-lease',status:'active',expires_at:'later'})
 vi.spyOn(localApi,'releaseDeviceLease').mockResolvedValue({lease_id:'public-lease',status:'released'})
 vi.spyOn(localApi,'voiceInteraction').mockResolvedValue({route:'onboarding',action:'advance',source:'cloud',response_text:'公开合成回复'}as never)
 vi.spyOn(localApi,'agentTurn').mockRejectedValue(new Error('PRACTICE_MUST_NOT_REACH_AGENT'))
 const asr=vi.spyOn(localApi,'transcribeStandby').mockResolvedValue({client_action_id:'wake-public',transcript:'小知',duration_ms:320,model_version:'public-local',status:'completed',persisted:false,cloud_used:false})
 const transcribe=vi.spyOn(localApi,'transcribe').mockResolvedValue({status:'completed',cloud_used:false,local_only:true,transcript:'今天数学学了分数作业是做三道题',model_version:'sherpa-onnx-zipformer-zh-en-2023-11-22'}as never)
 let view:any
 await act(async()=>{view=render(<inputs.HandsFreeVoiceInput sessionId='public-session' childId='public-child' purpose='agent_command' autoStart initialConversationActive onTranscript={vi.fn()}/>);await vi.advanceTimersByTimeAsync(1)})
 for(let i=0;i<100&&!processors.at(-1)?.onaudioprocess;i++)await act(async()=>{await vi.advanceTimersByTimeAsync(1)})
 expect(processors.at(-1)?.onaudioprocess).toBeTypeOf('function')
 const frame=async(amplitude=0)=>{await act(async()=>{const samples=processors.at(-1).samples;await vi.advanceTimersByTimeAsync(samples/sampleRate*1000);processors.at(-1).onaudioprocess({inputBuffer:{getChannelData:()=>Float32Array.from({length:samples},(_,i)=>amplitude*Math.sin((i+1)*2*Math.PI*180/sampleRate))}})})}
 const frames=async(n:number,amplitude=0)=>{for(let i=0;i<n;i++)await frame(amplitude)}
 const end=async()=>{await act(async()=>{await vi.advanceTimersByTimeAsync(1);utterances.at(-1)?.onend?.({isTrusted:true});await vi.advanceTimersByTimeAsync(1)})}
 return {view,frame,frames,end,track,synthesis,playbackAmplitude,playbackKinds,nativeStarted,utterances,tones,desktop,asr,transcribe,progress:()=>progress,processors}
}

// Compile the actual selected production caller in a native Node process;
// jsdom's TextEncoder realm is incompatible with esbuild's native binding.
const compiled=new Map<string,string>()
function callerCode(start:string,end:string):string {
 const source=readFileSync(process.env.ORDINARY_SOURCE_PATH??resolve(process.cwd(),'src/renderer/src/SessionWorkspace.tsx'),'utf8')
 const from=source.indexOf(start),to=source.indexOf(end,from)
 if(from<0||to<0||source.indexOf(start,from+1)>=0)throw Error('ACTUAL_CALLER_REQUIRED')
 const body=source.slice(from,to),old=compiled.get(body)
 if(old)return old
 const script="import {readFileSync} from 'node:fs';import {transformSync} from 'esbuild';process.stdout.write(transformSync(readFileSync(0,'utf8'),{loader:'tsx',target:'es2022'}).code)"
 const code=execFileSync(process.execPath,['--input-type=module','-e',script],{input:body,encoding:'utf8',timeout:10000,maxBuffer:262144})
 compiled.set(body,code);return code
}
function ordinaryCaller(){
 const code=callerCode('function speakOrdinaryPrompt(','\ntype AgentReplySpeechPolicy')
 const ports={ordinaryResponseMayOwnPrompt,ordinaryPromptPlayback,speakLocally:inputs.speakLocally,privacyStopPending:()=>false,tutorialSpeechOwners:new Map()}
 return new Function(...Object.keys(ports),code+';return speakOrdinaryPrompt')(...Object.values(ports))
}
const message='公开合成日常问题：今天感觉怎么样？'
it.each([false,true])('mounted ordinary actual managed prefix $0 fallback preserves response cue',async(partialManaged)=>{
 const h=await mount('completed',0,0,16000,partialManaged)
 const dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 const call=ordinaryCaller()
 await act(async()=>{call(message,workspace.session,{localOnly:false});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.utterances.at(-1)?.text).toBe(message)
 expect(h.nativeStarted.at(-1)?.text).toBe(message)
 if(partialManaged)expect(h.playbackKinds).toContain('managed');else expect(h.playbackKinds).not.toContain('managed')
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 await h.frames(100)
 await h.end()
 expect(ordinaryResponseGrant('public-session','public-child')).not.toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(true)
 dispose()
})

function replyCaller(){
 const code=callerCode('  const speakReply = ','\n  useDailyReflectionSummary')
 const ports={speechSuppressedRef:{current:false},mountedRef:{current:true},qwenRealtimeActive:false,commandGenerationRef:{current:1},ordinarySessionRef:{current:{state:'check_in'}},workspace,
 setVoiceDelivery:vi.fn(),playPublicReply:()=>{throw Error('PUBLIC_REPLY_UNEXPECTED')},ordinaryResponseMayOwnPrompt,ordinaryPromptPlayback,ordinaryTutorPlayback,realtimeSpeechCaptureRef:{current:null},
 lastSpeechRequestedAtRef:{current:0},setVoiceActivationSignal:vi.fn(),privacyStopped:false,tutorialOwnsSpeech:false,playLocalSpeech:inputs.speakLocally,speakLocally:inputs.speakLocally,
 onboardingRef:{current:null},setOnboarding:vi.fn()}
 return new Function(...Object.keys(ports),code+';return speakReply')(...Object.values(ports))
}
it.each([false,true])('real ordinary speakReply caller partial-managed $0 binds actual final playback only',async(partialManaged)=>{
 const h=await mount('completed',0,0,16000,partialManaged),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 await act(async()=>{replyCaller()(message,undefined,{localOnly:false,ordinaryResponseAllowed:true,sourceAgentTurnId:'public-runtime-turn'});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.nativeStarted.at(-1)?.text).toBe(message);if(partialManaged)expect(h.playbackKinds).toContain('managed')
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 await h.frames(100);await h.end()
 expect(ordinaryResponseGrant('public-session','public-child')?.binding).toMatchObject({source_agent_turn_id:'public-runtime-turn'})
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(true);dispose()
})
it.each(['owner','session','duplicate_start','error'])('actual fallback %s cannot produce response readiness',async(kind)=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 let current=true
 await act(async()=>{ordinaryCaller()(message,workspace.session,{localOnly:false,isCurrent:()=>current});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.nativeStarted.at(-1)?.text).toBe(message);expect(h.playbackKinds).toContain('managed')
 await act(async()=>{if(kind==='owner')current=false;if(kind==='session')registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'reflection'});if(kind==='duplicate_start')h.utterances.at(-1).onstart?.({isTrusted:true});if(kind==='error')h.utterances.at(-1).onerror?.({isTrusted:true})})
 await h.frames(100);await h.end()
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})
it('managed-only requirement preserves partial failure and never speaks fallback or grants an answer',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 await act(async()=>{ordinaryCaller()(message,workspace.session,{localOnly:false,managedVoiceRequired:true});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.playbackKinds).toContain('managed');expect(h.utterances).toHaveLength(0)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})
it('no-board Tutor repeated generation cannot renew cancelled receipt or confirm fallback',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 const row={playback_id:'public-receipt',turn_id:'public-tutor',child_id:'public-child',session_id:'public-session',source_version:'public-version',generation:1,status:'reserved',lease_id:null,lease_generation:null,lease_kind:null}
 const receipt=vi.spyOn(localApi,'tutorVoicePlayback').mockImplementation(async(_s,op,body)=>({...row,status:op==='reserve'?'reserved':op==='start'?'started':op==='natural-end'?'finished':'cancelled',...(op==='reserve'?{}:{lease_id:body.lease_id,lease_generation:body.lease_generation,lease_kind:body.lease_kind})})as never)
 await act(async()=>{replyCaller()(message,undefined,{localOnly:false,ordinaryResponseAllowed:true,sourceTutorTurnId:'public-tutor'});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.playbackKinds).toContain('managed');await h.frames(100);await h.end()
 expect(receipt.mock.calls.map(x=>x[1])).toEqual(['reserve','start','cancel','reserve','cancel'])
 expect(h.utterances).toHaveLength(0)
 // This fixture always returns generation 1 and the same row: a fresh reserve
 // ACK is a conflict, never evidence of a newer attempt.
 expect(row.generation).toBe(1)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})

it('original context epoch changed during fallback preparation cannot be refrozen at the later start',async()=>{
 const h=await mount('completed',0,0,16000,true,500),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 await act(async()=>{ordinaryCaller()(message,workspace.session,{localOnly:false});await vi.advanceTimersByTimeAsync(100)})
 expect(h.playbackKinds).toContain('managed');expect(h.utterances.at(-1)?.text).toBe(message)
 clearVoiceCaptureContext('public-session')
 await act(async()=>{await vi.advanceTimersByTimeAsync(600)})
 await h.end()
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})
it('a different actual lease generation at fallback start cannot renew the original prompt',async()=>{
 const h=await mount('completed',0,0,16000,true,500),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 await act(async()=>{ordinaryCaller()(message,workspace.session,{localOnly:false});await vi.advanceTimersByTimeAsync(100)})
 expect(h.playbackKinds).toContain('managed');expect(h.utterances.at(-1)?.text).toBe(message)
 const get=voice.getCompanionPlaybackLease
 vi.spyOn(voice,'getCompanionPlaybackLease').mockImplementation(()=>{const lease=get();return lease?.kind==='local_sapi'?{...lease,generation:lease.generation+1}:lease})
 await act(async()=>{await vi.advanceTimersByTimeAsync(600)})
 await h.end()
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})

it('explicit cancel in the fallback window never revives the old capture grant',async()=>{
 const h=await mount('completed',0,0,16000,true,500),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 await act(async()=>{ordinaryCaller()(message,workspace.session,{localOnly:false});await vi.advanceTimersByTimeAsync(100)})
 expect(h.playbackKinds).toContain('managed');inputs.cancelLocalSpeech()
 await act(async()=>{await vi.advanceTimersByTimeAsync(600)});await h.end()
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})
it('unavailable local voice keeps partial managed delivery failed without a response grant',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 h.synthesis.getVoices=()=>[]
 await act(async()=>{ordinaryCaller()(message,workspace.session,{localOnly:false});await vi.advanceTimersByTimeAsync(1800)})
 expect(h.playbackKinds).toContain('managed');expect(h.utterances).toHaveLength(0)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})
it('a successor prompt owns its own final reply while an obsolete fallback is rejected',async()=>{
 const h=await mount('completed',0,0,16000,true,500),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 await act(async()=>{replyCaller()(message,undefined,{localOnly:false,ordinaryResponseAllowed:true,sourceAgentTurnId:'old-runtime-turn'});await vi.advanceTimersByTimeAsync(100)})
 expect(h.playbackKinds).toContain('managed')
 await act(async()=>{replyCaller()('新公开合成提示',undefined,{localOnly:true,ordinaryResponseAllowed:true,sourceAgentTurnId:'new-runtime-turn'});await vi.advanceTimersByTimeAsync(1200)})
 // The real dialogue arbiter queues the successor until actual old end.
 expect(h.nativeStarted.at(-1)?.text).toBe(message)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 expect(h.transcribe).not.toHaveBeenCalled()
 const old=h.utterances.at(-1);await h.end()
 await act(async()=>{await vi.advanceTimersByTimeAsync(700)})
 expect(h.nativeStarted.at(-1)?.text).toBe('新公开合成提示')
 await act(async()=>{old.onend?.({isTrusted:true})})
 expect(voice.getCompanionPlaybackLease()?.text).toBe('新公开合成提示')
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 await h.frames(100);await h.end()
 expect(ordinaryResponseGrant('public-session','public-child')?.binding).toMatchObject({source_agent_turn_id:'new-runtime-turn'})
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(true);dispose()
})

it('invalid prompt scope leaves no live companion voice subscription',()=>{
 const watch=vi.spyOn(voice,'subscribeCompanionVoice')
 const permit=ordinaryPromptPlayback({sessionId:'missing-session',childId:'public-child',state:'check_in',text:message,prompt:{},isCurrent:()=>false})
 expect(watch).not.toHaveBeenCalled();expect(permit.options.isCurrent?.()).toBe(false)
 permit.started();expect(watch).not.toHaveBeenCalled();expect(ordinaryResponseGrant('missing-session','public-child')).toBeNull()
})
