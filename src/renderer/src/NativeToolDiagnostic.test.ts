/// <reference types="node" />
// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'
import {QwenNativeConversation} from './qwenNativeConversation'
import {sanitizeDiagnosticValue} from '../../main/diagnostics'
import type {QwenNativeBoundRuntime,QwenNativeToolReceipt} from './qwenNativeProtocol'
const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[{name:'workspace.navigate',tool_name:'runtime_workspace_navigate',description:'Change the visible workspace.',required_arguments:['destination'],arguments_schema:{type:'object',properties:{destination:{type:'string',enum:['learning','mistake_book']}},required:['destination'],additionalProperties:false}}]}
const complete:QwenNativeToolReceipt={version:1,proposal_status:'admitted',execution:{status:'completed',location:'client_device_bridge'},public_result:{status:'ready',private_marker:'PRIVATE_RESULT'}}
const sessions:QwenRealtimeVoiceSession[]=[]
afterEach(()=>{sessions.splice(0).forEach(session=>session.close());vi.restoreAllMocks();vi.unstubAllGlobals()})
async function fixture(log?:(value:unknown)=>unknown){
 const events:Array<{event:string;details:Record<string,unknown>}>=[]
 vi.stubGlobal('window',{setTimeout,clearTimeout,setInterval,clearInterval,desktop:{recordRendererDiagnostic:(event:{event:string;details:Record<string,unknown>})=>{events.push(event);return log?log(event):Promise.resolve({recorded:true})}}})
 const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(),admit:vi.fn(async()=>({voice_input_id:'canonical-voice'})),propose:vi.fn(async()=>complete),readReceipt:vi.fn(),recordDialogue:vi.fn()}
 const ledger=new QwenNativeConversation('supplier-session',runtime,()=>true,'host-connection')
 expect(ledger.startInput('public-item',{sessionId:'public-session',reference:{child_id:'public-child'},carryoverConfirmationId:null,localInputId:'public-onset',capturedAt:1,source:'accepted_onset'})).toBe(true)
 await ledger.completeInput('public-item','PRIVATE_LOCAL_UTTERANCE');expect(ledger.bindResponse('public-response','public-item')).toBe(true)
 const session=new QwenRealtimeVoiceSession({childName:'Synthetic',nativeRuntime:runtime,callbacks:{onStatus:vi.fn(),onError:vi.fn()}})
 sessions.push(session)
 const control=session as unknown as {nativeCatalog:typeof catalog;nativeConversation:QwenNativeConversation;runNativeTool:(response:string,event:Record<string,unknown>)=>Promise<void>;nativeTools:Map<string,{calls:Array<{result?:QwenNativeToolReceipt}>}>}
 control.nativeCatalog=catalog;control.nativeConversation=ledger
 return {runtime,events,run:(raw:string)=>control.runNativeTool('public-response',{name:'runtime_workspace_navigate',call_id:'private-call',arguments:raw}),control}
}
it('records a pending dispatch before admission and the actual completed receipt with only the real registered capability',async()=>{
 const f=await fixture();let resolve!:(r:QwenNativeToolReceipt)=>void
 vi.mocked(f.runtime.propose).mockImplementation(()=>new Promise(done=>{resolve=done}))
 const work=f.run('{"args":{"destination":"mistake_book"}}')
 await vi.waitFor(()=>expect(f.runtime.propose).toHaveBeenCalledTimes(1))
 expect(f.events).toHaveLength(1);expect(f.events[0].details).toMatchObject({input_stage:'dispatch',status:'pending'})
 expect(f.events[0].details).not.toHaveProperty('admission_confirmed')
 resolve(complete);await work
 expect(sanitizeDiagnosticValue(f.events[1].details)).toMatchObject({native_operation:'proposal',native_capability:'workspace.navigate',status:'completed',input_stage:'dispatch'})
 const raw=JSON.stringify(f.events);for(const forbidden of ['PRIVATE_LOCAL_UTTERANCE','PRIVATE_RESULT','canonical-voice','host-connection','private-call','args','lease'])expect(raw).not.toContain(forbidden)
})
it('records a malformed tool shape as not admitted/INPUT_INVALID without logging its raw arguments or proposing an effect',async()=>{
 const f=await fixture();const raw='{"destination":"mistake_book","note":"PRIVATE_RAW_ARGS"}'
 await f.run(raw)
 expect(f.runtime.propose).not.toHaveBeenCalled()
 expect(sanitizeDiagnosticValue(f.events[1].details)).toEqual({runtime:'qwen_omni_realtime',native_operation:'proposal',native_capability:'workspace.navigate',status:'failed',input_stage:'input_not_admitted',chars:raw.length,error_code:'INPUT_INVALID'})
 expect(JSON.stringify(f.events)).not.toContain('PRIVATE_RAW_ARGS')
})
it.each(['sync','async'] as const)('a %s diagnostic failure cannot change the actual Runtime effect or receipt',async kind=>{
 const f=await fixture(()=>{if(kind==='sync')throw Error('diagnostic unavailable');return Promise.reject(Error('diagnostic unavailable'))})
 await f.run('{"args":{"destination":"mistake_book"}}');await Promise.resolve()
 expect(f.runtime.propose).toHaveBeenCalledTimes(1)
 expect(f.control.nativeTools.get('public-response')?.calls[0].result?.execution.status).toBe('completed')
})
