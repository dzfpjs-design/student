import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { playPublicReply } from './publicReplyPlayback'
import { queueManagedReply, cancelLocalSpeech } from './LocalInputs'
import { getCompanionPlaybackLease, getCompanionVoiceState, stopCompanionSpeech, subscribeCompanionVoice } from './companionVoice'
import { ordinaryPromptPlayback, ordinaryResponseGrant, ordinaryResponseMayOwnPrompt, registerOrdinarySession, revokeOrdinaryResponses } from './ordinaryResponseGrant'
import { clearVoiceCaptureContext, setVoiceActivityCapture } from './voiceCaptureContext'

import { bindPublicReplyProviderMetadata, bindManagedPlaybackSourceMetadata, confirmManagedPlaybackSourceMetadata, readManagedPlaybackSourceMetadata } from './managedPlaybackSourceMetadata'

// All audio, main IPC and records below are explicit synthetic fixtures. Actual
// renderer functions own the complete callback/order/lease/revocation path.
const sid='SYNTHETIC-current-session', child='SYNTHETIC-child', text='公开合成语音播放验证'
const disposers:Array<()=>void>=[]
let contexts: Context[]=[]
class Context {
  currentTime=0; state='running'; destination={}; sources:Array<any>=[]
  close=vi.fn(async()=>{this.state='closed'})
  constructor(){contexts.push(this)}
  createBuffer(_channels:number,length:number,sampleRate:number){return {duration:length/sampleRate,length,sampleRate,numberOfChannels:_channels,getChannelData:()=>new Float32Array(length)}}
  createBufferSource(){const source=Object.assign(new EventTarget(),{context:this,buffer:null as any,tail:0,startState:null as any,beforeStartMetadata:null as any,connect:vi.fn(),disconnect:vi.fn(),stop:vi.fn(),start:vi.fn()})
    source.start.mockImplementation((when:number)=>{source.startState=getCompanionVoiceState();source.beforeStartMetadata=readManagedPlaybackSourceMetadata(source as unknown as AudioBufferSourceNode);source.tail=when+source.buffer.duration})
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

it('first native source has exact preparing producer binding while public getLease remains null',async()=>{
 const t=await publicAttempt();const c=contexts[0],source=c.sources[0],before=source.beforeStartMetadata
 expect(source.startState).toBe('preparing');expect(before).toMatchObject({session_id:sid,child_id:child,agent_turn_id:'SYNTHETIC-runtime-turn',main_lease_id:'SYNTHETIC-main-lease',pre_start_lease_kind:'preparing',source_start_confirmed:false,native_origin_verified:false,natural_end_verified:false})
 expect(before.companion_lease_id).not.toBe(before.main_lease_id);expect(before.companion_lease_id).toBe(t.starts[0].lease.id)
 expect(before.companion_generation).toBe(t.starts[0].lease.generation);expect(t.starts[0].state).toBe('speaking');expect(t.scope.grant.playingCurrent()).toBe(true)
 finishAll();await vi.waitFor(()=>expect(t.main.receipts).toEqual(['started','rendered']));t.main.ack();await t.promise
})
it('each original block confirms the same managed lease only after original speaking callback, retaining completed identity',async()=>{
 const t=await publicAttempt();await vi.waitFor(()=>expect(contexts[0].sources).toHaveLength(2));const nodes=contexts[0].sources,rows=nodes.map(readManagedPlaybackSourceMetadata)
 expect(rows.every(Boolean)).toBe(true);expect(rows[0]).toMatchObject({source_start_confirmed:true,companion_lease_id:t.starts[0].lease.id,companion_generation:t.starts[0].lease.generation,session_id:sid})
 expect(rows[1]).toMatchObject({source_start_confirmed:true,pre_start_lease_kind:'managed',context_ordinal:rows[0]!.context_ordinal,companion_lease_id:rows[0]!.companion_lease_id});expect(rows[0]!.source_ordinal).not.toBe(rows[1]!.source_ordinal)
 finishAll();await vi.waitFor(()=>expect(t.main.receipts).toEqual(['started','rendered']));t.main.ack();await t.promise
 expect(getCompanionPlaybackLease()).toBeNull();expect(readManagedPlaybackSourceMetadata(nodes[0])).toEqual(rows[0]);expect(rows[0]!.native_origin_verified).toBe(false)
})
it('untagged same-shape source or changed actual buffer cannot borrow another original source identity',async()=>{
 const t=await publicAttempt();const s=contexts[0].sources[0],before=readManagedPlaybackSourceMetadata(s)
 expect(readManagedPlaybackSourceMetadata({...s})).toBeNull();const old=s.buffer;s.buffer={...old};expect(readManagedPlaybackSourceMetadata(s)).toBeNull();s.buffer=old;expect(readManagedPlaybackSourceMetadata(s)).toEqual(before)
 finishAll();await vi.waitFor(()=>expect(t.main.receipts).toEqual(['started','rendered']));t.main.ack();await t.promise
})
it('wrong current ordinary session still revokes; source identity never grants delivery',async()=>{
 const t=await publicAttempt({grantSid:'SYNTHETIC-other-session'});await t.promise;const s=contexts[0].sources[0],row=readManagedPlaybackSourceMetadata(s)
 expect(t.scope.grant.playbackCurrent()).toBe(false);expect(row?.native_origin_verified??false).toBe(false);expect(row?.natural_end_verified??false).toBe(false);expect(t.main.receipts).toEqual(['started','unconfirmed']);expect(t.results).not.toHaveBeenCalledWith('managed')
})
it('readonly surface has no registration reset source creation or native claim capability',()=>{
 const surface=(window as any).__hscManagedPlaybackSourceMetadata;expect(Object.keys(surface)).toEqual(['readSource']);expect(Object.isFrozen(surface)).toBe(true)
 const d=Object.getOwnPropertyDescriptor(window,'__hscManagedPlaybackSourceMetadata')!;expect(d.writable).toBe(false);expect(d.configurable).toBe(false);expect(surface.readSource({})).toBeNull()
})
function helperFixture(overrides:Record<string,unknown>={}) {
 const provider={},context={},buffer={},source={context,buffer} as AudioBufferSourceNode,lease={id:'SYNTHETIC-companion-lease',generation:7,sessionId:sid,kind:'preparing'}
 const pub={mainLeaseId:'SYNTHETIC-public-lease',sessionId:sid,childId:child,agentTurnId:'SYNTHETIC-source-turn',speechKey:'runtime-public-SYNTHETIC-source-turn'}
 bindPublicReplyProviderMetadata(provider,pub)
 const options={localPlaybackSessionId:sid,speechKey:pub.speechKey,localOnly:false,...overrides};bindManagedPlaybackSourceMetadata(source,context as AudioContext,buffer as AudioBuffer,provider,lease,options)
 return {provider,context,buffer,source,lease,pub,options}
}
it('source identity retains two distinct lease roles and immutable payload without PCM or text',()=>{
 const f=helperFixture(),m=readManagedPlaybackSourceMetadata(f.source)!;expect(m).not.toBeNull();expect(m.main_lease_id).toBe(f.pub.mainLeaseId);expect(m.companion_lease_id).toBe(f.lease.id);expect(m.source_start_confirmed).toBe(false)
 expect(Object.isFrozen(m)).toBe(true);expect(Object.keys(m).some(k=>/(?:^|_)(?:pcm|text|audio|supplier|api_key|config|token)(?:_|$)/.test(k)&&k!=='speech_key')).toBe(false)
 confirmManagedPlaybackSourceMetadata(f.source,{...f.lease,kind:'managed'},'preparing');expect(readManagedPlaybackSourceMetadata(f.source)!.source_start_confirmed).toBe(false)
 confirmManagedPlaybackSourceMetadata(f.source,{...f.lease,kind:'managed'},'speaking');expect(readManagedPlaybackSourceMetadata(f.source)!.source_start_confirmed).toBe(true)
})
it.each([{localPlaybackSessionId:'SYNTHETIC-other-session'},{speechKey:'runtime-public-SYNTHETIC-other-turn'},{localOnly:true}])('wrong provider source scope rejects without touching source %o',overrides=>{
 const f=helperFixture(overrides);expect(readManagedPlaybackSourceMetadata(f.source)).toBeNull();expect(f.source.context).toBe(f.context);expect(f.source.buffer).toBe(f.buffer)
})
it('same values on a different provider object cannot inherit binding; wrong generation cannot confirm',()=>{
 const f=helperFixture(),other={context:f.context,buffer:f.buffer} as AudioBufferSourceNode;bindManagedPlaybackSourceMetadata(other,f.context as AudioContext,f.buffer as AudioBuffer,{},f.lease,f.options);expect(readManagedPlaybackSourceMetadata(other)).toBeNull()
 confirmManagedPlaybackSourceMetadata(f.source,{...f.lease,kind:'managed',generation:8},'speaking');expect(readManagedPlaybackSourceMetadata(f.source)!.source_start_confirmed).toBe(false)
})
it('metadata property access failure is contained and cannot write native source state',()=>{
 const provider={};expect(()=>bindPublicReplyProviderMetadata(provider,new Proxy({} as any,{get(){throw Error('METADATA_ONLY_FAILURE')}}))).not.toThrow()
 const f=helperFixture();Object.defineProperty(f.source,'context',{get(){throw Error('METADATA_ONLY_FAILURE')}});expect(()=>confirmManagedPlaybackSourceMetadata(f.source,{...f.lease,kind:'managed'},'speaking')).not.toThrow();expect(readManagedPlaybackSourceMetadata(f.source)).toBeNull()
})
