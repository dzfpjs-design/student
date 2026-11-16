/// <reference types="node" />
// @vitest-environment node
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {QwenNativeConversation,qwenNativePublicToolOutput} from './qwenNativeConversation'
import {QwenRealtimeVoiceSession} from './qwenRealtimeVoice'
import type {QwenNativeBoundRuntime,QwenNativeToolReceipt} from './qwenNativeProtocol'
const catalog={version:1 as const,protocol:'qwen_runtime_capability_v1' as const,capabilities:[
 {name:'workspace.navigate',tool_name:'runtime_workspace_navigate',description:'Navigate workspace.',required_arguments:['destination'],arguments_schema:{type:'object',properties:{destination:{type:'string',enum:['learning','mistake_book']}},required:['destination'],additionalProperties:false}},
 {name:'system.inspect_learning',tool_name:'runtime_system_inspect_learning',description:'Read real progress.',required_arguments:[],arguments_schema:{type:'object',properties:{},additionalProperties:false}}
]}
const valid='{"args":{"destination":"mistake_book"}}',broken='{"args":{"destination":"mistake_book"},"objective":{"phase":"'
const completed:QwenNativeToolReceipt={version:1,proposal_status:'admitted',execution:{status:'completed',location:'client_device_bridge'},public_result:{status:'opened'}}
const sessions:QwenRealtimeVoiceSession[]=[]
const budgetKey='hsc-qwen-tool-format-budget-v1'
class PublicStorage implements Storage {
 private values=new Map<string,string>();get length(){return this.values.size}
 clear(){this.values.clear()}getItem(key:string){return this.values.get(key)??null}
 key(n:number){return [...this.values.keys()][n]??null}removeItem(key:string){this.values.delete(key)}
 setItem(key:string,value:string){this.values.set(key,value)}
}
beforeEach(()=>vi.stubGlobal('localStorage',new PublicStorage()))
afterEach(()=>{sessions.splice(0).forEach(s=>s.close());vi.restoreAllMocks();vi.unstubAllGlobals()})
async function fixture(){
 const runtime:QwenNativeBoundRuntime={catalog,loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(),admit:vi.fn(async()=>({voice_input_id:'canonical-original'})),propose:vi.fn(async()=>completed),readReceipt:vi.fn(async()=>completed),recordDialogue:vi.fn()}
 let current=true
 const ledger=new QwenNativeConversation('supplier-original',runtime,()=>current,'host-original')
 const capture={sessionId:'session-original',reference:{child_id:'child-original'},carryoverConfirmationId:null,localInputId:'onset-original',capturedAt:Date.now(),source:'accepted_onset' as const}
 ledger.startInput('item-original',capture);await ledger.completeInput('item-original','打开错题本并读取我的学习进度。');ledger.bindResponse('response-1','item-original')
 const response=(name:string)=>{expect(ledger.bindResponse(name,'item-original')).toBe(true)}
 return {runtime,ledger,response,capture,retire:()=>{current=false}}
}
const recovery=(r:QwenNativeToolReceipt)=>JSON.parse(qwenNativePublicToolOutput(r)).public_result.argument_recovery
it('an incomplete JSON is explicitly unadmitted and offers one correction; a valid new call and normal multi-tool chain preserve the original identity',async()=>{
 const f=await fixture();const bad=await f.ledger.propose('response-1','bad-call','runtime_workspace_navigate',broken)
 expect(f.runtime.propose).not.toHaveBeenCalled();expect(bad.not_admitted).toBe(true)
 expect(recovery(bad)).toMatchObject({state:'retry_once',retry_allowed:true})
 expect(recovery(bad).instruction).toContain('args');expect(recovery(bad).instruction).toContain('objective')
 await f.ledger.finishDialogue('response-1','completed');f.response('response-2')
 const good=await f.ledger.propose('response-2','corrected-call','runtime_workspace_navigate',valid)
 expect(good.execution.status).toBe('completed');expect(recovery(good)).toBeUndefined()
 expect(f.runtime.propose).toHaveBeenCalledWith(expect.objectContaining({voice_input_id:'canonical-original',input_item_id:'item-original',supplier_session_id:'supplier-original',host_connection_id:'host-original',call_id:'corrected-call',args:{destination:'mistake_book'}}))
 f.response('response-3');await f.ledger.propose('response-3','next-legitimate-call','runtime_system_inspect_learning','{"args":{}}')
 expect(f.runtime.propose).toHaveBeenCalledTimes(2)
})
it('the recovery allowance belongs to the original input across responses, and the second malformed correction is exhausted without any effect',async()=>{
 const f=await fixture();const first=await f.ledger.propose('response-1','bad-1','runtime_workspace_navigate',broken)
 expect(recovery(first)?.state).toBe('retry_once')
 await f.ledger.finishDialogue('response-1','completed');f.response('response-2')
 const second=await f.ledger.propose('response-2','bad-2','runtime_workspace_navigate',broken)
 expect(second.not_admitted).toBe(true);expect(recovery(second)).toMatchObject({state:'exhausted',retry_allowed:false})
 expect(f.runtime.propose).not.toHaveBeenCalled()
 f.ledger.startInput('fresh-item',{...f.capture,localInputId:'fresh-onset',capturedAt:Date.now()+1});vi.mocked(f.runtime.admit).mockResolvedValueOnce({voice_input_id:'fresh-canonical-voice'});await f.ledger.completeInput('fresh-item','请查看我的学习进度。');f.ledger.bindResponse('fresh-response','fresh-item')
 expect(recovery(await f.ledger.propose('fresh-response','fresh-bad','runtime_system_inspect_learning',broken))?.state).toBe('retry_once')
})
it('a malformed reuse of an already executed call cannot claim not-admitted or authorize a duplicate correction',async()=>{
 const f=await fixture();await f.ledger.propose('response-1','same-call','runtime_workspace_navigate',valid)
 const changed=await f.ledger.propose('response-1','same-call','runtime_workspace_navigate',broken)
 expect(changed.public_result.status).toBe('call_identity_conflict');expect(changed.not_admitted).not.toBe(true);expect(recovery(changed)).toBeUndefined();expect(f.runtime.propose).toHaveBeenCalledTimes(1)
})
it('an invalid call ID cannot be reused with corrected parameters, and a retired source gets no recovery permission',async()=>{
 const f=await fixture();await f.ledger.propose('response-1','invalid-id','runtime_workspace_navigate',broken)
 const changed=await f.ledger.propose('response-1','invalid-id','runtime_workspace_navigate',valid)
 expect(changed.public_result.status).toBe('call_identity_conflict');expect(f.runtime.propose).not.toHaveBeenCalled()
 f.retire();const old=await f.ledger.propose('response-1','late-call','runtime_workspace_navigate',broken)
 expect(old.not_admitted).not.toBe(true);expect(recovery(old)).toBeUndefined()
})
it('unknown or admitted effects are never treated as format rejections and are only queried with their original identity',async()=>{
 const f=await fixture();vi.mocked(f.runtime.propose).mockResolvedValue({version:1,proposal_status:'admitted',execution:{status:'unknown',location:'server'},public_result:{status:'tool_arguments_invalid'}})
 const unknown=await f.ledger.propose('response-1','original-call','runtime_workspace_navigate',valid)
 expect(recovery(unknown)).toBeUndefined()
 const bad=await f.ledger.propose('response-1','new-malformed','runtime_workspace_navigate',broken)
 expect(bad.not_admitted).not.toBe(true);expect(recovery(bad)).toBeUndefined()
 await f.ledger.propose('response-1','original-call','runtime_workspace_navigate',valid)
 expect(f.runtime.propose).toHaveBeenCalledTimes(1);expect(f.runtime.readReceipt).toHaveBeenCalledTimes(1)
})
it('a queued malformed call waits for the real prior outcome and cannot turn an unknown execution into a retry offer',async()=>{
 const f=await fixture();let resolve!:(r:QwenNativeToolReceipt)=>void;vi.mocked(f.runtime.propose).mockImplementation(()=>new Promise(done=>{resolve=done}))
 const first=f.ledger.propose('response-1','in-flight','runtime_workspace_navigate',valid)
 await vi.waitFor(()=>expect(f.runtime.propose).toHaveBeenCalledTimes(1))
 const second=f.ledger.propose('response-1','malformed-while-pending','runtime_system_inspect_learning',broken)
 resolve({version:1,proposal_status:'admitted',execution:{status:'unknown',location:'server'},public_result:{status:'unknown'}});await first
 expect(recovery(await second)).toBeUndefined();expect(f.runtime.propose).toHaveBeenCalledTimes(1)
})
it('an explicit server non-admission can offer the same single correction but an admitted failure cannot',async()=>{
 const f=await fixture();vi.mocked(f.runtime.propose).mockResolvedValueOnce({version:1,proposal_status:'rejected',not_admitted:true,execution:{status:'failed',location:'server'},public_result:{status:'tool_arguments_invalid'}}).mockResolvedValueOnce({version:1,proposal_status:'admitted',execution:{status:'failed',location:'server'},public_result:{status:'tool_arguments_invalid'}})
 expect(recovery(await f.ledger.propose('response-1','server-denied','runtime_workspace_navigate',valid))?.state).toBe('retry_once')
 expect(recovery(await f.ledger.propose('response-1','admitted-failed','runtime_system_inspect_learning','{"args":{}}'))).toBeUndefined()
})
it('after the second format rejection, the real tool-output flush sends the finite failure and cannot schedule a third automatic response',async()=>{
 const f=await fixture();const first=await f.ledger.propose('response-1','bad-1','runtime_workspace_navigate',broken);f.response('response-2')
 const second=await f.ledger.propose('response-2','bad-2','runtime_workspace_navigate',broken)
 vi.stubGlobal('window',{desktop:{},setTimeout,clearTimeout,setInterval,clearInterval})
 const session=new QwenRealtimeVoiceSession({childName:'Synthetic',nativeRuntime:f.runtime,callbacks:{onStatus:vi.fn()}});sessions.push(session)
 // IO-only scheduling boundary: no microphone, permission or effect is asserted
 // here. The actual ledger owns the same admitted input and rejection receipts.
 const c=session as unknown as {nativeConversation:QwenNativeConversation;nativeResponseAllowed:()=>boolean;send:(e:unknown)=>Promise<boolean>;manualAudio:{requestContinuation:(r:string)=>boolean;dispose:()=>void};nativeTools:Map<string,unknown>;flushNativeTools:(r:string)=>void}
 c.nativeConversation=f.ledger;c.nativeResponseAllowed=()=>true;const send=vi.fn<(event:unknown)=>Promise<boolean>>(async()=>true),next=vi.fn(()=>true);c.send=send;c.manualAudio={requestContinuation:next,dispose:()=>undefined}
 c.nativeTools.set('response-1',{done:true,calls:[{id:'bad-1',result:first}]});c.flushNativeTools('response-1')
 await vi.waitFor(()=>expect(next).toHaveBeenCalledTimes(1))
 c.nativeTools.set('response-2',{done:true,calls:[{id:'bad-2',result:second}]});c.flushNativeTools('response-2')
 await vi.waitFor(()=>expect(send).toHaveBeenCalledTimes(2));await Promise.resolve()
 expect(next).toHaveBeenCalledTimes(1)
 const output=JSON.parse((send.mock.calls[1][0] as {item:{output:string}}).item.output)
 expect(output.public_result.argument_recovery.state).toBe('exhausted')
})

