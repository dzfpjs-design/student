/// <reference types="node" />
// @vitest-environment node
import {SUPPORTED_AGENT_ACTION_TYPES} from './agentActionContract'
import {expect,it} from 'vitest'
import {readFileSync} from 'node:fs'
import {fitQwenNativeSession,qwenRuntimeTools,type QwenRuntimeCapabilityCatalog} from './qwenNativeProtocol'
import {qwenNativePublicToolOutput} from './qwenNativeConversation'
const catalog=JSON.parse(readFileSync(new URL('./test/fullNativePublicCatalog.json',import.meta.url),'utf8')) as QwenRuntimeCapabilityCatalog
it('keeps the actual complete 104-tool catalog while trimming only oldest ordinary unverified dialogue to real UTF8 packet budget',()=>{
 const context={version:1 as const,revision:1,recent_dialogue:Array.from({length:24},(_,n)=>({role:'user' as const,text:'中文公开样本'.repeat(350)+n,status:'unverified' as const})),runtime_facts:{version:1,items:[{status:'saved'}]},pending_calls:[{call_id:'unknown-call',status:'unknown' as const}],pending_inputs:[{voice_input_id:'host-only-voice',client_action_id:'host-only-action',created_at:new Date().toISOString(),text:'HOST_ONLY_PENDING_INPUT'}]}
 const actual=fitQwenNativeSession({type:'session.update',session:{turn_detection:null}},catalog,context)
 expect(new TextEncoder().encode(JSON.stringify(actual)).byteLength).toBeLessThanOrEqual(128000)
 const session=actual.session as {tools:unknown[];instructions:string}
 expect(session.tools).toHaveLength(104);expect(session.tools).toEqual(qwenRuntimeTools(catalog))
 expect(session.instructions).toContain('has_pending_operation');expect(session.instructions).toContain('saved');expect(session.instructions).not.toContain('HOST_ONLY_PENDING_INPUT')
 expect(session.instructions.length).toBeLessThan(JSON.stringify(context.recent_dialogue).length)
 expect(()=>fitQwenNativeSession({session:{}},catalog,{...context,recent_dialogue:[]},1000)).toThrow('PAYLOAD_TOO_LARGE')
})
it('never sends host dispatch/turn/private completion leases in a provider function result',()=>{
 const output=qwenNativePublicToolOutput({version:1,proposal_status:'admitted',execution:{status:'completed',location:'server'},public_result:{status:'opened'},client_dispatch:{version:1,dispatch_id:'PRIVATE_DISPATCH',action_id:'PRIVATE_ACTION',turn:{} as never,status:'pending'},host_turn:{agent_turn_id:'PRIVATE_TURN'} as never,host_execution:{turn:{} as never,execution:{capability:'private',response_text:'PRIVATE_BODY',result:{secret:'PRIVATE_RESULT'},evidence:[]}}})
 expect(output).toContain('opened');expect(output).not.toContain('PRIVATE_');expect(output).not.toContain('host_')
})

it('every one of the 103 registered concrete capabilities reaches a supported action tag; prepare_request stays its separate durable server tool',()=>{
 const rows=JSON.parse(readFileSync(new URL('./test/fullNativeCapabilityActions.json',import.meta.url),'utf8')) as Array<{name:string;action_type:string}>
 expect(rows).toHaveLength(103);for(const row of rows)expect(SUPPORTED_AGENT_ACTION_TYPES).toContain(row.action_type)
 expect(catalog.capabilities).toHaveLength(104)
})
