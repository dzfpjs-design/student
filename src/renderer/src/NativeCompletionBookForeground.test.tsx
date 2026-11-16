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
const fixtureFile=process.env.HSC_NATIVE_COMPLETION_TCP_FIXTURE
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

it.skipIf(!fixtureFile)('book open ACK follows actual title layout/scroll, keeps one voice surface, pauses hidden receivers and preserves saved learning drafts',async()=>{
 let scrolled=false
 const originalScroll=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'scrollIntoView')
 Object.defineProperty(HTMLElement.prototype,'scrollIntoView',{configurable:true,value:function(this:HTMLElement){if(this.tagName==='H1')scrolled=true}})
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockImplementation(function(this:HTMLElement){return {x:0,y:scrolled?180:900,top:scrolled?180:900,bottom:scrolled?216:936,left:0,right:600,width:600,height:36,toJSON(){return {}}}})
 await mount()
 const draft=screen.getByRole('textbox',{name:'对学习伙伴说'})
 const {fireEvent}=await import('@testing-library/react');fireEvent.change(draft,{target:{value:'公开未发送的学习草稿'}})
 const hidden=vi.fn(),resolve=vi.fn();window.addEventListener('hsc-study-learning-voice-action',hidden)
 try{
  const opened=await command('workspace.navigate',{destination:'mistake_book'},'打开错题本浏览')
  expect(opened.result.execution.status).toBe('completed');expect(scrolled).toBe(true)
  const title=screen.getByText('智能错题本'),box=title.getBoundingClientRect();expect(box.top).toBeGreaterThanOrEqual(0);expect(box.bottom).toBeLessThan(window.innerHeight)
  expect(h.props!.visibleLearningSource).toBe(false)
  window.dispatchEvent(new CustomEvent('hsc-study-learning-voice-action',{cancelable:true,detail:{action:'synthetic-no-effect',resolve}}))
  expect(hidden).not.toHaveBeenCalled();expect(resolve).toHaveBeenCalledWith(false)
  const returned=await command('workspace.navigate',{destination:'learning'},'回到学习')
  expect(returned.result.execution.status).toBe('completed');expect(h.props!.visibleLearningSource).toBe(true)
  expect(screen.getByRole('textbox',{name:'对学习伙伴说'})).toHaveValue('公开未发送的学习草稿')
  window.dispatchEvent(new CustomEvent('hsc-study-learning-voice-action',{cancelable:true,detail:{action:'synthetic-no-effect',resolve:()=>undefined}}));expect(hidden).toHaveBeenCalledTimes(1)
 }finally{window.removeEventListener('hsc-study-learning-voice-action',hidden);if(originalScroll)Object.defineProperty(HTMLElement.prototype,'scrollIntoView',originalScroll);else delete (HTMLElement.prototype as unknown as Record<string,unknown>).scrollIntoView}
})

it.skipIf(!fixtureFile)('book layout negative never reports opened when data mounted but the title has no visible rectangle',async()=>{
 vi.spyOn(HTMLElement.prototype,'getBoundingClientRect').mockReturnValue({x:0,y:900,top:900,bottom:900,left:0,right:0,width:0,height:0,toJSON(){return {}}})
 await mount()
 const {result}=await command('workspace.navigate',{destination:'mistake_book'},'打开错题本')
 expect(result.execution.status).toBe('failed');expect(result.public_result.workspace_opened).not.toBe(true)
 expect(screen.getByText('智能错题本')).toBeInTheDocument();expect(h.props!.visibleLearningSource).toBe(false)
})
