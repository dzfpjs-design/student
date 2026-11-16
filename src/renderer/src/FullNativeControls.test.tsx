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
const fixtureFile=process.env.HSC_FULL_NATIVE_TCP_FIXTURE
const f=fixtureFile?JSON.parse(readFileSync(fixtureFile,'utf8')):{url:'',token:'',session:'',child:''} as {url:string;token:string;session:string;child:string}
const net=globalThis.fetch
const packetTrace:Array<{operation:string;phase?:unknown;body?:Record<string,unknown>}>=[]
let host:string,serial=0
const runId=crypto.randomUUID()
let gateway:NativeRealtimeCapabilityGateway
const event={sender:{id:71}},owner={status:'ready',localApiUrl:f.url,sessionToken:f.token,runtimeEpoch:1,child:{},revision:0}
beforeEach(async()=>{
 cleanup();localStorage.clear();resetNativeClientExecutionsForTests();packetTrace.length=0;h.props=null
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
it.skipIf(!fixtureFile)('runs original MistakeBook render/load ACK and returns to learning while retaining the same cloud voice host',async()=>{
 localStorage.setItem('hsc-pending-evaluation-v1:'+f.session,'CORRUPT_ORIGINAL_EVALUATION')
 await mount();const original=h.props
 const {result,p}=await command('workspace.navigate',{destination:'mistake_book'},'帮我打开错题本')
 expect(result.execution.status).toBe('completed')
 expect(screen.getByText('智能错题本')).toBeVisible();expect(h.props!.sessionId).toBe(original!.sessionId)
 const phases=packetTrace.filter(x=>x.operation==='client').map(x=>x.phase);expect(phases).toEqual(['claim','complete'])
 const complete=packetTrace.find(x=>x.phase==='complete')!.body!;expect(JSON.stringify(complete)).toContain('workspace_opened')
 const previous=packetTrace.length;await h.props!.nativeRuntime!.readReceipt(p)
 expect(packetTrace.slice(previous).some(x=>x.operation==='client')).toBe(false)
 const returned=await command('workspace.navigate',{destination:'learning'},'返回学习')
 expect(returned.result.execution.status).toBe('completed');expect(screen.getByText('智能错题本')).not.toBeVisible()
 expect(h.props!.sessionId).toBe(f.session)
 expect(localStorage.getItem('hsc-pending-evaluation-v1:'+f.session)).toBe('CORRUPT_ORIGINAL_EVALUATION')
 expect(packetTrace.some(x=>['agentTurn','voiceInteraction'].includes(x.operation))).toBe(false)
})
it.skipIf(!fixtureFile)('uses the canonical server prepared turn once and sends its actual host delivery ACK without calling the legacy semantic planner',async()=>{
 await mount();const planner=vi.spyOn(localApi,'agentTurn'),ordinaryOutcome=vi.spyOn(globalThis,'fetch')
 const {result}=await command('system.inspect_learning',{scope:'progress',track:'all'},'现在学习进行到哪里了')
 expect(result.execution.status).toBe('completed')
 expect(packetTrace.filter(x=>x.operation==='client').map(x=>x.phase)).toEqual(['claim','execute','complete'])
 expect(planner).not.toHaveBeenCalled()
 expect(ordinaryOutcome.mock.calls.some(([url])=>String(url).includes('/agent-turns/')&&String(url).includes('/outcome'))).toBe(false)
})

it.skipIf(!fixtureFile)('checks only the requested document camera through the real frame clock and saves its actual source-specific receipt',async()=>{
 await mount()
 const lease=await localApi.registerDevice(f.session,'document_camera','d'.repeat(64),{synthetic:true,width:640,height:480})
 await localApi.confirmDeviceFrame(lease.lease_id,640,480)
 const {CAMERA_DEVICE_STATUS,CameraFrameClock}=await import('./cameraDeviceRuntime')
 const seen:string[]=[],clock=new CameraFrameClock(),track={readyState:'live',enabled:true,muted:false}
 const stream={getVideoTracks:()=>[track]} as unknown as MediaStream
 let frames=1
 const video={srcObject:stream,paused:false,ended:false,readyState:2,videoWidth:640,videoHeight:480,currentTime:1,getVideoPlaybackQuality:()=>({totalVideoFrames:frames})} as unknown as HTMLVideoElement
 clock.read('document_camera',video,stream,true,null);frames++
 const receiver=(event:Event)=>{const detail=(event as CustomEvent<import('./cameraDeviceRuntime').CameraStatusRequest>).detail;seen.push(detail.device);if(detail.device==='document_camera')detail.resolve(clock.read(detail.device,video,stream,true,null))}
 window.addEventListener(CAMERA_DEVICE_STATUS,receiver)
 try{
  const {result}=await command('device.status',{device:'document_camera'},'你能通过高拍仪看到我吗')
  expect(result.execution.status).toBe('completed');expect(seen).toEqual(['document_camera'])
  const complete=packetTrace.find(x=>x.phase==='complete')!.body!
  const output=(complete.result as {execution_receipt:{output:{devices:Array<{device_type:string;frame_confirmed:boolean;track_live:boolean}>}}}).execution_receipt.output
  expect(output.devices).toEqual([expect.objectContaining({device_type:'document_camera',frame_confirmed:true,track_live:true})])
 }finally{window.removeEventListener(CAMERA_DEVICE_STATUS,receiver)}
})
