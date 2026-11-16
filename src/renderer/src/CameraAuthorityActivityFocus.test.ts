import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import { playPublicReply } from './publicReplyPlayback'
import { queueManagedReply, cancelLocalSpeech } from './LocalInputs'
import { getCompanionPlaybackLease, getCompanionVoiceState, stopCompanionSpeech, subscribeCompanionVoice } from './companionVoice'
import { ordinaryPromptPlayback, ordinaryResponseGrant, ordinaryResponseMayOwnPrompt, registerOrdinarySession, revokeOrdinaryResponses } from './ordinaryResponseGrant'
import { clearVoiceCaptureContext, setVoiceActivityCapture, setVoiceQuestionCapture, voiceActivityCaptureEpoch, voiceCaptureReference } from './voiceCaptureContext'
import { createActivityFocusReader, type ActivityVoiceFocus } from './activityVoiceFocus'

// All audio, main IPC and records below are explicit synthetic fixtures. Actual
// renderer functions own the complete callback/order/lease/revocation path.
const sid='SYNTHETIC-current-session', child='SYNTHETIC-child', text='公开合成语音播放验证'
const disposers:Array<()=>void>=[]
let contexts: Context[]=[]
class Context {
  currentTime=0; state='running'; destination={}; sources:Array<any>=[]
  close=vi.fn(async()=>{this.state='closed'})
  constructor(){contexts.push(this)}
  createBuffer(_channels:number,length:number,sampleRate:number){return {duration:length/sampleRate,getChannelData:()=>new Float32Array(length)}}
  createBufferSource(){const source=Object.assign(new EventTarget(),{buffer:null as any,tail:0,connect:vi.fn(),disconnect:vi.fn(),stop:vi.fn(),start:vi.fn()})
    source.start.mockImplementation((when:number)=>{source.tail=when+source.buffer.duration})
    this.sources.push(source);return source}
}
afterEach(()=>{cancelLocalSpeech();revokeOrdinaryResponses();stopCompanionSpeech();for(const f of disposers.splice(0))f();clearVoiceCaptureContext();contexts=[];vi.unstubAllGlobals();vi.restoreAllMocks()})
function nativeFixture(){vi.stubGlobal('AudioContext',Context);vi.stubGlobal('speechSynthesis',{cancel:vi.fn(),getVoices:()=>[],speak:vi.fn()})}
function finishAll(){for(const c of contexts){c.currentTime=Math.max(...c.sources.map(s=>s.tail),c.currentTime);for(const s of c.sources)s.dispatchEvent(new Event('ended'))}}
function mainFixture(){const receipts:string[]=[];let retire:(id:string)=>void=()=>{};let ack:()=>void=()=>{}
 const bridge={openPublicReplySpeech:vi.fn(async()=>({status:'eligible',leaseId:'SYNTHETIC-main-lease'})),
  generatePublicReplySpeech:vi.fn(async()=>({leaseId:'SYNTHETIC-main-lease',sampleRate:24000,generationCompleted:true,renderedCompleted:false,frameCount:2,sampleCount:16000,pcmBase64:btoa(String.fromCharCode(...new Uint8Array(new Int16Array(16000).fill(250).buffer)))})),
  settlePublicReplySpeech:vi.fn(async(_id:string,event:string)=>{receipts.push(event);if(event==='rendered')await new Promise<void>(resolve=>{ack=resolve})}),cancelPublicReplySpeech:vi.fn(async()=>{}),onPublicReplySpeechRetired:vi.fn((fn:(id:string)=>void)=>{retire=fn;return()=>{}})}
 Object.defineProperty(window,'desktop',{configurable:true,value:bridge});return {bridge,receipts,ack:()=>ack(),retire:()=>retire('SYNTHETIC-main-lease')}
}
function ordinary(sessionId=sid){disposers.push(registerOrdinarySession({sessionId,childId:child,state:'check_in'}))
 return ordinaryPromptPlayback({sessionId,childId:child,state:'check_in',prompt:{publicSynthetic:true},text,sourceAgentTurnId:'SYNTHETIC-runtime-turn',deliveryPending:true,isCurrent:()=>true})}
