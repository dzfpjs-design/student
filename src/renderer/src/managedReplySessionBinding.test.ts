import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import { playPublicReply } from './publicReplyPlayback'
import { queueManagedReply, cancelLocalSpeech } from './LocalInputs'
import { getCompanionPlaybackLease, getCompanionVoiceState, stopCompanionSpeech, subscribeCompanionVoice } from './companionVoice'
import { ordinaryPromptPlayback, ordinaryResponseGrant, ordinaryResponseMayOwnPrompt, registerOrdinarySession, revokeOrdinaryResponses } from './ordinaryResponseGrant'
import { clearVoiceCaptureContext, setVoiceActivityCapture } from './voiceCaptureContext'

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
it('actual public → queue → companion → ordinary started preserves canonical session and natural whole PCM end',async()=>{
 const t=await publicAttempt();expect(t.starts[0]).toMatchObject({state:'speaking',lease:{sessionId:sid,kind:'managed'}})
 expect(t.scope.grant.playingCurrent()).toBe(true);expect(t.states).toContain('preparing');expect(t.states.at(-1)).toBe('speaking')
 await vi.waitFor(()=>expect(contexts[0].sources).toHaveLength(2));expect(t.main.receipts).toEqual(['started']);expect(ordinaryResponseGrant(sid,child)).toBeNull()
 contexts[0].currentTime=contexts[0].sources[0].tail;contexts[0].sources[0].dispatchEvent(new Event('ended'))
 await Promise.resolve();expect(t.main.receipts).toEqual(['started']);expect(t.scope.grant.playingCurrent()).toBe(true)
 finishAll();await vi.waitFor(()=>expect(t.main.receipts).toEqual(['started','rendered']));expect(t.results).not.toHaveBeenCalledWith('managed')
 t.main.ack();await t.promise;await vi.waitFor(()=>expect(t.results).toHaveBeenCalledWith('managed'))
 expect(getCompanionVoiceState()).toBe('idle');expect(ordinaryResponseGrant(sid,child)).toBe(t.scope.grant);expect(t.main.bridge.cancelPublicReplySpeech).not.toHaveBeenCalled()
})
it('canonical queue session overrides an incompatible caller option without mutating caller options',async()=>{
 nativeFixture();let lease:any;const options={localPlaybackSessionId:'SYNTHETIC-wrong-option'}
 const promise=queueManagedReply({id:'SYNTHETIC-provider',isProductionVoice:true,async *stream(){yield {pcm16:new Int16Array([250,250]).buffer,sampleRate:24000}}},text,sid,()=>{lease=getCompanionPlaybackLease()},options)
 await vi.waitFor(()=>expect(lease).toBeTruthy());expect(lease.sessionId).toBe(sid);expect(options.localPlaybackSessionId).toBe('SYNTHETIC-wrong-option')
 finishAll();expect(await promise).toBe('managed')
})
it('an ordinary grant for a different session still revokes at actual managed start',async()=>{
 const t=await publicAttempt({grantSid:'SYNTHETIC-other-session'});expect(t.starts[0].lease.sessionId).toBe(sid)
 expect(t.scope.grant.playbackCurrent()).toBe(false);await t.promise;expect(t.main.receipts).toEqual(['started','unconfirmed']);expect(t.results).not.toHaveBeenCalledWith('managed')
})
it('changed frozen capture stamp still cancels current managed playback',async()=>{
 const t=await publicAttempt({onStarted:()=>setVoiceActivityCapture(sid,child,null)});await t.promise
 expect(t.scope.grant.playbackCurrent()).toBe(false);expect(t.main.receipts).toEqual(['started','unconfirmed']);expect(t.results).not.toHaveBeenCalledWith('managed')
})
it('changed registered child/session state still cancels current managed playback',async()=>{
 const t=await publicAttempt({onStarted:()=>{disposers.push(registerOrdinarySession({sessionId:sid,childId:'SYNTHETIC-other-child',state:'study'}))}});await t.promise
 expect(t.scope.grant.playbackCurrent()).toBe(false);expect(t.main.receipts).toEqual(['started','unconfirmed']);expect(t.results).not.toHaveBeenCalledWith('managed')
})
it('runtime retirement still interrupts the original speech key without rendered credit',async()=>{
 const t=await publicAttempt();t.main.retire();await t.promise
 expect(t.main.receipts).toEqual(['started','interrupted']);expect(t.scope.grant.playbackCurrent()).toBe(false);expect(t.results).not.toHaveBeenCalledWith('managed')
})
it('truncated native ended events never become natural whole PCM completion',async()=>{
 const t=await publicAttempt();await vi.waitFor(()=>expect(contexts[0].sources).toHaveLength(2));contexts[0].sources[0].dispatchEvent(new Event('ended'));await t.promise
 expect(t.main.receipts).toEqual(['started','unconfirmed']);expect(t.results).not.toHaveBeenCalledWith('managed');expect(ordinaryResponseGrant(sid,child)).toBeNull()
})


