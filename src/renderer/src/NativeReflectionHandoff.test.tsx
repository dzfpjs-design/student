/// <reference types="node" />
import {act,cleanup,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {readFileSync} from 'node:fs'
import type {ComponentProps} from 'react'
import {NativeRealtimeCapabilityGateway,signNativeRealtimeCapability} from '../../main/native-realtime-capabilities'
import {ownedVoiceRuntimePolicyDesktop} from './test/ownedVoiceRuntimePolicy'
import {App} from './App'
import {SessionWorkspace} from './SessionWorkspace'
import {privacyStopKey} from './deviceCommandReceipt'
import {localApi,resetRuntimeCache} from './api'
import {resetNativeClientExecutionsForTests} from './nativeClientExecution'
import type {QwenRealtimeVoiceInput} from './QwenRealtimeVoiceInput'
import type {QwenNativeCapabilityProposal} from './qwenNativeProtocol'
import * as inputs from './LocalInputs'
const h=vi.hoisted(()=>({props:null as ComponentProps<typeof QwenRealtimeVoiceInput>|null,unmounts:0,retire:()=>undefined as void}))
vi.mock('./QwenRealtimeVoiceInput',async()=>{const {useEffect}=await import('react');return {QwenRealtimeVoiceInput:(props:ComponentProps<typeof QwenRealtimeVoiceInput>)=>{h.props=props;useEffect(()=>()=>{h.unmounts++;h.retire()},[]);return null}}})
vi.mock('./VoiceCalibrationPanel',async()=>{const {useEffect}=await import('react');return {VoiceCalibrationPanel:({onMicrophoneBindingReadyChange}:{onMicrophoneBindingReadyChange:(v:boolean)=>void})=>{useEffect(()=>{onMicrophoneBindingReadyChange(true)},[onMicrophoneBindingReadyChange]);return null}}})
vi.mock('./DocumentCameraCapture',()=>({DocumentCameraCapture:()=>null}))
vi.mock('./ContinuousDocumentMonitor',()=>({ContinuousDocumentMonitor:()=>null}))
vi.mock('./LocalObservation',()=>({LocalObservation:()=>null}))
const file=process.env.HSC_NATIVE_REFLECTION_TCP_FIXTURE
const f=file?JSON.parse(readFileSync(file,'utf8')):{url:'',token:'',session:'',child:''} as {url:string;token:string;session:string;child:string}
const net=globalThis.fetch
const runId=crypto.randomUUID();let serial=0,host:string,gateway:NativeRealtimeCapabilityGateway
let sid:string,child:string,failAck=false,saves=0
const calls:Array<{phase:unknown;lease:unknown;body:unknown}>=[]
const event={sender:{id:71}},owner={status:'ready',localApiUrl:f.url,sessionToken:f.token,runtimeEpoch:1,child:{},revision:0}
async function raw(path:string,body:unknown,method='POST'){const r=await net(f.url+'/v1'+path,{method,headers:{'X-HSC-Session':f.token,'Content-Type':'application/json'},body:JSON.stringify(body)});const x=await r.json();if(!r.ok)throw Error(path+' '+r.status+' '+JSON.stringify(x));return x}
beforeEach(async()=>{
 cleanup();localStorage.clear();resetRuntimeCache();resetNativeClientExecutionsForTests();calls.length=0;h.props=null;h.unmounts=0;saves=0;failAck=false
 const bootstrap=await (await net(f.url+'/v1/bootstrap',{headers:{'X-HSC-Session':f.token}})).json();const s=bootstrap.active_session??await raw('/sessions',{child_id:f.child,client_action_id:'public-r12-'+runId+'-'+serial++});sid=s.session_id;child=s.child_id
 let state=s.state
 for(const next of ['classroom_recall','homework_capture','plan_review','study','reflection']){
  if(state===next||state==='reflection')continue
  await raw('/sessions/'+sid+'/transition',{expected_state:state,target_state:next,reason:'公开合成复盘准备',client_action_id:'public-r12-stage-'+sid+'-'+next});state=next
  if(next==='classroom_recall')await raw('/sessions/'+sid+'/recalls/none',{client_action_id:'public-r12-no-recall-'+sid})
  if(next==='homework_capture')await raw('/sessions/'+sid+'/homework/none',{client_action_id:'public-r12-no-homework-'+sid})
 }
 gateway??=new NativeRealtimeCapabilityGateway({allowed:e=>e.sender.id===71,current:()=>owner,policy:async()=>({capture:'configured',runtimeEpoch:1}),sign:p=>signNativeRealtimeCapability('cd'.repeat(32),p),fetch:async(url,init)=>{const r=await net(url,init);if(!r.ok)console.error('PUBLIC_SYNTHETIC_NATIVE_REJECTION',r.status,await r.clone().text());return r}})
 host=gateway.register(event);h.retire=()=>gateway.close(event,host)
 vi.stubGlobal('desktop',ownedVoiceRuntimePolicyDesktop({
  getQwenRealtimeSettings:async()=>({configured:true,enabled:true}),
  getWakeWordSettings:async(id:string)=>({childId:id,wakeWord:id===child?'小知':'小语',enabled:true,inactivitySeconds:60}),
  getVoiceOnboarding:async()=>({childId:child,version:1,step:'completed',updatedAt:new Date().toISOString()}),
  forwardQwenNativeRuntime:async(request:Parameters<NativeRealtimeCapabilityGateway['forward']>[1])=>{
   if(request.operation==='client'){calls.push({phase:request.body?.phase,lease:request.completion_lease,body:request.body});if(failAck&&request.body?.phase==='complete')throw Error('PUBLIC_ACK_RESPONSE_UNAVAILABLE')}
   return gateway.forward(event,request)
  }
 },{status:'ready',localApiUrl:f.url,sessionToken:f.token,runtimeEpoch:1},net))
 vi.spyOn(localApi,'installationSetupStatus').mockResolvedValue(null as never) // already-onboarded UI boundary; no install flow under test
 const realComplete=localApi.completeReflection;vi.spyOn(localApi,'completeReflection').mockImplementation(async(...args)=>{saves++;return realComplete(...args)})
 vi.spyOn(inputs,'speakLocally').mockImplementation((_t,_s,done)=>done?.('text_only'))
 vi.spyOn(inputs,'cancelLocalSpeech').mockImplementation(()=>undefined)
 // Camera preview hardware is outside this reflection handoff check.
 const track={kind:'audio',readyState:'live',stop:vi.fn(),getSettings:()=>({echoCancellation:true}),getCapabilities:()=>({echoCancellation:[true]}),applyConstraints:async()=>undefined,addEventListener:vi.fn(),removeEventListener:vi.fn()}
 Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{enumerateDevices:async()=>[{kind:'audioinput',deviceId:'default',groupId:'public-mic',label:'Default Microphone'}],getUserMedia:async()=>({getAudioTracks:()=>[track],getTracks:()=>[track]}),addEventListener:vi.fn(),removeEventListener:vi.fn()}})
 vi.stubGlobal('AudioContext',class{sampleRate=16000;destination={};createMediaStreamSource=()=>({connect:vi.fn(),disconnect:vi.fn()});createScriptProcessor=()=>({connect:vi.fn(),disconnect:vi.fn(),onaudioprocess:null});resume=async()=>undefined;close=async()=>undefined})
})
afterEach(()=>{cleanup();vi.useRealTimers();gateway?.close(event,host);vi.restoreAllMocks();vi.unstubAllGlobals()})
async function mount(){
 await act(async()=>{render(<App/>)})
 await waitFor(()=>expect(h.props?.nativeRuntime).toBeDefined(),{timeout:6000})
 await h.props!.nativeRuntime!.loadCatalog(host);await h.props!.nativeRuntime!.loadContext(host)
}
async function command(capability:string,args:Record<string,unknown>,text:string){
 const runtime=h.props!.nativeRuntime!,n=runId+'-'+serial++
 const voice=await runtime.admit(text,{source:'accepted_onset',sessionId:sid,localInputId:'public-r12-onset-'+n,capturedAt:Date.now(),reference:{child_id:child},carryoverConfirmationId:null},host)
 const p:QwenNativeCapabilityProposal={version:1,host_connection_id:host,voice_input_id:voice.voice_input_id,supplier_session_id:'public-supplier-'+n,input_item_id:'public-item-'+n,response_id:'public-response-'+n,call_id:'public-call-'+n,capability_name:capability,args}
 let done=false,result:Awaited<ReturnType<typeof runtime.propose>>|undefined
 await act(async()=>{void runtime.propose(p).then(r=>{result=r;done=true},()=>{done=true})})
 await waitFor(()=>expect(done).toBe(true),{timeout:20000})
 return result
}
for(const fault of [false,true])it.skipIf(!file)(`real reflection save retires its voice owner and returns Home standby when ACK ${fault?'is unavailable':'succeeds'}`,async()=>{
 await mount()
 const update=await command('reflection.update',{field:'learned',value:'公开合成：今天知道了地球自转。'},'今天我学会了地球自转')
 expect(update?.execution.status).toBe('completed')
 calls.length=0;failAck=fault
 await command('reflection.complete',{},'保存今天的复盘')
 await waitFor(()=>expect(calls.filter(c=>c.phase==='complete'&&(c.body as {tool_status?:string})?.tool_status==='completed').length).toBeGreaterThan(0),{timeout:2500})
 const completePacket=calls.find(c=>c.phase==='complete'&&(c.body as {tool_status?:string})?.tool_status==='completed')!.body as {result?:{execution_receipt?:{output?:unknown}}}
 expect(completePacket.result?.execution_receipt?.output).toEqual({session_id:sid,child_id:child,session_status:'completed',daily_report_ready:expect.any(Boolean),client_action_id:'reflection-close:'+sid})
 await waitFor(()=>expect(document.querySelector('.child-home-surface')).not.toBeNull(),{timeout:6000})
 expect(screen.getByText('今天的复盘已保存')).toBeInTheDocument()
 await waitFor(()=>expect(document.querySelector('.household-standby-voice')).toHaveTextContent('待机中'),{timeout:5000})
 expect(h.unmounts).toBe(1);expect(saves).toBe(1)
 const saved=await localApi.sessionWorkspace(sid)
 expect(saved.session.status).toBe('completed');expect(saved.session.state).toBe('completed')
 const acks=calls.filter(c=>c.phase==='complete');expect(acks).toHaveLength(fault?2:1)
 for(const ack of acks)expect(ack.lease).toEqual(expect.any(String))
 if(fault)expect(acks[0].body).toEqual(acks[1].body)
 const keys=Object.keys(localStorage).filter(k=>k.startsWith('hsc-native-reflection-close-v1:'))
 expect(keys).toHaveLength(fault?1:0)
 if(fault){const pending=JSON.parse(localStorage.getItem(keys[0])!);expect(pending.status).toBe('unconfirmed');expect(pending.receipt).toEqual({session_id:sid,child_id:child,session_status:'completed',daily_report_ready:saved.daily_report_ready,client_action_id:'reflection-close:'+sid});expect(Object.keys(localStorage).some(k=>k.startsWith('hsc-native-completion-v1:'))).toBe(true)}
 expect(screen.queryByText('操作没有保存，请重试。')).not.toBeInTheDocument()
})

it.skipIf(!file)('the existing eight-second completed fallback cannot leave a privacy-stopped workspace',async()=>{
 const completed=await localApi.sessionWorkspace(f.session)
 expect(completed.session.status).toBe('completed')
 localStorage.setItem(privacyStopKey(completed.session.session_id),'privacy-stop')
 const back=vi.fn();vi.useFakeTimers()
 await act(async()=>{render(<SessionWorkspace initial={completed} childName="公开同学" childGrade="四年级" onBack={back}/>)})
 await act(async()=>{await vi.advanceTimersByTimeAsync(8500)})
 expect(back).not.toHaveBeenCalled()
})
