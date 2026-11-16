import {nativeCompletionDescriptor,persistNativeCompletion,registerNativeCompletion,removeNativeCompletion,NATIVE_COMPLETION_READY,type NativeCompletionDescriptor} from './nativeCompletionRecovery'
import {nativeClientExecution,registerNativeClientExecution,type NativeClientExecutionPort,type NativeClientOutcome} from './nativeClientExecution'
import type {AgentTurn} from './types'
import type { NativeRealtimeForward, NativeRealtimeOperation } from '../../shared/nativeRealtimeCapability'
import { isQwenCurriculumTutorDispatch, isQwenNativeLocalSafetyControl } from './qwenNativeProtocol'
import type { QwenNativeLocalSafetyControl, QwenNativeRuntimeBridge, QwenNativeToolReceipt,QwenNativeClientDispatch,QwenNativeCapabilityProposal,QwenNativeHostExecution } from './qwenNativeProtocol'

type QwenCanonicalHousehold=import('./qwenNativeProtocol').QwenCanonicalAdmission&{source_kind:'household_local_transcript';raw_audio_available:false;entry_state:'unprocessed'|'processed'|'pending_read_only'}
export type NativeTraceMeta={phase?:'claim'|'execute'|'complete'|'read';capability?:string}
type Host={id:string;current:()=>boolean}
const liveHosts=new Map<string,Host>()
const checkedReceipt=(value:unknown):QwenNativeToolReceipt=>{
 const r=value as QwenNativeToolReceipt
 if(!r||r.version!==1||!r.execution||!['pending','completed','failed','unknown'].includes(r.execution.status)||typeof r.execution.location!=='string'||!r.public_result||typeof r.public_result!=='object'||Array.isArray(r.public_result))throw Error('QWEN_NATIVE_RECEIPT_INVALID')
 return r
}
const id=(v:unknown):v is string=>typeof v==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(v)
/** Canonical voice admission and Runtime calls share the actual Main owner.
 * Normal dialogue never enters the legacy intent/planner/admission journals. */
