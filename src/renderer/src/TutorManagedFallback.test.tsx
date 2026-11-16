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
async function mount(step='wake_practice',saveDelay=0,playbackAmplitude=0,sampleRate=16000,partialManaged=false,nativeStartDelay=0,completeManaged=false){
 vi.useFakeTimers()
 const processors:Array<any>=[],utterances:Array<any>=[],tones:Array<any>=[],playbackKinds:Array<string>=[],nativeStarted:Array<any>=[],managedNodes:Array<any>=[]
 class Audio{
  state='running';sampleRate=sampleRate;destination={};get currentTime(){return Date.now()/1000}
  createMediaStreamSource=()=>({connect:vi.fn(),disconnect:vi.fn()})
  createScriptProcessor=(samples:number)=>{const p={samples,connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null};processors.push(p);return p}
  createBuffer=(_c:number,n:number,rate:number)=>({duration:n/rate,getChannelData:()=>new Float32Array(n)})
  createBufferSource=()=>{const listeners=new Set<()=>void>();let timer:any;const node:any={buffer:null,onended:null,connect:vi.fn(),disconnect:vi.fn(),addEventListener:(_type:string,fn:()=>void)=>listeners.add(fn),stop:()=>{clearTimeout(timer);listeners.forEach(fn=>fn())},start:(at?:number)=>{if(at===undefined)tones.push(node);else managedNodes.push(node);timer=setTimeout(()=>{listeners.forEach(fn=>fn());node.onended?.({isTrusted:true})},Math.ceil(Math.max(0,(at??this.currentTime)-this.currentTime)*1000+node.buffer.duration*1000))}};node.emitEnded=()=>{listeners.forEach(fn=>fn());node.onended?.({isTrusted:true})};return node}
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
 vi.spyOn(api,'requestVoiceSynthesis').mockImplementation(async(_session,_text,requestId)=>{
   if(!partialManaged)throw new Error('MANAGED_ROUTE_UNAVAILABLE')
   const pcm=new Uint8Array(8+9600),view=new DataView(pcm.buffer);view.setUint32(0,16000,true);view.setUint32(4,9600,true)
   for(let i=0;i<4800;i++)view.setInt16(8+i*2,Math.trunc(2000*Math.sin(i*2*Math.PI*180/16000)),true)
   if(completeManaged){const end=new TextEncoder().encode(JSON.stringify({type:'generation_completed',request_id:requestId,frame_count:1,pcm_bytes:9600,sample_count:4800})),wire=new Uint8Array(pcm.length+8+end.length),header=new DataView(wire.buffer);wire.set(pcm);header.setUint32(pcm.length+4,end.length,true);wire.set(end,pcm.length+8);return new Response(wire,{status:200,headers:{'Content-Type':'application/x-hsc-pcm16-frame-v2'}})}
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
 return {view,frame,frames,end,track,synthesis,playbackAmplitude,playbackKinds,nativeStarted,utterances,tones,desktop,asr,transcribe,progress:()=>progress,processors,managedNodes}
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
function replyCaller(){
 const code=callerCode('  const speakReply = ','\n  useDailyReflectionSummary')
 const ports={speechSuppressedRef:{current:false},mountedRef:{current:true},qwenRealtimeActive:false,commandGenerationRef:{current:1},ordinarySessionRef:{current:{state:'check_in'}},workspace,
 setVoiceDelivery:vi.fn(),playPublicReply:()=>{throw Error('PUBLIC_REPLY_UNEXPECTED')},ordinaryResponseMayOwnPrompt,ordinaryPromptPlayback,ordinaryTutorPlayback,realtimeSpeechCaptureRef:{current:null},
 lastSpeechRequestedAtRef:{current:0},setVoiceActivationSignal:vi.fn(),privacyStopped:false,tutorialOwnsSpeech:false,playLocalSpeech:inputs.speakLocally,speakLocally:inputs.speakLocally,
 onboardingRef:{current:null},setOnboarding:vi.fn()}
 return new Function(...Object.keys(ports),code+';return speakReply')(...Object.values(ports))
}

function receiptServer(delay:Partial<Record<'reserve'|'start'|'cancel'|'natural-end',number|((count:number)=>number)>>={}){
 const rows:Array<import('./ordinaryResponseGrant').TutorVoicePlaybackReceipt>=[],events:Array<{op:string;body:Record<string,unknown>}>=[]
 const receipt=vi.spyOn(localApi,'tutorVoicePlayback').mockImplementation(async(_s,op,body,signal)=>{
  events.push({op,body:{...body}})
  const configured=delay[op],ms=typeof configured==='function'?configured(events.filter(event=>event.op===op).length):(configured??0)
  if(ms)await new Promise(resolve=>setTimeout(resolve,ms))
  if(op==='reserve'){
   for(const row of rows)if(['reserved','started'].includes(row.status))row.status='cancelled'
   const row={playback_id:`public-receipt-${rows.length+1}`,turn_id:'public-tutor',child_id:'public-child',session_id:'public-session',source_version:'public-version',generation:rows.length+1,status:'reserved',lease_id:null,lease_generation:null,lease_kind:null}as import('./ordinaryResponseGrant').TutorVoicePlaybackReceipt
   rows.push(row);return {...row}
  }
  const row=rows.find(row=>row.playback_id===body.playback_id)
  if(!row||row.source_version!==body.source_version||row.generation!==body.generation)throw Error('RECEIPT_IDENTITY_MISMATCH')
  const same=()=>['lease_id','lease_generation','lease_kind'].every(key=>(row as any)[key]===body[key])
  if(op==='start'){
   if(row.status!=='reserved'&&!(row.status==='started'&&same()))throw Error('START_STATE_INVALID')
   Object.assign(row,{status:'started',lease_id:body.lease_id,lease_generation:body.lease_generation,lease_kind:body.lease_kind})
  }else if(op==='cancel'){
   if((row.status!=='reserved'||['lease_id','lease_generation','lease_kind'].some(key=>body[key]!=null))&&!same())throw Error('CANCEL_TUPLE_MISMATCH')
   row.status='cancelled'
  }else{
   if(row.status!=='started'||!same())throw Error('END_STATE_INVALID')
   row.status='finished'
  }
  return {...row}
 })
 return {rows,events,receipt}
}
const tutorPolicy={localOnly:false,ordinaryResponseAllowed:true,sourceTutorTurnId:'public-tutor'}
it('actual Tutor managed prefix cancels exact old receipt and admits final SAPI before response authority',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer(),before=vi.fn(async()=>true)
 await act(async()=>{replyCaller()(message,undefined,{...tutorPolicy,beforePlayback:before});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.playbackKinds).toContain('managed');expect(h.nativeStarted.at(-1)?.text).toBe(message)
 expect(before).toHaveBeenCalledTimes(1)
 expect(server.events.map(x=>x.op)).toEqual(['reserve','start','cancel','reserve','start'])
 expect(server.rows).toMatchObject([{status:'cancelled',lease_kind:'managed_audio'},{status:'started',lease_kind:'local_sapi'}])
 expect(server.rows[1].generation).toBeGreaterThan(server.rows[0].generation)
 expect(server.rows[1].lease_id).toBe(server.rows[0].lease_id)
 expect(server.rows[1].lease_generation).toBe(server.rows[0].lease_generation)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 await h.frames(100);await h.end()
 expect(server.rows).toMatchObject([{status:'cancelled'},{status:'finished',lease_kind:'local_sapi'}])
 expect(server.events.at(-1)).toMatchObject({op:'natural-end',body:{playback_id:'public-receipt-2'}})
 expect(ordinaryResponseGrant('public-session','public-child')?.binding).toMatchObject({source_tutor_turn_id:'public-tutor',source_tutor_voice_playback_id:'public-receipt-2'})
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(true)
 dispose()
})
it('provider failure before any PCM prefix keeps the original admission single-use',async()=>{
 const h=await mount('completed'),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer(),before=vi.fn(async()=>true)
 await act(async()=>{replyCaller()(message,undefined,{...tutorPolicy,beforePlayback:before});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.playbackKinds).not.toContain('managed');expect(before).toHaveBeenCalledTimes(1)
 expect(server.events.map(x=>x.op)).toEqual(['reserve','start'])
 await h.end();expect(server.events.map(x=>x.op)).toEqual(['reserve','start','natural-end'])
 expect(ordinaryResponseGrant('public-session','public-child')?.binding.source_tutor_voice_playback_id).toBe('public-receipt-1');dispose()
})

it.each(['start','cancel','reserve']as const)('unknown %s ACK stops fallback; a late reservation is cancelled without granting audio',async(operation)=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'})
 const server=receiptServer({[operation]:operation==='reserve'?(count:number)=>count===2?6000:0:6000})
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(5500)})
 expect(h.playbackKinds).toContain('managed');expect(h.utterances).toHaveLength(0)
 expect(server.events.filter(e=>e.op==='reserve')).toHaveLength(operation==='reserve'?2:1)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 await act(async()=>{await vi.advanceTimersByTimeAsync(13000)})
 if(operation==='reserve')expect(server.rows).toMatchObject([{status:'cancelled'},{status:'cancelled'}])
 // Unknown start cannot fabricate cancellation of an unobserved backend state.
 if(operation==='start')expect(server.events.map(x=>x.op)).toEqual(['reserve','start'])
 expect(server.events.some(x=>x.op==='natural-end')).toBe(false)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false);dispose()
})
it('one five-second fallback budget includes both old cancellation and fresh reservation',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer({cancel:3000,reserve:count=>count===2?3000:0})
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(5500)})
 expect(server.events.map(x=>x.op)).toEqual(['reserve','start','cancel','reserve']);expect(h.utterances).toHaveLength(0)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 await act(async()=>{await vi.advanceTimersByTimeAsync(7000)})
 expect(server.rows).toMatchObject([{status:'cancelled'},{status:'cancelled'}]);dispose()
})
it.each(['context','cancel','child']as const)('%s change while old cancellation waits prevents fresh admission',async(change)=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer({cancel:1000})
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(100)})
 expect(server.events.map(x=>x.op)).toEqual(['reserve','start','cancel'])
 if(change==='cancel')inputs.cancelLocalSpeech()
 if(change==='child')registerOrdinarySession({sessionId:'public-session',childId:'other-child',state:'check_in'})
 if(change==='context'){
  const context=await import('./voiceCaptureContext')
  context.setVoiceQuestionCapture('public-session','public-child',{kind:'diagnostic',binding:{prompt_id:'public-new-question',item_version:1}}as any)
 }
 await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
 expect(server.events.filter(x=>x.op==='reserve')).toHaveLength(1);expect(h.utterances).toHaveLength(0)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();dispose()
})
it.each(['start','cancel','reserve']as const)('mismatched %s ACK never admits or confirms fallback',async(operation)=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer()
 const actual=server.receipt.getMockImplementation()!
 server.receipt.mockImplementation(async(...args)=>{
  const value=await actual(...args)
  if(args[1]===operation&&(operation!=='reserve'||server.rows.length===2))return {...value,source_version:'changed-source'}as never
  return value
 })
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(1500)})
 expect(h.utterances).toHaveLength(0);expect(server.events.some(x=>x.op==='natural-end')).toBe(false)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();dispose()
})
it('managed voice requirement never invokes fallback admission after a true prefix',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer()
 await act(async()=>{replyCaller()(message,undefined,{...tutorPolicy,managedVoiceRequired:true});await vi.advanceTimersByTimeAsync(1000)})
 expect(h.playbackKinds).toContain('managed');expect(h.utterances).toHaveLength(0)
 expect(server.events.filter(x=>x.op==='reserve')).toHaveLength(1)
 expect(server.events.some(x=>x.op==='natural-end')).toBe(false);expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();dispose()
})
it('interrupting the fresh SAPI attempt cancels only its receipt and late native end has no authority',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer()
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(1000)})
 expect(server.rows).toMatchObject([{status:'cancelled'},{status:'started'}]);const native=h.utterances.at(-1)
 inputs.cancelLocalSpeech()
 await act(async()=>{native.onend?.({isTrusted:true});await vi.advanceTimersByTimeAsync(100)})
 expect(server.rows).toMatchObject([{status:'cancelled'},{status:'cancelled'}])
 expect(server.events.filter(x=>x.op==='natural-end')).toHaveLength(0)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();dispose()
})
it('late native managed source end cannot finish the fresh SAPI receipt',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer()
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(1000)})
 expect(h.managedNodes.length).toBeGreaterThan(0)
 await act(async()=>{h.managedNodes[0].emitEnded();await vi.advanceTimersByTimeAsync(100)})
 expect(server.events.some(x=>x.op==='natural-end')).toBe(false);expect(ordinaryResponseGrant('public-session','public-child')).toBeNull()
 await h.end();expect(server.events.at(-1)).toMatchObject({op:'natural-end',body:{playback_id:'public-receipt-2'}});dispose()
})
it('pending final end ACK withholds the cue and confirmation does not renew the response deadline',async()=>{
 const h=await mount('completed',0,0,16000,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer({'natural-end':4000})
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(1000)})
 await h.frames(100);await h.end()
 expect(server.rows[1].status).toBe('started');expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(false)
 await act(async()=>{await vi.advanceTimersByTimeAsync(4100)})
 expect(server.rows[1].status).toBe('finished');expect(ordinaryResponseGrant('public-session','public-child')).not.toBeNull()
 expect(listenReadyPending('public-session','public-child',getVoiceCaptureOwner()!)).toBe(true)
 await act(async()=>{await vi.advanceTimersByTimeAsync(4000)})
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();dispose()
})
it('different actual SAPI lease generation cannot bind the fresh reservation',async()=>{
 const h=await mount('completed',0,0,16000,true,500),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer()
 await act(async()=>{replyCaller()(message,undefined,tutorPolicy);await vi.advanceTimersByTimeAsync(100)})
 expect(server.rows).toMatchObject([{status:'cancelled'},{status:'reserved'}])
 const actual=voice.getCompanionPlaybackLease
 vi.spyOn(voice,'getCompanionPlaybackLease').mockImplementation(()=>{const lease=actual();return lease?.kind==='local_sapi'?{...lease,generation:lease.generation+1}:lease})
 await act(async()=>{await vi.advanceTimersByTimeAsync(1000)})
 await h.end()
 expect(server.rows).toMatchObject([{status:'cancelled'},{status:'cancelled'}]);expect(server.events.some(x=>x.op==='natural-end')).toBe(false)
 expect(ordinaryResponseGrant('public-session','public-child')).toBeNull();dispose()
})
it('a fully completed managed stream keeps its original receipt and does not invoke fallback admission',async()=>{
 const h=await mount('completed',0,0,16000,true,0,true),dispose=registerOrdinarySession({sessionId:'public-session',childId:'public-child',state:'check_in'}),server=receiptServer(),before=vi.fn(async()=>true)
 await act(async()=>{replyCaller()(message,undefined,{...tutorPolicy,beforePlayback:before,managedVoiceRequired:true});await vi.advanceTimersByTimeAsync(1000)})
 expect(before).toHaveBeenCalledTimes(1);expect(h.playbackKinds).toContain('managed');expect(h.utterances).toHaveLength(0)
 expect(server.events.map(x=>x.op)).toEqual(['reserve','start','natural-end'])
 expect(server.rows).toMatchObject([{status:'finished',lease_kind:'managed_audio'}])
 expect(ordinaryResponseGrant('public-session','public-child')?.binding.source_tutor_voice_playback_id).toBe('public-receipt-1');dispose()
})
