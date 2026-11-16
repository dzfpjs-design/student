import type {AgentTurn} from './types'
import type {NativeClientOutcome} from './nativeClientExecution'
import type {QwenNativeHostExecution,QwenNativeToolReceipt,QwenNativeIdentity} from './qwenNativeProtocol'
export type NativeCompletionDescriptor={version:1;dispatch_id:string;action_id:string;agent_turn_id:string;capability_name:string;action_type:string;session_id:string;child_id:string;storage_generation:string;identity:Omit<QwenNativeIdentity,'host_connection_id'>&{call_id:string}}
export const NATIVE_COMPLETION_READY='hsc-native-completion-ready'
const PREFIX='hsc-native-completion-v1:'
const ids=['dispatch_id','action_id','agent_turn_id','session_id','child_id','storage_generation'] as const
const identifier=(v:unknown):v is string=>typeof v==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(v)
export function nativeCompletionDescriptor(value:unknown):NativeCompletionDescriptor{
 const d=value as NativeCompletionDescriptor
 if(!d||d.version!==1||Object.keys(d).sort().join(',')!=='action_id,action_type,agent_turn_id,capability_name,child_id,dispatch_id,identity,session_id,storage_generation,version'
  ||ids.some(k=>!identifier(d[k]))||typeof d.capability_name!=='string'||!/^[a-z][a-z0-9_.]{0,99}$/.test(d.capability_name)||!identifier(d.action_type)||!d.identity)throw Error('NATIVE_COMPLETION_DESCRIPTOR_INVALID')
 const i=d.identity,keys=Object.keys(i).filter(k=>k!=='input_source').sort().join(',')
 if(i.version!==1||keys!=='call_id,input_item_id,response_id,supplier_session_id,version,voice_input_id'
  ||[i.voice_input_id,i.supplier_session_id,i.input_item_id,i.response_id,i.call_id].some(v=>!identifier(v))
  ||i.input_source!==undefined&&!['persisted_native','household_local_transcript'].includes(i.input_source))throw Error('NATIVE_COMPLETION_DESCRIPTOR_INVALID')
 return structuredClone(d)
}
function fingerprint(d:NativeCompletionDescriptor):string{return JSON.stringify([d.version,...ids.map(k=>d[k]),d.capability_name,d.action_type,d.identity.version,d.identity.voice_input_id,d.identity.supplier_session_id,d.identity.input_item_id,d.identity.response_id,d.identity.call_id,d.identity.input_source??null])}
export function persistNativeCompletion(value:unknown):NativeCompletionDescriptor{
 const d=nativeCompletionDescriptor(value),key=PREFIX+d.agent_turn_id
 const old=localStorage.getItem(key)
 if(old&&fingerprint(nativeCompletionDescriptor(JSON.parse(old)))!==fingerprint(d))throw Error('NATIVE_COMPLETION_DESCRIPTOR_CHANGED')
 localStorage.setItem(key,JSON.stringify(d))
 if(localStorage.getItem(key)!==JSON.stringify(d))throw Error('NATIVE_COMPLETION_DESCRIPTOR_NOT_SAVED')
 return d
}
export function removeNativeCompletion(turnId:string):void{localStorage.removeItem(PREFIX+turnId)}
export type NativeCompletionPort={completion_descriptor:NativeCompletionDescriptor;current:()=>boolean;settled:()=>boolean;claim:()=>Promise<void>;execute:()=>Promise<QwenNativeHostExecution>;complete:(value:NativeClientOutcome)=>Promise<AgentTurn>;read:()=>Promise<QwenNativeToolReceipt>}
const verified=new Map<string,NativeCompletionPort>(),waiting=new Map<string,NativeCompletionPort>()
/** Only a fresh signed context can install this read/complete authority. The
 * durable descriptor alone never grants a host or execution permission. */
export function registerNativeCompletion(port:NativeCompletionPort):void{
 const d=nativeCompletionDescriptor(port.completion_descriptor),key=PREFIX+d.agent_turn_id,old=localStorage.getItem(key)
 if(old&&fingerprint(nativeCompletionDescriptor(JSON.parse(old)))!==fingerprint(d))throw Error('NATIVE_COMPLETION_DESCRIPTOR_CHANGED')
 for(const [id,p]of verified)if(!p.current())verified.delete(id)
 if(!verified.has(d.agent_turn_id)&&verified.size>=256)throw Error('NATIVE_COMPLETION_CAPACITY')
 verified.set(d.agent_turn_id,port)
}
export function recoveredNativeCompletion(turnId:string):NativeCompletionPort|undefined{
 const live=verified.get(turnId);if(live?.current())return live
 const raw=localStorage.getItem(PREFIX+turnId)
 if(!raw)return undefined
 const d=nativeCompletionDescriptor(JSON.parse(raw));if(d.agent_turn_id!==turnId)throw Error('NATIVE_COMPLETION_DESCRIPTOR_CHANGED')
 const old=waiting.get(turnId);if(old)return old
 if(waiting.size>=256)throw Error('NATIVE_COMPLETION_CAPACITY')
 const authority=async():Promise<NativeCompletionPort>=>{
  const deadline=performance.now()+5000
  while(performance.now()<deadline){const p=verified.get(turnId);if(p?.current()&&fingerprint(p.completion_descriptor)===fingerprint(d))return p;await new Promise<void>(done=>setTimeout(done,25))}
  throw Error('NATIVE_COMPLETION_CURRENT_HOST_UNCONFIRMED')
 }
 const deny=async():Promise<never>=>{throw Error('NATIVE_COMPLETION_ONLY_NO_EXECUTION')}
 const port:NativeCompletionPort={completion_descriptor:d,current:()=>verified.get(turnId)?.current()===true,settled:()=>verified.get(turnId)?.settled()===true,claim:deny,execute:deny,
  complete:async value=>(await authority()).complete(value),read:async()=>(await authority()).read()}
 waiting.set(turnId,port);return port
}
export function resetNativeCompletionAuthoritiesForTests():void{verified.clear();waiting.clear()}