it('an exhausted input cannot execute an already queued or later valid third correction, while prior receipts remain readable',async()=>{
 const f=await fixture();await f.ledger.propose('response-1','bad-1','runtime_workspace_navigate',broken)
 f.response('response-2');const second=f.ledger.propose('response-2','bad-2','runtime_workspace_navigate',broken)
 const queued=f.ledger.propose('response-2','valid-third','runtime_workspace_navigate',valid)
 await second;const refused=await queued
 expect(refused.execution.status).toBe('failed');expect(refused.not_admitted).toBe(true)
 expect(recovery(refused)).toMatchObject({state:'exhausted',retry_allowed:false});expect(f.runtime.propose).not.toHaveBeenCalled()
})
it.each(['admitted','unknown','has_action'] as const)('a contradictory %s non-admission receipt never offers automatic recovery',async kind=>{
 const f=await fixture();vi.mocked(f.runtime.propose).mockResolvedValue({version:1,proposal_status:kind==='admitted'?'admitted':'rejected',not_admitted:true,execution:{status:kind==='unknown'?'unknown':'failed',location:'server'},...(kind==='has_action'?{action_id:'already-accepted-action'}:{}),public_result:{status:'tool_arguments_invalid'}})
 expect(recovery(await f.ledger.propose('response-1','ambiguous-receipt','runtime_workspace_navigate',valid))).toBeUndefined()
})