async function publicAttempt({grantSid=sid,poisoned=false,onStarted}:{grantSid?:string,poisoned?:boolean,onStarted?:()=>void}={}){
 nativeFixture();const main=mainFixture(),scope=ordinary(grantSid),results=vi.fn(),starts:any[]=[];const states:string[]=[]
 disposers.push(subscribeCompanionVoice(next=>states.push(next)))
 const promise=playPublicReply({sessionId:sid,childId:child,agentTurnId:'SYNTHETIC-runtime-turn'},text,
  {sourceAgentTurnId:'SYNTHETIC-runtime-turn',...(poisoned?{localPlaybackSessionId:'SYNTHETIC-untrusted-option'}:{}),
   isCurrent:()=>scope.options.isCurrent!(),onPlaybackEnd:()=>scope.grant.confirmDelivery()},results,
  ()=>{starts.push({state:getCompanionVoiceState(),lease:getCompanionPlaybackLease()});scope.started();onStarted?.()},()=>scope.grant.naturalEnd())
 await vi.waitFor(()=>expect(starts).toHaveLength(1));return {main,scope,results,starts,states,promise}
}

const focus={activity_id:'SYNTHETIC-activity',version:'SYNTHETIC-v1',last_turn_id:'SYNTHETIC-turn'}
function actualCameraRefresh(reader:ReturnType<typeof createActivityFocusReader>, getFocus:()=>ActivityVoiceFocus['activity_focus'], selectedChild=child) {
 const source=readFileSync(resolve('src/renderer/src/SessionWorkspace.tsx'),'utf8')
 const extract=(start:string,end:string)=>{const a=source.indexOf(start),b=source.indexOf(end,a);expect(a).toBeGreaterThan(-1);expect(b).toBeGreaterThan(a);expect(source.indexOf(start,a+1)).toBe(-1);return source.slice(a,b)}
 const code=extract('  const refreshActivityFocus = async ', '\n  useLayoutEffect(() => {\n    activityMounted.current = true')+'\n'+extract('  const refresh = async (isCurrent:', '\n\n  const runOwner = ')
 const scope=selectedChild+':'+sid,initial={session:{session_id:sid,child_id:selectedChild}},workspace={session:{...initial.session,status:'active',state:'check_in',observation_status:'enabled'}}
 const localApi={activityVoiceFocus:vi.fn(async()=>({session_id:sid,child_id:selectedChild,activity_focus:getFocus(),local_only:true,cloud_transmission_allowed:false})),sessionWorkspace:vi.fn(async()=>workspace)}
 const ports={activityScope:scope,activityFocusReader:{current:reader},initial,localApi,activityMounted:{current:true},activityScopeRef:{current:scope},workspace,setWorkspace:vi.fn()}
 const compiled=execFileSync(process.execPath,['--input-type=module','-e',"import {readFileSync} from 'node:fs';import {transformSync} from 'esbuild';process.stdout.write(transformSync(readFileSync(0,'utf8'),{loader:'tsx',target:'es2022'}).code)"],{input:code,encoding:'utf8',timeout:5000,maxBuffer:64000})
 const handler='current => refresh(current, true)'
 expect(source).toContain('onAuthorityRefresh={'+handler+'}')
 return {refresh:new Function(...Object.keys(ports),compiled+';return '+handler)(...Object.values(ports)) as (current:()=>boolean)=>Promise<void>,localApi,workspaceWrites:ports.setWorkspace}
}
const envelope=(value:ActivityVoiceFocus['activity_focus'],c=child):ActivityVoiceFocus=>({session_id:sid,child_id:c,activity_focus:value,local_only:true,cloud_transmission_allowed:false})
async function primed(value:ActivityVoiceFocus['activity_focus']) {const reader=createActivityFocusReader();expect(await reader.refresh(sid,child,async()=>envelope(value),()=>true)).toBe(true);return reader}

