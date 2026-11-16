/// <reference types="node" />
import {act,cleanup,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {readFileSync} from 'node:fs'
import type {ComponentProps} from 'react'
import {NativeRealtimeCapabilityGateway,signNativeRealtimeCapability} from '../../main/native-realtime-capabilities'
import {ownedVoiceRuntimePolicyDesktop} from './test/ownedVoiceRuntimePolicy'
import {SessionWorkspace} from './SessionWorkspace'
import {localApi} from './api'
import {resetNativeClientExecutionsForTests} from './nativeClientExecution'
import type {QwenRealtimeVoiceInput} from './QwenRealtimeVoiceInput'
import type {QwenNativeCapabilityProposal} from './qwenNativeProtocol'
import * as inputs from './LocalInputs'
const h=vi.hoisted(()=>({props:null as ComponentProps<typeof QwenRealtimeVoiceInput>|null,mounts:0}))
vi.mock('./QwenRealtimeVoiceInput',()=>({QwenRealtimeVoiceInput:(props:ComponentProps<typeof QwenRealtimeVoiceInput>)=>{h.props=props;return null}}))
vi.mock('./VoiceCalibrationPanel',async()=>{const {useEffect}=await import('react');return {VoiceCalibrationPanel:({onMicrophoneBindingReadyChange}:{onMicrophoneBindingReadyChange:(v:boolean)=>void})=>{useEffect(()=>{onMicrophoneBindingReadyChange(true)},[onMicrophoneBindingReadyChange]);return null}}})
vi.mock('./DocumentCameraCapture',()=>({DocumentCameraCapture:()=>null}))
vi.mock('./ContinuousDocumentMonitor',()=>({ContinuousDocumentMonitor:()=>null}))
const fixtureFile=process.env.HSC_NATIVE_MISTAKE_TCP_FIXTURE
const f=fixtureFile?JSON.parse(readFileSync(fixtureFile,'utf8')):{url:'',token:'',session:'',child:''} as {url:string;token:string;session:string;child:string;mistake_id:string}
const net=globalThis.fetch
const packetTrace:Array<{operation:string;phase?:unknown;body?:Record<string,unknown>}>=[]
let host:string,serial=0
const runId=crypto.randomUUID()
let gateway:NativeRealtimeCapabilityGateway
const event={sender:{id:71}},owner={status:'ready',localApiUrl:f.url,sessionToken:f.token,runtimeEpoch:1,child:{},revision:0}
beforeEach(async()=>{
 cleanup();localStorage.clear();resetNativeClientExecutionsForTests();packetTrace.length=0;h.props=null
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:0,y:80,top:80,bottom:180,left:0,right:600,width:600,height:100,toJSON(){return {}}})
 gateway??=new NativeRealtimeCapabilityGateway({allowed:e=>e.sender.id===71,current:()=>owner,policy:async()=>({capture:'configured',runtimeEpoch:1}),sign:p=>signNativeRealtimeCapability('cd'.repeat(32),p),fetch:net})
 host=gateway.register(event)
 vi.stubGlobal('desktop',ownedVoiceRuntimePolicyDesktop({
  getQwenRealtimeSettings:async()=>({configured:true,enabled:true}),
  getWakeWordSettings:async()=>({childId:f.child,wakeWord:'小知',enabled:true,inactivitySeconds:60}),
  getVoiceOnboarding:async()=>({childId:f.child,version:1,step:'completed',updatedAt:new Date().toISOString()}),
  forwardQwenNativeRuntime:async(request:Parameters<NativeRealtimeCapabilityGateway['forward']>[1])=>{packetTrace.push({operation:request.operation,phase:request.body?.phase,body:request.body});const result=await gateway.forward(event,request);return result}
 },{status:'ready',localApiUrl:f.url,sessionToken:f.token,runtimeEpoch:1},net))
 vi.spyOn(inputs,'speakLocally').mockImplementation((_t,_s,done)=>done?.('text_only'))
 vi.spyOn(inputs,'cancelLocalSpeech').mockImplementation(()=>undefined)
})
afterEach(()=>{cleanup();gateway.close(event,host);vi.restoreAllMocks();vi.unstubAllGlobals()})
async function mount(){
 const initial=await localApi.sessionWorkspace(f.session),bootstrap=await localApi.bootstrap(),child=bootstrap.children.find(c=>c.child_id===f.child)!
 await act(async()=>{render(<SessionWorkspace initial={initial} activeChild={child} childName={child.nickname} childGrade={child.grade} onBack={vi.fn()}/>)})
 await waitFor(()=>expect(h.props?.nativeRuntime).toBeDefined())
 await h.props!.nativeRuntime!.loadCatalog(host);await h.props!.nativeRuntime!.loadContext(host)
}
async function command(capability:string,args:Record<string,unknown>,text:string){
 const runtime=h.props!.nativeRuntime!,n=runId+'-'+(++serial)
 const voice=await runtime.admit(text,{source:'accepted_onset',sessionId:f.session,localInputId:'public-onset-'+n,capturedAt:Date.now(),reference:{child_id:f.child},carryoverConfirmationId:null},host)
 const p:QwenNativeCapabilityProposal={version:1,host_connection_id:host,voice_input_id:voice.voice_input_id,supplier_session_id:'public-supplier-'+n,input_item_id:'public-item-'+n,response_id:'public-response-'+n,call_id:'public-call-'+n,capability_name:capability,args}
 let result!:Awaited<ReturnType<typeof runtime.propose>>
 let done=false
 await act(async()=>{void runtime.propose(p).then(value=>{result=value;done=true})})
 await waitFor(()=>expect(done).toBe(true),{timeout:18000})
 return {result,p}
}
it.skipIf(!fixtureFile)('real signed native select renders the current mistake, waits for protected playback end, then completes; already-open same-version select works again',async()=>{
 await mount()
 const observations:string[]=[],rendered:unknown[]=[]
 const receive=(event:Event)=>{const d=(event as CustomEvent<import('./mistakeLearningTypes').MistakeLearningRendered>).detail;if(d.kind!=='library'&&d.child_id===f.child&&d.mistake_id===f.mistake_id){rendered.push(d);observations.push('case-rendered')}}
 window.addEventListener('hsc-mistake-learning-rendered',receive)
 let playbackEnd:(()=>void)|undefined
 vi.mocked(inputs.speakLocally).mockImplementation((_text,_session,onResult,onStart,options)=>{
  observations.push('speech-meta:'+JSON.stringify({localOnly:options?.localOnly,source:!!options?.sourceAgentTurnId,skip:(options as Record<string,unknown>|undefined)?.skipPublicReplyProvider}))
  if(options?.localOnly&&(options as Record<string,unknown>).skipPublicReplyProvider===true){
   expect(rendered.length).toBeGreaterThan(0)
   expect((options as typeof options&{skipPublicReplyProvider?:boolean}).skipPublicReplyProvider).toBe(true)
   observations.push('protected-playback-start');onStart?.();onResult?.('managed')
   playbackEnd=()=>{observations.push('protected-playback-end');options.onPlaybackEnd?.()};return
  }
  onResult?.('text_only')
 })
 try{
  for(let n=0;n<2;n++){
   playbackEnd=undefined;rendered.length=0;const before=packetTrace.length
   const pending=command('mistake.learn',{operation:'select',selector:1},n?'继续看刚才这道错题':'打开第一道错题')
   try{await waitFor(()=>expect(playbackEnd).toBeTypeOf('function'),{timeout:4000})}catch(error){console.log('PUBLIC_ONLY_FLOW_METADATA',JSON.stringify({observations,phases:packetTrace.filter(x=>x.operation==='client').map(x=>x.phase)}));throw error}
   expect(screen.getByText('公开选择题：1加1是多少？',{selector:'p[data-mistake-learning-question]'})).toBeVisible()
   expect(packetTrace.slice(before).filter(x=>x.operation==='client').map(x=>x.phase)).toEqual(['claim','execute'])
   await act(async()=>{playbackEnd!()})
   const {result,p}=await pending
   expect(result.execution.status).toBe('completed')
   const steps=packetTrace.slice(before).filter(x=>x.operation==='client');expect(steps.map(x=>x.phase)).toEqual(['claim','execute','complete'])
   const complete=steps[2].body!,saved=complete.result as {local_delivery:{status:string;kind:string};client_bridge_receipt:{output:{mistake_learning_effect_id:string;view_rendered:boolean}}}
   expect(saved.local_delivery).toEqual({status:'ended',kind:'protected_runtime_speech'})
   expect(saved.client_bridge_receipt.output).toMatchObject({mistake_learning_effect_id:expect.any(String),view_rendered:true})
   const previous=packetTrace.length;await h.props!.nativeRuntime!.readReceipt(p);expect(packetTrace.slice(previous).some(x=>x.operation==='client')).toBe(false)
   expect(observations.indexOf('case-rendered')).toBeLessThan(observations.indexOf('protected-playback-start'))
  }
  const book=screen.getByText('智能错题本').closest('.mistake-book-shell')!
  expect(book.textContent).not.toMatch(/核对评分依据|掌握门槛|错因候选|家长发布/)
 }finally{window.removeEventListener('hsc-mistake-learning-rendered',receive)}
},30000)