it('real SessionWorkspace public caller passes through the strict current session and delivery acknowledgment',async()=>{
 nativeFixture();const main=mainFixture(),started=vi.fn(),finished=vi.fn(),delivery=vi.fn();let task:Promise<unknown>|undefined
 disposers.push(registerOrdinarySession({sessionId:sid,childId:child,state:'check_in'}))
 const source=readFileSync(resolve('src/renderer/src/SessionWorkspace.tsx'),'utf8'),begin='  const speakReply = ',end='\n  useDailyReflectionSummary'
 const from=source.indexOf(begin),to=source.indexOf(end,from);expect(from).toBeGreaterThan(-1);expect(to).toBeGreaterThan(from);expect(source.indexOf(begin,from+1)).toBe(-1)
 const ports={speechSuppressedRef:{current:false},mountedRef:{current:true},qwenRealtimeActive:true,commandGenerationRef:{current:1},ordinarySessionRef:{current:{state:'check_in'}},workspace:{session:{session_id:sid,child_id:child}},
  setVoiceDelivery:delivery,playPublicReply:(...args:Parameters<typeof playPublicReply>)=>{task=playPublicReply(...args);return task},ordinaryResponseMayOwnPrompt,ordinaryPromptPlayback,realtimeSpeechCaptureRef:{current:null},
  lastSpeechRequestedAtRef:{current:0},setVoiceActivationSignal:vi.fn(),privacyStopped:false,tutorialOwnsSpeech:false,playLocalSpeech:()=>{throw Error('UNEXPECTED_FALLBACK')},speakLocally:()=>{throw Error('UNEXPECTED_FALLBACK')},ordinaryTutorPlayback:()=>{throw Error('UNEXPECTED_TUTOR')},onboardingRef:{current:null},setOnboarding:vi.fn()}
 // Compile the current caller in Node, keeping esbuild outside jsdom's realm.
 const code=execFileSync(process.execPath,['--input-type=module','-e',
  "import {readFileSync} from 'node:fs';import {transformSync} from 'esbuild';process.stdout.write(transformSync(readFileSync(0,'utf8'),{loader:'tsx',target:'es2022'}).code)"],
  {input:source.slice(from,to),encoding:'utf8',timeout:5000,maxBuffer:64000})+';return speakReply'
 const reply=new Function(...Object.keys(ports),code)(...Object.values(ports))
 reply(text,finished,{localOnly:true,sourceAgentTurnId:'SYNTHETIC-runtime-turn',ordinaryResponseAllowed:true,onPlaybackStart:()=>started(getCompanionPlaybackLease())})
 await vi.waitFor(()=>expect(started).toHaveBeenCalledOnce());expect(started.mock.calls[0][0]).toMatchObject({sessionId:sid,kind:'managed'})
 await vi.waitFor(()=>expect(contexts[0].sources).toHaveLength(2));expect(finished).not.toHaveBeenCalled();expect(ordinaryResponseGrant(sid,child)).toBeNull()
 finishAll();await vi.waitFor(()=>expect(main.receipts).toEqual(['started','rendered']));expect(finished).not.toHaveBeenCalled();main.ack();await task
 await vi.waitFor(()=>expect(finished).toHaveBeenCalledOnce());expect(delivery).toHaveBeenLastCalledWith('natural');expect(ordinaryResponseGrant(sid,child)?.binding.source_agent_turn_id).toBe('SYNTHETIC-runtime-turn')
})
