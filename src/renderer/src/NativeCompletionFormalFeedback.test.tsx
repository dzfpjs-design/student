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

it.skipIf(!fixtureFile)('real source-backed Tutor and explicit actual grader show and play protected feedback through the original speech receipt before native host ACK',async()=>{
 vi.mocked(inputs.speakLocally).mockRestore()
 const spoken:string[]=[]
 class PublicUtterance{voice:SpeechSynthesisVoice|null=null;lang='';rate=1;onstart:(()=>void)|null=null;onend:(()=>void)|null=null;onerror:(()=>void)|null=null;constructor(public text:string){}}
 const voice={name:'Public Local Chinese',lang:'zh-CN',localService:true,default:true,voiceURI:'public-local'} as SpeechSynthesisVoice
 vi.stubGlobal('SpeechSynthesisUtterance',PublicUtterance)
 vi.stubGlobal('speechSynthesis',{getVoices:()=>[voice],cancel:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),speak:(u:PublicUtterance)=>{spoken.push(u.text);queueMicrotask(()=>{u.onstart?.();queueMicrotask(()=>u.onend?.())})}})
 await mount()
 await command('learning.prepare_request',{subject:'数学'},'我想学习数学')
 const question='请给我讲解正弦和余弦有什么区别。'
 const explained=await command('learning.explain',{question:'模型概括短问',subject:'数学',support_level:6},question)
 expect(explained.result.execution.status).toBe('completed')
 const runtime=await localApi.agentRuntimeStatus(f.session)
 const reference=runtime.pending_learning_capture?.reference
 expect(reference).toBeTruthy()
 const native=h.props!.nativeRuntime!,n=runId+'-grade'
 const admitted=await native.admit('我的答案是正弦看对边，余弦看邻边，都要除以斜边。',{source:'accepted_onset',sessionId:f.session,localInputId:'public-onset-'+n,capturedAt:Date.now(),reference:{child_id:f.child,question_reference:reference},carryoverConfirmationId:null},host)
 const p:QwenNativeCapabilityProposal={version:1,host_connection_id:host,voice_input_id:admitted.voice_input_id,supplier_session_id:'public-supplier-'+n,input_item_id:'public-item-'+n,response_id:'public-response-'+n,call_id:'public-call-'+n,capability_name:'learning.evaluate_response',args:{subject:'数学'}}
 let done=false,result:Awaited<ReturnType<typeof native.propose>>|undefined
 await act(async()=>{void native.propose(p).then(value=>{result=value;done=true})})
 await waitFor(()=>expect(done).toBe(true),{timeout:18000})
 expect(result!.execution.status).toBe('completed')
 expect(spoken.some(text=>text.includes('回答和评价已保存')&&text.includes('原来的对边会变成哪一条边'))).toBe(true)
 const complete=packetTrace.filter(x=>x.phase==='complete').at(-1)!.body!
 expect((complete.result as {local_delivery:{status:string;kind:string}}).local_delivery).toEqual({status:'ended',kind:'protected_runtime_speech'})
 expect(result!.public_result).toMatchObject({local_delivery:{status:'ended',kind:'protected_runtime_speech'}})
 expect(screen.queryAllByText(/原题保留/)).toHaveLength(0)
})
