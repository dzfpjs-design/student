import {resetNativeClientExecutionsForTests} from './nativeClientExecution'
import {expect,it,afterEach,vi} from 'vitest'
import {nativeCompletionDescriptor,persistNativeCompletion,registerNativeCompletion,recoveredNativeCompletion,resetNativeCompletionAuthoritiesForTests,type NativeCompletionDescriptor} from './nativeCompletionRecovery'
const d:NativeCompletionDescriptor={version:1,dispatch_id:'dispatch',action_id:'action',agent_turn_id:'turn',capability_name:'learning.capture_question',action_type:'request_capture',session_id:'session',child_id:'child',storage_generation:'generation',identity:{version:1,voice_input_id:'voice',supplier_session_id:'supplier',input_item_id:'item',response_id:'response',call_id:'call'}}
afterEach(()=>{resetNativeClientExecutionsForTests();resetNativeCompletionAuthoritiesForTests();localStorage.clear();vi.restoreAllMocks();vi.useRealTimers()})
it('durable descriptor admits only the exact metadata schema and never a transcript/lease or executable recovered turn',async()=>{
 expect(()=>nativeCompletionDescriptor({...d,text:'PRIVATE'})).toThrow('INVALID');expect(()=>nativeCompletionDescriptor({...d,completion_lease:'FORBIDDEN'})).toThrow('INVALID')
 persistNativeCompletion(d);const p=recoveredNativeCompletion('turn')!
 await expect(p.execute()).rejects.toThrow('NO_EXECUTION');await expect(p.claim()).rejects.toThrow('NO_EXECUTION');expect(p.current()).toBe(false)
})
it('a changed storage generation cannot borrow a locally saved completion, and unknown local results are never synthesized',async()=>{
 persistNativeCompletion(d)
 expect(()=>registerNativeCompletion({completion_descriptor:{...d,storage_generation:'another-generation'},current:()=>true,settled:()=>false,claim:vi.fn(),execute:vi.fn(),complete:vi.fn(),read:vi.fn()})).toThrow('CHANGED')
 vi.useFakeTimers();const p=recoveredNativeCompletion('turn')!,complete=p.complete({final_response:'原结果没有确认',tool_status:'failed'});const rejected=expect(complete).rejects.toThrow('HOST_UNCONFIRMED');await vi.advanceTimersByTimeAsync(5001);await rejected
 expect(localStorage.getItem('hsc-native-completion-v1:turn')).toBeTruthy()
})

it('a real claim ACK cannot start its concrete UI effect when recovery metadata could not be durably saved',async()=>{
 const {createNativeRuntimeBridge}=await import('./nativeRuntimeBridge')
 const turn={agent_turn_id:d.agent_turn_id,session_id:d.session_id,child_id:d.child_id,runtime_capability:d.capability_name,runtime_execution_location:'client_device_bridge',tool_status:'pending',transcript:'公开请求',action:{type:d.action_type,voice_input_id:d.identity.voice_input_id}} as import('./types').AgentTurn
 const dispatch={version:1 as const,dispatch_id:d.dispatch_id,action_id:d.action_id,turn,status:'pending' as const},effect=vi.fn()
 const forward=vi.fn(async(request:import('../../shared/nativeRealtimeCapability').NativeRealtimeForward)=>request.operation==='proposal'?{version:1,proposal_status:'admitted',execution:{status:'pending',location:'client_device_bridge'},public_result:{},client_dispatch:dispatch}:request.operation==='client'?{version:1,status:'claimed',client_dispatch:dispatch,completion_descriptor:d}:{version:1,proposal_status:'replayed',execution:{status:'unknown',location:'client_device_bridge'},public_result:{}})
 vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw Error('SYNTHETIC_STORAGE_FULL')})
 const bridge=createNativeRuntimeBridge({sessionId:d.session_id,childId:d.child_id,current:()=>true,forward,executePrepared:async(_turn,port)=>{await port.claim();effect()}})
 const result=await bridge.propose({...d.identity,host_connection_id:'host',capability_name:d.capability_name,args:{}})
 expect(result.execution.status).toBe('unknown');expect(effect).not.toHaveBeenCalled();expect(forward.mock.calls.filter(([r])=>r.operation==='client')).toHaveLength(1)
})
