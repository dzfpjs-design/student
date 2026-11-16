/// <reference types="node" />
import {expect,it,vi,afterEach} from 'vitest'
import {readFileSync,openSync} from 'node:fs'
import {spawn,type ChildProcess} from 'node:child_process'
import {waitFor,act} from '@testing-library/react'
import {NativeRealtimeCapabilityGateway,signNativeRealtimeCapability} from '../../main/native-realtime-capabilities'
import {createNativeRuntimeBridge} from './nativeRuntimeBridge'
import {createCaptureReceiptBridge,emitCaptureReceipt} from './deviceCommandReceipt'
import {localApi,resetRuntimeCache} from './api'
import {resetNativeClientExecutionsForTests,nativeClientExecution} from './nativeClientExecution'
import type {NativeRealtimeForward} from '../../shared/nativeRealtimeCapability'
import type {QwenNativeCapabilityProposal,QwenNativeClientDispatch} from './qwenNativeProtocol'
const file=process.env.HSC_NATIVE_COMPLETION_TCP_FIXTURE
const config=file?JSON.parse(readFileSync(file,'utf8')) as {url:string;token:string;session:string;child:string;store:string;fixture_script:string;python:string;pid:number}:null
let restarted:ChildProcess|undefined
const originalFetch=globalThis.fetch
const currentOwner={status:'ready',localApiUrl:config?.url??'',sessionToken:config?.token??'',runtimeEpoch:1,child:{},revision:0}
afterEach(()=>{restarted?.kill('SIGTERM');restarted=undefined;resetNativeClientExecutionsForTests();localStorage.clear();resetRuntimeCache();vi.unstubAllGlobals();vi.restoreAllMocks()})
it.skipIf(!config)('actual capture journal survives unmount, registry loss and backend restart; new current signed host saves only the original OCR receipt, never repeats the effect',async()=>{
 const f=config!,event={sender:{id:81}},gateway=new NativeRealtimeCapabilityGateway({allowed:e=>e.sender.id===81,current:()=>currentOwner,policy:async()=>({capture:'configured',runtimeEpoch:currentOwner.runtimeEpoch}),sign:p=>signNativeRealtimeCapability('cd'.repeat(32),p),fetch:originalFetch})
 let host=gateway.register(event),current=true,blockCompletion=true,physicalEffects=0
 const trace:NativeRealtimeForward[]=[]
 const forward=async(request:NativeRealtimeForward)=>{trace.push(request);if(blockCompletion&&request.operation==='client'&&request.body?.phase==='complete')throw Error('SYNTHETIC_RECEIPT_TRANSPORT_LOST');return gateway.forward(event,request)}
 vi.stubGlobal('desktop',{getRuntimeConfig:async()=>({status:'ready',localApiUrl:f.url,sessionToken:f.token,runtimeEpoch:currentOwner.runtimeEpoch})});resetRuntimeCache();localStorage.clear()
 const errors=vi.fn(),capture=createCaptureReceiptBridge(f.session,errors),stop=capture.start()
 let dispatch!:QwenNativeClientDispatch
 const first=createNativeRuntimeBridge({sessionId:f.session,childId:f.child,current:()=>current,forward,
  executePrepared:async(d,port)=>{dispatch=d;await port.claim();capture.request(d.turn,false,request=>{
   request.accept();physicalEffects++
   void originalFetch(f.url+'/public-synthetic/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:f.session,client_action_id:'public-physical-original'})}).then(async response=>{
    expect(response.ok).toBe(true);const artifact=await response.json() as {artifact_id:string;status:string}
    emitCaptureReceipt({sessionId:f.session,commandId:d.turn.agent_turn_id,status:'recognized',artifactId:artifact.artifact_id,ocrStatus:'completed'})
   })
  })}})
 await first.loadCatalog(host);await first.loadContext(host)
 const admitted=await first.admit('帮我拍下这道题',{source:'accepted_onset',sessionId:f.session,reference:{child_id:f.child},localInputId:'public-durable-onset',capturedAt:Date.now(),carryoverConfirmationId:null},host)
 const identity:QwenNativeCapabilityProposal={version:1,host_connection_id:host,voice_input_id:admitted.voice_input_id,supplier_session_id:'public-supplier',input_item_id:'public-item',response_id:'public-response',call_id:'public-call',capability_name:'learning.capture_question',args:{}}
 await first.propose(identity)
 const journal='hsc-device-capture-v1:'+f.session+':'+dispatch.turn.agent_turn_id
 await waitFor(()=>{expect(JSON.parse(localStorage.getItem(journal)!).status).toBe('recognized');expect(errors).toHaveBeenCalled()})
 const descriptor=localStorage.getItem('hsc-native-completion-v1:'+dispatch.turn.agent_turn_id)!
 expect(descriptor).toBeTruthy();expect(descriptor).not.toContain('帮我拍下');expect(descriptor).not.toContain('completion_lease');expect(physicalEffects).toBe(1)
 stop();current=false;gateway.close(event,host);resetNativeClientExecutionsForTests()
 process.kill(f.pid,'SIGTERM');await new Promise(resolve=>setTimeout(resolve,500))
 restarted=spawn(f.python,[f.fixture_script,'40389'],{cwd:process.cwd(),env:{...process.env,HSC_PUBLIC_NATIVE_FIXTURE_DIR:f.store,TMPDIR:'/private/tmp'},stdio:['ignore',openSync('/tmp/hsc-r11-restarted.log','w'),openSync('/tmp/hsc-r11-restarted.err','w')]})
 await vi.waitFor(async()=>{const response=await originalFetch(f.url+'/v1/health',{headers:{'X-HSC-Session':f.token}});expect(response.ok).toBe(true)},{timeout:8000,interval:50})
 currentOwner.child={};currentOwner.runtimeEpoch++;host=gateway.register(event);blockCompletion=false
 const resumedCapture=createCaptureReceiptBridge(f.session,errors),resumeStop=resumedCapture.start()
 const resumed=createNativeRuntimeBridge({sessionId:f.session,childId:f.child,current:()=>true,forward})
 const before=trace.length;const context=await resumed.loadContext(host)
 expect(context.pending_completions?.some(d=>d.agent_turn_id===dispatch.turn.agent_turn_id)).toBe(true)
 await waitFor(()=>expect(localStorage.getItem(journal)).toBeNull(),{timeout:6000})
 expect(localStorage.getItem('hsc-native-completion-v1:'+dispatch.turn.agent_turn_id)).toBeNull()
 expect(trace.slice(before).filter(x=>x.operation==='client').map(x=>x.body?.phase)).toEqual(['complete'])
 await expect(nativeClientExecution(dispatch.turn.agent_turn_id)!.execute()).rejects.toThrow('NO_EXECUTION')
 await expect(nativeClientExecution(dispatch.turn.agent_turn_id)!.claim()).rejects.toThrow('NO_EXECUTION')
 const saved=await resumed.readReceipt({...identity,host_connection_id:host});expect(saved.execution.status).toBe('completed')
 const count=await(await originalFetch(f.url+'/public-synthetic/effect-count')).json() as {capture_count:number};expect(count.capture_count).toBe(1);expect(physicalEffects).toBe(1)
 // Another child's renderer cannot install this old completion authority.
 const foreign=createNativeRuntimeBridge({sessionId:f.session,childId:'different-child',current:()=>true,forward})
 await expect(foreign.loadContext(host)).rejects.toThrow('CONTEXT_OWNER_CHANGED')
 // A real newly persisted proposal without a claim is never a completion authority.
 const unclaimedVoice=await resumed.admit('帮我查看这道新题',{source:'accepted_onset',sessionId:f.session,reference:{child_id:f.child},localInputId:'public-no-claim-onset',capturedAt:Date.now(),carryoverConfirmationId:null},host)
 const unclaimed={...identity,host_connection_id:host,voice_input_id:unclaimedVoice.voice_input_id,input_item_id:'public-no-claim-item',response_id:'public-no-claim-response',call_id:'public-no-claim-call'}
 const unclaimedResult=await gateway.forward(event,{session_id:f.session,host_connection_id:host,operation:'proposal',body:unclaimed}) as {client_dispatch:QwenNativeClientDispatch}
 expect(unclaimedResult.client_dispatch).toBeTruthy()
 await expect(gateway.forward(event,{session_id:f.session,host_connection_id:host,operation:'client',body:{version:1,voice_input_id:unclaimed.voice_input_id,supplier_session_id:unclaimed.supplier_session_id,input_item_id:unclaimed.input_item_id,response_id:unclaimed.response_id,call_id:unclaimed.call_id,dispatch_id:unclaimedResult.client_dispatch.dispatch_id,phase:'complete',final_response:'没有实际回执',tool_status:'completed'}})).rejects.toThrow('UNCONFIRMED')
 const noClaimContext=await resumed.loadContext(host);expect(noClaimContext.pending_completions?.some(d=>d.agent_turn_id===unclaimedResult.client_dispatch.turn.agent_turn_id)).toBe(false)
 // A separate accepted capture with no recognized result is cancelled after the
 // UI disappears; neither the old artifact nor a new OCR is invented for it.
 resumeStop();resetNativeClientExecutionsForTests()
 const unknownCapture=createCaptureReceiptBridge(f.session,errors),unknownStop=unknownCapture.start()
 let unknownDispatch!:QwenNativeClientDispatch
 const unknownBridge=createNativeRuntimeBridge({sessionId:f.session,childId:f.child,current:()=>true,forward,executePrepared:async(d,port)=>{unknownDispatch=d;await port.claim();unknownCapture.request(d.turn,false,request=>request.accept())}})
 const unknownVoice=await unknownBridge.admit('这是另一道还没拍好的题',{source:'accepted_onset',sessionId:f.session,reference:{child_id:f.child},localInputId:'public-unknown-capture',capturedAt:Date.now(),carryoverConfirmationId:null},host)
 const unknownIdentity={...identity,host_connection_id:host,voice_input_id:unknownVoice.voice_input_id,input_item_id:'public-unknown-item',response_id:'public-unknown-response',call_id:'public-unknown-call'}
 await unknownBridge.propose(unknownIdentity);unknownStop();resetNativeClientExecutionsForTests()
 const finalCapture=createCaptureReceiptBridge(f.session,errors),finalStop=finalCapture.start();await resumed.loadContext(host)
 await waitFor(()=>expect(localStorage.getItem('hsc-device-capture-v1:'+f.session+':'+unknownDispatch.turn.agent_turn_id)).toBeNull(),{timeout:6000})
 const cancelled=await resumed.readReceipt(unknownIdentity);expect(cancelled.execution.status).toBe('failed')
 expect((await(await originalFetch(f.url+'/public-synthetic/effect-count')).json() as {capture_count:number}).capture_count).toBe(1);expect(physicalEffects).toBe(1)
 finalStop();gateway.close(event,host)
})