export function createNativeRuntimeBridge(options: {
  sessionId: string; childId: string
  current: () => boolean
  forward: (request: NativeRealtimeForward) => Promise<unknown>
  trace?: (operation: NativeRealtimeOperation, status: 'completed' | 'failed', durationMs: number,meta?:NativeTraceMeta) => void
  onReceipt?: (receipt: QwenNativeToolReceipt) => Promise<void>
  executePrepared?:(dispatch:QwenNativeClientDispatch,port:NativeClientExecutionPort)=>Promise<void>
}): QwenNativeRuntimeBridge {
  const scope=options.sessionId+'|'+options.childId
  const request = async (operation: NativeRealtimeOperation, host: string, body?: Record<string, unknown>,completionLease?:string,meta?:NativeTraceMeta): Promise<unknown> => {
    const started = performance.now()
    let status: 'completed' | 'failed' = 'failed'
    try {
    if (!options.current()&&!completionLease) throw Error('QWEN_NATIVE_CONTEXT_OWNER_CHANGED')
    const result = await options.forward({ session_id: options.sessionId, host_connection_id: host, operation, body,...(completionLease?{completion_lease:completionLease}:{}) })
    if (!options.current()&&!completionLease) throw Error('QWEN_NATIVE_CONTEXT_OWNER_CHANGED')
    if(['catalog','context','input'].includes(operation)&&options.current()){
      for(const [key,owner]of liveHosts)if(!owner.current())liveHosts.delete(key)
      if(!liveHosts.has(scope)&&liveHosts.size>=256)throw Error('NATIVE_CLIENT_HOST_CAPACITY')
      liveHosts.set(scope,{id:host,current:options.current})
    }
    status = 'completed'
    return result
    } finally { try { options.trace?.(operation, status, performance.now() - started,meta??(operation==='proposal'&&typeof body?.capability_name==='string'?{capability:body.capability_name}:undefined)) } catch { /* Diagnostics cannot alter a durable receipt. */ } }
  }
  const receipt = async (operation: 'proposal' | 'receipt', value: Parameters<QwenNativeRuntimeBridge['propose']>[0]
    | Parameters<QwenNativeRuntimeBridge['readReceipt']>[0]): Promise<QwenNativeToolReceipt> => {
    const result = checkedReceipt(await request(operation, value.host_connection_id, value))
    if (!result || result.version !== 1 || !result.execution
      || !['pending', 'completed', 'failed', 'unknown'].includes(result.execution.status)
      || !result.public_result || typeof result.public_result !== 'object') throw Error('QWEN_NATIVE_RECEIPT_INVALID')
    // A failed screen refresh cannot turn a received execution receipt into an
    // unknown effect or cause the supplier to repeat the operation.
    if(operation==='proposal'&&result.client_dispatch)return dispatch(value as QwenNativeCapabilityProposal,result.client_dispatch)
    if (operation === 'proposal' && result.execution.status === 'completed') await options.onReceipt?.(result).catch(() => undefined)
    return result
  }
  const checkedTurn=(turn:AgentTurn|undefined,original:AgentTurn):AgentTurn=>{
    if(!turn||turn.agent_turn_id!==original.agent_turn_id||turn.session_id!==options.sessionId||turn.child_id!==options.childId||turn.runtime_capability!==original.runtime_capability||turn.runtime_execution_location!==original.runtime_execution_location||turn.action?.type!==original.action?.type||turn.action?.voice_input_id!==original.action?.voice_input_id)throw Error('NATIVE_CLIENT_TURN_CHANGED')
    return turn
  }
  const dispatch=async(identity:QwenNativeCapabilityProposal,d:QwenNativeClientDispatch):Promise<QwenNativeToolReceipt>=>{
    if(!options.executePrepared||d.version!==1||d.status!=='pending'||!id(d.dispatch_id)||!id(d.action_id)||!d.turn
      ||d.turn.runtime_capability!==identity.capability_name||d.turn.action?.voice_input_id!==identity.voice_input_id)throw Error('NATIVE_CLIENT_DISPATCH_INVALID')
    checkedTurn(d.turn,d.turn)
    const prior=nativeClientExecution(d.turn.agent_turn_id)
    if(prior){if(('dispatch'in prior?prior.dispatch.dispatch_id:prior.completion_descriptor.dispatch_id)!==d.dispatch_id)throw Error('NATIVE_CLIENT_DISPATCH_CHANGED');return prior.read()}
    let claimStarted=false,executionStarted=false,claimed=false,completionLease:string|undefined,lastReceipt:QwenNativeToolReceipt|undefined,settled=false
    const body=(phase:'claim'|'execute'|'complete')=>({version:1,voice_input_id:identity.voice_input_id,supplier_session_id:identity.supplier_session_id,
      input_item_id:identity.input_item_id,response_id:identity.response_id,call_id:identity.call_id,dispatch_id:d.dispatch_id,phase,...(identity.input_source?{input_source:identity.input_source}:{})})
    const read=async():Promise<QwenNativeToolReceipt>=>{
      if(!options.current()&&lastReceipt)return lastReceipt
      const host=liveHosts.get(scope);const live=host?.current()?host.id:identity.host_connection_id
      const result=checkedReceipt(await request('receipt',live,{version:1,voice_input_id:identity.voice_input_id,supplier_session_id:identity.supplier_session_id,input_item_id:identity.input_item_id,response_id:identity.response_id,call_id:identity.call_id,...(identity.input_source?{input_source:identity.input_source}:{})},undefined,{phase:'read',capability:d.turn.runtime_capability}));lastReceipt=result;settled=['completed','failed'].includes(result.execution.status);return result
    }
    const port:NativeClientExecutionPort={dispatch:d,current:options.current,settled:()=>settled,
      claim:async()=>{
        if(claimed){if(!options.current())throw Error('NATIVE_CLIENT_NOT_CURRENT');return}
        if(claimStarted)throw Error('NATIVE_CLIENT_ALREADY_CLAIMED')
        claimStarted=true
        const result=await request('client',identity.host_connection_id,body('claim'),undefined,{phase:'claim',capability:d.turn.runtime_capability}) as {version:number;status:string;client_dispatch:QwenNativeClientDispatch;completion_lease?:string;completion_descriptor?:unknown}
        if(result?.version!==1||result.status!=='claimed'||result.client_dispatch?.dispatch_id!==d.dispatch_id
          ||result.client_dispatch.action_id!==d.action_id)throw Error('NATIVE_CLIENT_CLAIM_UNCONFIRMED')
        checkedTurn(result.client_dispatch.turn,d.turn)
        const recovery=nativeCompletionDescriptor(result.completion_descriptor)
        if(recovery.agent_turn_id!==d.turn.agent_turn_id||recovery.dispatch_id!==d.dispatch_id||recovery.action_id!==d.action_id||recovery.session_id!==options.sessionId||recovery.child_id!==options.childId||recovery.capability_name!==d.turn.runtime_capability||recovery.action_type!==d.turn.action.type
          ||recovery.identity.voice_input_id!==identity.voice_input_id||recovery.identity.supplier_session_id!==identity.supplier_session_id||recovery.identity.input_item_id!==identity.input_item_id||recovery.identity.response_id!==identity.response_id||recovery.identity.call_id!==identity.call_id||recovery.identity.input_source!==identity.input_source)throw Error('NATIVE_COMPLETION_DESCRIPTOR_CHANGED')
        persistNativeCompletion(recovery);claimed=true
        if(result.completion_lease!==undefined){if(!id(result.completion_lease))throw Error('NATIVE_CLIENT_LEASE_INVALID');completionLease=result.completion_lease}
      },
      execute:async()=>{
        if(!claimed||!options.current())throw Error('NATIVE_CLIENT_NOT_CURRENT')
        if(executionStarted)throw Error('NATIVE_CLIENT_ALREADY_EXECUTED')
        executionStarted=true
        const result=checkedReceipt(await request('client',identity.host_connection_id,body('execute'),undefined,{phase:'execute',capability:d.turn.runtime_capability}))
        lastReceipt=result
        const saved=result.host_execution;checkedTurn(saved?.turn,d.turn)
        if(!saved||saved.execution?.capability!==d.turn.runtime_capability)throw Error('NATIVE_CLIENT_EXECUTION_UNCONFIRMED')
        return saved
      },
      complete:async(outcome:NativeClientOutcome):Promise<AgentTurn>=>{
        if(!claimed)throw Error('NATIVE_CLIENT_NOT_CLAIMED')
        const live=liveHosts.get(scope),current=options.current(),host=current&&live?.current()?live.id:identity.host_connection_id
        if(!current&&!completionLease)throw Error('NATIVE_CLIENT_COMPLETION_OWNER_CHANGED')
        const result=checkedReceipt(await request('client',host,{...body('complete'),...outcome},current?undefined:completionLease,{phase:'complete',capability:d.turn.runtime_capability}))
        lastReceipt=result;const turn=checkedTurn(result.host_turn,d.turn)
        if(!['completed','failed'].includes(turn.tool_status))throw Error('NATIVE_CLIENT_OUTCOME_UNCONFIRMED')
        settled=true;removeNativeCompletion(d.turn.agent_turn_id);return turn
      },read}
    registerNativeClientExecution(port)
    try{await options.executePrepared(d,port)}catch{lastReceipt=await read();return lastReceipt}
    const final=await read();settled=['completed','failed'].includes(final.execution.status)
    if(settled&&options.current())await options.onReceipt?.(final).catch(()=>undefined)
    return final
  }
  return {
    loadCatalog: async host => await request('catalog', host) as Awaited<ReturnType<QwenNativeRuntimeBridge['loadCatalog']>>,
    loadContext: async host => {
      const context=await request('context',host) as Awaited<ReturnType<QwenNativeRuntimeBridge['loadContext']>>
      if(context?.pending_completions!==undefined){
        if(!Array.isArray(context.pending_completions)||context.pending_completions.length>64)throw Error('NATIVE_COMPLETION_CONTEXT_INVALID')
        for(const raw of context.pending_completions){
          const d=nativeCompletionDescriptor(raw)
          if(d.session_id!==options.sessionId||d.child_id!==options.childId)throw Error('NATIVE_COMPLETION_CONTEXT_OWNER_CHANGED')
          let terminal=false
          const current=()=>options.current()&&liveHosts.get(scope)?.id===host
          const deny=async():Promise<never>=>{throw Error('NATIVE_COMPLETION_ONLY_NO_EXECUTION')}
          const original={...d.identity,dispatch_id:d.dispatch_id}
          registerNativeCompletion({completion_descriptor:d,current,settled:()=>terminal,claim:deny,execute:deny,
            read:async()=>checkedReceipt(await request('receipt',host,d.identity,undefined,{phase:'read',capability:d.capability_name})),
            complete:async outcome=>{
              if(!current())throw Error('NATIVE_COMPLETION_CURRENT_HOST_UNCONFIRMED')
              const result=checkedReceipt(await request('client',host,{...original,phase:'complete',...outcome},undefined,{phase:'complete',capability:d.capability_name}))
              const t=result.host_turn
              if(!t||t.agent_turn_id!==d.agent_turn_id||t.session_id!==d.session_id||t.child_id!==d.child_id||t.runtime_capability!==d.capability_name||t.action?.type!==d.action_type||!['completed','failed'].includes(t.tool_status))throw Error('NATIVE_COMPLETION_OUTCOME_UNCONFIRMED')
              terminal=true;removeNativeCompletion(d.agent_turn_id);return t
            }})
        }
        if(context.pending_completions.length&&typeof window!=='undefined')window.dispatchEvent(new CustomEvent(NATIVE_COMPLETION_READY,{detail:{sessionId:options.sessionId,childId:options.childId}}))
      }
      return context
    },
    admit: async (utterance, capture, host) => {
      if (capture.source !== 'accepted_onset' || capture.sessionId !== options.sessionId || capture.reference.child_id !== options.childId
        || capture.reference.local_recovery_only === true || !utterance.trim()) throw Error('QWEN_NATIVE_INPUT_SOURCE_INVALID')
      const result = await request('input', host, { version: 1, local_input_id: capture.localInputId,
        transcript: utterance.trim(), captured_at_ms: capture.capturedAt, capture_reference: capture.reference }) as { voice_input_id?: unknown; local_control?: unknown; curriculum_tutor?: unknown }
      if (!result || typeof result.voice_input_id !== 'string' || !/^[A-Za-z0-9_-]{1,160}$/.test(result.voice_input_id)) throw Error('QWEN_NATIVE_INPUT_UNCONFIRMED')
      if (result.local_control !== undefined && !isQwenNativeLocalSafetyControl(result.local_control)) throw Error('QWEN_NATIVE_LOCAL_CONTROL_UNCONFIRMED')
      return {
        voice_input_id: result.voice_input_id,
        ...(result.local_control ? { local_control: result.local_control as QwenNativeLocalSafetyControl } : {}),
        ...(isQwenCurriculumTutorDispatch(result.curriculum_tutor) ? { curriculum_tutor: result.curriculum_tutor } : {}),
      }
    },
    admitHousehold:async(entry,host)=>{
      if(!entry?.entry_receipt||entry.entry_receipt.source_kind!=='household_local_transcript'||!entry.transcript.trim())throw Error('QWEN_NATIVE_INPUT_SOURCE_INVALID')
      const result=await request('input',host,{version:1,source_kind:'household_local_transcript',entry_receipt:entry.entry_receipt,transcript:entry.transcript,child_id:options.childId}) as QwenCanonicalHousehold
      if(!id(result?.voice_input_id)||result.source_kind!=='household_local_transcript'||result.raw_audio_available!==false||!['unprocessed','processed','pending_read_only'].includes(result.entry_state))throw Error('QWEN_NATIVE_INPUT_UNCONFIRMED')
      if(result.local_control!==undefined&&!isQwenNativeLocalSafetyControl(result.local_control))throw Error('QWEN_NATIVE_LOCAL_CONTROL_UNCONFIRMED')
      return result
    },
    propose: value => receipt('proposal', value),
    readReceipt: value => receipt('receipt', value),
    recordDialogue: async value => { await request('dialogue', value.host_connection_id, value) },
  }
}