it('the canonical input budget survives a real ledger/transport replacement and does not renew a third execution',async()=>{
 const f=await fixture();await f.ledger.propose('response-1','bad-1','runtime_workspace_navigate',broken);f.response('response-2');await f.ledger.propose('response-2','bad-2','runtime_workspace_navigate',broken)
 const stored=localStorage.getItem(budgetKey)!;expect(JSON.parse(stored).entries).toEqual([{voice_input_id:'canonical-original',count:2,expires_at:expect.any(Number)}])
 for(const raw of ['args','objective','lease','打开错题本'])expect(stored).not.toContain(raw)
 f.ledger.close()
 const next=new QwenNativeConversation('replacement-supplier',f.runtime,()=>true,'replacement-host')
 expect(next.restoreInput('restored-item',{voice_input_id:'canonical-original',client_action_id:'original-action',created_at:new Date(f.capture.capturedAt).toISOString(),text:'打开错题本并读取我的学习进度。'},()=>true)).toBe(true)
 next.bindResponse('replacement-response','restored-item')
 const result=await next.propose('replacement-response','valid-after-rotation','runtime_workspace_navigate',valid)
 expect(recovery(result)).toMatchObject({state:'exhausted',retry_allowed:false});expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('storage failure disables only automatic format correction; a valid new utterance and normal tool still execute',async()=>{
 const f=await fixture();vi.spyOn(localStorage,'setItem').mockImplementation(()=>{throw Error('public storage unavailable')})
 expect(recovery(await f.ledger.propose('response-1','bad-storage','runtime_workspace_navigate',broken))).toMatchObject({state:'exhausted',retry_allowed:false})
 f.ledger.startInput('new-live-item',{...f.capture,localInputId:'new-live-onset',capturedAt:Date.now()+1});await f.ledger.completeInput('new-live-item','请查看学习进度。');f.ledger.bindResponse('new-live-response','new-live-item')
 expect((await f.ledger.propose('new-live-response','valid-new-input','runtime_system_inspect_learning','{"args":{}}')).execution.status).toBe('completed')
 expect(f.runtime.propose).toHaveBeenCalledTimes(1)
})
it('capacity and fixed expiry bound metadata; full storage closes recovery and expired entries are pruned without global tool disablement',async()=>{
 const f=await fixture();const entries=Array.from({length:256},(_,i)=>({voice_input_id:'public-old-'+i,count:1,expires_at:Date.now()+30*60_000}))
 localStorage.setItem(budgetKey,JSON.stringify({version:1,entries}))
 expect(recovery(await f.ledger.propose('response-1','capacity-bad','runtime_workspace_navigate',broken))?.state).toBe('exhausted')
 expect(JSON.parse(localStorage.getItem(budgetKey)!).entries).toHaveLength(256)
 vi.mocked(f.runtime.admit).mockResolvedValueOnce({voice_input_id:'capacity-distinct-new-voice'})
 f.ledger.startInput('capacity-new-item',{...f.capture,localInputId:'capacity-new-onset',capturedAt:Date.now()+1});await f.ledger.completeInput('capacity-new-item','读取我的学习进度。');f.ledger.bindResponse('valid-response','capacity-new-item')
 expect((await f.ledger.propose('valid-response','capacity-valid','runtime_system_inspect_learning','{"args":{}}')).execution.status).toBe('completed')
 for(const row of entries)row.expires_at=Date.now()-1
 localStorage.setItem(budgetKey,JSON.stringify({version:1,entries}))
 f.ledger.startInput('fresh-after-expiry',{...f.capture,localInputId:'after-expiry-onset',capturedAt:Date.now()+1});vi.mocked(f.runtime.admit).mockResolvedValueOnce({voice_input_id:'fresh-after-expiry-voice'});await f.ledger.completeInput('fresh-after-expiry','请打开错题本。');f.ledger.bindResponse('after-expiry-response','fresh-after-expiry')
 expect(recovery(await f.ledger.propose('after-expiry-response','expired-cache-bad','runtime_workspace_navigate',broken))?.state).toBe('retry_once')
 expect(JSON.parse(localStorage.getItem(budgetKey)!).entries).toHaveLength(1)
})
it('an old persisted source cannot obtain a fresh format retry after its fixed recovery age window expires',async()=>{
 const f=await fixture();const old=new QwenNativeConversation('old-supplier',f.runtime,()=>true,'old-host')
 expect(old.restoreInput('old-item',{voice_input_id:'old-canonical-voice',client_action_id:'old-action',created_at:new Date(Date.now()-11*60_000).toISOString(),text:'公开已保存的请求'},()=>true)).toBe(true)
 old.bindResponse('old-response','old-item')
 expect(recovery(await old.propose('old-response','old-invalid','runtime_workspace_navigate',broken))?.state).toBe('exhausted');expect(f.runtime.propose).not.toHaveBeenCalled()
})
it('a local malformed call without a saved canonical admission is source-unconfirmed, with no recovery grant or saved-request claim',async()=>{
 const f=await fixture();vi.mocked(f.runtime.admit).mockRejectedValueOnce(Error('public admission rejected'))
 f.ledger.startInput('unadmitted-item',{...f.capture,localInputId:'unadmitted-onset'});await expect(f.ledger.completeInput('unadmitted-item','公开尚未保存的请求')).rejects.toThrow()
 f.ledger.bindResponse('unadmitted-response','unadmitted-item')
 const result=await f.ledger.propose('unadmitted-response','unadmitted-bad','runtime_workspace_navigate',broken)
 expect(result.public_result.status).toBe('source_unconfirmed');expect(recovery(result)).toBeUndefined();expect(localStorage.getItem(budgetKey)).toBeNull()
 expect(qwenNativePublicToolOutput(result)).not.toContain('原请求已保留');expect(f.runtime.propose).not.toHaveBeenCalled()
})

it('storage failure exhausts the original input before a queued valid correction, while a fresh canonical input remains usable',async()=>{
 const f=await fixture();vi.spyOn(localStorage,'setItem').mockImplementation(()=>{throw Error('public store write failure')})
 const bad=f.ledger.propose('response-1','bad-storage-queued','runtime_workspace_navigate',broken)
 const queued=f.ledger.propose('response-1','valid-queued-after-storage-failure','runtime_workspace_navigate',valid)
 expect(recovery(await bad)?.state).toBe('exhausted')
 expect(recovery(await queued)?.state).toBe('exhausted');expect(f.runtime.propose).not.toHaveBeenCalled()
 vi.mocked(f.runtime.admit).mockResolvedValueOnce({voice_input_id:'distinct-new-canonical-voice'})
 f.ledger.startInput('new-fresh-item',{...f.capture,localInputId:'distinct-new-onset',capturedAt:Date.now()+1});await f.ledger.completeInput('new-fresh-item','请读取我的学习进度。');f.ledger.bindResponse('new-fresh-response','new-fresh-item')
 expect((await f.ledger.propose('new-fresh-response','new-fresh-valid','runtime_system_inspect_learning','{"args":{}}')).execution.status).toBe('completed');expect(f.runtime.propose).toHaveBeenCalledTimes(1)
})
