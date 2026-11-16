import type {NativeRealtimeForward} from '../../shared/nativeRealtimeCapability'
import {afterEach,expect,it,vi} from 'vitest'
import {createNativeRuntimeBridge} from './nativeRuntimeBridge'
import {localApi} from './api'
import {NativePreparedQueue,resetNativeClientExecutionsForTests} from './nativeClientExecution'
import type {AgentTurn} from './types'
import type {QwenNativeCapabilityProposal,QwenNativeClientDispatch,QwenNativeToolReceipt} from './qwenNativeProtocol'
const turn={agent_turn_id:'public-port-turn',session_id:'public-session',child_id:'public-child',transcript:'查看学习进度',response_text:'真实进度已读到。',runtime_capability:'system.inspect_learning',runtime_execution_location:'server',tool_status:'pending',action:{type:'none',voice_input_id:'public-voice',source:'qwen_realtime_capability'}} as AgentTurn
const dispatch:QwenNativeClientDispatch={version:1,dispatch_id:'public-dispatch',action_id:'public-action',turn,status:'pending'}
const identity:QwenNativeCapabilityProposal={version:1,host_connection_id:'public-host',voice_input_id:'public-voice',supplier_session_id:'public-supplier',input_item_id:'public-item',response_id:'public-response',call_id:'public-call',capability_name:'system.inspect_learning',args:{scope:'progress',track:'all'}}
const pending:QwenNativeToolReceipt={version:1,proposal_status:'admitted',execution:{status:'pending',location:'server'},public_result:{status:'pending'}}
afterEach(()=>{resetNativeClientExecutionsForTests();localStorage.clear();vi.restoreAllMocks();vi.unstubAllGlobals()})
it('an uncertain original execute cannot repeat through API helpers or fall back to ordinary HTTP, and readonly receipts never dispatch',async()=>{
 const net=vi.fn().mockRejectedValue(Error('ORDINARY_HTTP_FORBIDDEN'));vi.stubGlobal('fetch',net)
 const phases:string[]=[],forward=vi.fn(async(request:NativeRealtimeForward)=>{
  if(request.operation==='proposal')return {...pending,client_dispatch:dispatch}
  if(request.operation==='receipt')return {...pending,execution:{status:'unknown',location:'server'}}
  if(request.operation==='client'){phases.push(String(request.body?.phase));if(request.body?.phase==='claim')return {version:1,status:'claimed',client_dispatch:dispatch,completion_descriptor:{version:1,dispatch_id:dispatch.dispatch_id,action_id:dispatch.action_id,agent_turn_id:turn.agent_turn_id,capability_name:turn.runtime_capability,action_type:turn.action.type,session_id:turn.session_id,child_id:turn.child_id,storage_generation:'public-generation',identity:{version:1,voice_input_id:identity.voice_input_id,supplier_session_id:identity.supplier_session_id,input_item_id:identity.input_item_id,response_id:identity.response_id,call_id:identity.call_id}}};throw Error('SYNTHETIC_RESPONSE_LOST_AFTER_EFFECT_MAY_HAVE_STARTED')}
  throw Error('UNEXPECTED_OPERATION')
 })
 const bridge=createNativeRuntimeBridge({sessionId:'public-session',childId:'public-child',current:()=>true,forward,
  executePrepared:async(_d,port)=>{await port.claim();await expect(localApi.executeAgentTurn(turn.agent_turn_id)).rejects.toThrow('RESPONSE_LOST');await expect(localApi.executeAgentTurn(turn.agent_turn_id)).rejects.toThrow('ALREADY_EXECUTED')}})
 await expect(bridge.propose(identity)).resolves.toMatchObject({execution:{status:'unknown'}})
 await bridge.readReceipt(identity);expect(phases).toEqual(['claim','execute']);expect(net).not.toHaveBeenCalled()
})
it('waits for the original UI before claim and discards the queued effect when that exact owner leaves',async()=>{
 let current=true,ready=false;const queue=new NativePreparedQueue(),effect=vi.fn()
 const job=queue.run(()=>current,()=>ready,effect,1000)
 await Promise.resolve();expect(effect).not.toHaveBeenCalled();current=false;ready=true
 await expect(job).rejects.toThrow('OWNER_CHANGED');expect(effect).not.toHaveBeenCalled()
})
