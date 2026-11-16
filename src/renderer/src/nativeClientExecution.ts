import {recoveredNativeCompletion,resetNativeCompletionAuthoritiesForTests,type NativeCompletionPort} from './nativeCompletionRecovery'
import type {AgentTurn} from './types'
import type {QwenNativeClientDispatch,QwenNativeHostExecution,QwenNativeToolReceipt} from './qwenNativeProtocol'
export type NativeClientOutcome={final_response:string;tool_status:'completed'|'failed';tool_error_code?:string;result?:Record<string,unknown>}
export type NativeClientExecutionPort={
 dispatch:QwenNativeClientDispatch
 current:()=>boolean
 settled?:()=>boolean
 claim:()=>Promise<void>
 execute:()=>Promise<QwenNativeHostExecution>
 complete:(outcome:NativeClientOutcome)=>Promise<AgentTurn>
 read:()=>Promise<QwenNativeToolReceipt>
}
// Ports contain host-only authority. No model field or receipt read can create
// one. Completion may outlive a view only through Main's narrow completion lease.
const ports=new Map<string,NativeClientExecutionPort>()
export function registerNativeClientExecution(port:NativeClientExecutionPort):void{
 const d=port.dispatch,prior=ports.get(d.turn.agent_turn_id)
 if(prior&&prior.dispatch.dispatch_id!==d.dispatch_id)throw Error('NATIVE_CLIENT_DISPATCH_CHANGED')
 if(!prior&&ports.size>=256){for(const [key,value] of ports){if(value.settled?.())ports.delete(key);if(ports.size<256)break}}
 if(!prior&&ports.size>=256)throw Error('NATIVE_CLIENT_DISPATCH_CAPACITY')
 ports.set(d.turn.agent_turn_id,port)
}
export function nativeClientExecution(turnId:string):NativeClientExecutionPort|NativeCompletionPort|undefined{
 const live=ports.get(turnId);if(live?.current())return live
 const restored=recoveredNativeCompletion(turnId);if(restored?.current())return restored
 return live??restored
}
export function resetNativeClientExecutionsForTests():void{ports.clear();resetNativeCompletionAuthoritiesForTests()}
/** One real UI executor at a time. Readiness precedes claim and physical effect;
 * a later owner can never flush another child's queued dispatch. */
export class NativePreparedQueue{
 private tail:Promise<void>=Promise.resolve()
 run<T>(current:()=>boolean,ready:()=>boolean,work:()=>Promise<T>,timeoutMs=30000):Promise<T>{
  const job=this.tail.then(async()=>{
   const deadline=performance.now()+timeoutMs
   while(current()&&!ready()){
    if(performance.now()>=deadline)throw Error('NATIVE_CLIENT_UI_BUSY')
    await new Promise<void>(resolve=>setTimeout(resolve,25))
   }
   if(!current())throw Error('NATIVE_CLIENT_OWNER_CHANGED')
   return work()
  })
  this.tail=job.then(()=>undefined,()=>undefined);return job
 }
}