it.each([null,focus])('two original camera-authority refreshes of the same full focus preserve epoch and actual managed whole delivery: %s',async(value)=>{
 const reader=await primed(value),epoch=voiceActivityCaptureEpoch(sid),t=await publicAttempt()
 const caller=actualCameraRefresh(reader,()=>value?{last_turn_id:value.last_turn_id,activity_id:value.activity_id,version:value.version}:null)
 await caller.refresh(()=>true);await caller.refresh(()=>true)
 expect(caller.localApi.activityVoiceFocus).toHaveBeenCalledTimes(2);expect(caller.localApi.sessionWorkspace).toHaveBeenCalledTimes(2);expect(caller.workspaceWrites).toHaveBeenCalledTimes(2)
 expect(voiceActivityCaptureEpoch(sid)).toBe(epoch);expect(t.scope.grant.playingCurrent()).toBe(true)
 await vi.waitFor(()=>expect(contexts[0].sources).toHaveLength(2));finishAll();await vi.waitFor(()=>expect(t.main.receipts).toEqual(['started','rendered']));t.main.ack();await t.promise
 await vi.waitFor(()=>expect(t.results).toHaveBeenCalledWith('managed'));expect(ordinaryResponseGrant(sid,child)?.binding.source_agent_turn_id).toBe('SYNTHETIC-runtime-turn')
})
it.each([
 ['activity', {...focus,activity_id:'SYNTHETIC-other'}],['version',{...focus,version:'SYNTHETIC-v2'}],['turn',{...focus,last_turn_id:'SYNTHETIC-next'}],['withdrawn',null]
] as const)('original authority refresh changed %s still invalidates the old managed voice',async(_label,next)=>{
 const reader=await primed(focus),epoch=voiceActivityCaptureEpoch(sid),t=await publicAttempt(),caller=actualCameraRefresh(reader,()=>next)
 await caller.refresh(()=>true);expect(voiceActivityCaptureEpoch(sid)).toBe(epoch+1);expect(t.scope.grant.playbackCurrent()).toBe(false);await t.promise
 expect(t.main.receipts).toEqual(['started','unconfirmed']);expect(t.results).not.toHaveBeenCalledWith('managed');expect(voiceCaptureReference(sid,child).activity_reference).toEqual(next??undefined)
})
it('same-null cross-child authority cannot inherit the prior registered child',async()=>{
 const reader=await primed(null),epoch=voiceActivityCaptureEpoch(sid),t=await publicAttempt(),caller=actualCameraRefresh(reader,()=>null,'SYNTHETIC-child-b')
 await caller.refresh(()=>true);expect(voiceActivityCaptureEpoch(sid)).toBe(epoch+1);expect(t.scope.grant.playbackCurrent()).toBe(false);await t.promise
 expect(t.main.receipts).toEqual(['started','unconfirmed'])
})
it('first null registration, including question-only metadata, must establish an activity epoch',async()=>{
 for(const hasQuestionOwner of [false,true]){clearVoiceCaptureContext();if(hasQuestionOwner)setVoiceQuestionCapture(sid,child,null);const reader=createActivityFocusReader(),epoch=voiceActivityCaptureEpoch(sid)
 expect(await reader.refresh(sid,child,async()=>envelope(null),()=>true)).toBe(true);expect(voiceActivityCaptureEpoch(sid)).toBe(epoch+1)}
})
it('a private owner change hidden by the nullable public reference cannot skip synchronization',async()=>{
 const reader=await primed(null),epoch=voiceActivityCaptureEpoch(sid);setVoiceQuestionCapture(sid,'SYNTHETIC-child-b',null)
 expect(voiceCaptureReference(sid,child).activity_reference).toBeUndefined();expect(voiceActivityCaptureEpoch(sid)).toBe(epoch)
 expect(await reader.refresh(sid,child,async()=>envelope(null),()=>true)).toBe(true);expect(voiceActivityCaptureEpoch(sid)).toBe(epoch+1)
})
it('explicit same-null setter retains its original strong retirement and is not the new synchronization API',async()=>{
 await primed(null);const epoch=voiceActivityCaptureEpoch(sid),t=await publicAttempt();setVoiceActivityCapture(sid,child,null)
 expect(voiceActivityCaptureEpoch(sid)).toBe(epoch+1);expect(t.scope.grant.playbackCurrent()).toBe(false);await t.promise;expect(t.main.receipts).toEqual(['started','unconfirmed'])
})
it('load errors and invalid/cross-child replies stay false and preserve only the prior valid capture; no cached success',async()=>{
 const reader=await primed(focus),epoch=voiceActivityCaptureEpoch(sid)
 expect(await reader.refresh(sid,child,async()=>{throw Error('PUBLIC_SYNTHETIC_READ_FAILED')},()=>true)).toBe(false)
 expect(await reader.refresh(sid,child,async()=>envelope(focus,'SYNTHETIC-child-b'),()=>true)).toBe(false)
 expect(await reader.refresh(sid,child,async()=>({...envelope(focus),activity_focus:{...focus,extra:'rejected'}}),()=>true)).toBe(false)
 expect(voiceActivityCaptureEpoch(sid)).toBe(epoch);expect(voiceCaptureReference(sid,child).activity_reference).toEqual(focus)
})
it('a late successful same-value response cannot revive a privacy-cleared capture',async()=>{
 const reader=await primed(focus);let done!:(v:ActivityVoiceFocus)=>void;const pending=reader.refresh(sid,child,()=>new Promise(r=>done=r),()=>true)
 clearVoiceCaptureContext(sid);const epoch=voiceActivityCaptureEpoch(sid);done(envelope(focus));expect(await pending).toBe(false);expect(voiceActivityCaptureEpoch(sid)).toBe(epoch);expect(voiceCaptureReference(sid,child).activity_reference).toBeUndefined()
})
