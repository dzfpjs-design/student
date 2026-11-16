import {createHash,createHmac,randomUUID} from 'node:crypto'
import {validNativePrivacy,type NativePrivacyMetadata,type NativePrivacyCommand,type NativePrivacyResponse} from '../shared/nativePrivacy'
import type {ControlledLaunchIntents} from './controlled-launch-intent'
import type {ControlledLaunchReceipt} from '../shared/controlledLaunch'
import {LocalDraftPrivacy,type DraftPrivacyBinding} from './local-draft-privacy'

export function canonical(value:unknown):string{
  if(value===null||typeof value==='string'||typeof value==='boolean')return JSON.stringify(value)
  if(typeof value==='number'&&Number.isSafeInteger(value))return JSON.stringify(value)
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']'
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical((value as Record<string,unknown>)[k])).join(',')+'}'
  throw Error('NATIVE_PRIVACY_COMMAND_INVALID')
}
export function signNativePrivacy(key:string|undefined,metadata:NativePrivacyMetadata):Record<string,unknown>{
  if(!key||!/^[a-f0-9]{64}$/.test(key)||!validNativePrivacy(metadata))throw Error('NATIVE_PRIVACY_AUTHORITY_UNAVAILABLE')
  return {...metadata,signature:createHmac('sha256',Buffer.from(key,'hex')).update(canonical(['native_privacy_permit_v1',metadata])).digest('hex')}
}
type Transport={
  call:(path:string,body:unknown|undefined,parentToken:string,metadata?:NativePrivacyMetadata)=>Promise<NativePrivacyResponse>;
  permit:(metadata:NativePrivacyMetadata,parentToken:string,fence:boolean)=>Promise<NativePrivacyMetadata&{status:string}>;
  end:(binding:ControlledLaunchReceipt)=>Promise<void>;
  closeLegacy:(childId:string|null,parentToken:string)=>Promise<void>;
}
const receiptStatus=(value:unknown):string|undefined=>value&&typeof value==='object'?String((value as {status?:unknown}).status):undefined
export class NativePrivacyCoordinator{
  private running:Promise<NativePrivacyResponse>|null=null
  constructor(private readonly journal:ControlledLaunchIntents,private readonly transport:Transport,private readonly drafts?:LocalDraftPrivacy){}
  private draftBinding(metadata:NativePrivacyMetadata,deletionId:string|null=null):DraftPrivacyBinding|null{
    if(metadata.kind==='delete'&&!['knowledge','all_history'].includes(metadata.scope??''))return null
    return {schema:1,kind:metadata.kind,child_id:metadata.child_id,scope:metadata.scope as DraftPrivacyBinding['scope'],
      request_id:metadata.request_id,expected_generation:metadata.expected_generation,cleanup_manifest_sha256:metadata.cleanup_manifest_sha256,
      deletion_id:deletionId,command_sha256:metadata.command_sha256}
  }
  private receiptMatches(binding:DraftPrivacyBinding,value:unknown):boolean{
    if(!value||typeof value!=='object'||Array.isArray(value)||binding.request_id===null)return false
    const v=value as Record<string,unknown>
    return v.request_id===binding.request_id&&v.kind===binding.kind&&v.child_id===binding.child_id&&v.scope===binding.scope
      &&v.origin_generation===binding.expected_generation&&v.cleanup_manifest_sha256===binding.cleanup_manifest_sha256
      &&typeof v.current_generation==='string'&&v.current_generation.length>0
  }
  private committedDraft(binding:DraftPrivacyBinding,response:NativePrivacyResponse):boolean{
    if(response.status!==200||!this.receiptMatches(binding,response.body))throw Error('DRAFT_PRIVACY_RECEIPT_MISMATCH')
    const receipt=response.body as Record<string,unknown>
    return receipt.status==='committed'&&receipt.cleanup_allowed===true&&receipt.result_generation===receipt.current_generation
  }
  private failedDraft(binding:DraftPrivacyBinding,response:NativePrivacyResponse):boolean{
    return response.status===200&&this.receiptMatches(binding,response.body)
      &&(response.body as {status?:unknown;cleanup_allowed?:unknown}).status==='failed'
      &&(response.body as {cleanup_allowed?:unknown}).cleanup_allowed===false
  }
  private async eraseDraft(binding:DraftPrivacyBinding,token:string,metadata:NativePrivacyMetadata|null):Promise<void>{
    const current=()=>{
      const now=this.journal.activePrivacy()
      if(metadata===null?now!==null:now===null||canonical(now)!==canonical(metadata))throw Error('NATIVE_PRIVACY_SCOPE_CHANGED')
      const draft=this.drafts!.active();if(!draft||canonical(draft)!==canonical(binding))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
    }
    current();await this.drafts!.prepareErase(binding);current()
    if(binding.request_id!==null){
      const receipt=await this.transport.call(`/v1/parent/privacy-operations/${encodeURIComponent(binding.request_id)}`,undefined,token)
      current();if(!this.committedDraft(binding,receipt))throw Error('DRAFT_PRIVACY_PENDING')
    }else{
      const context=await this.transport.call(`/v1/parent/deletions/${encodeURIComponent(binding.deletion_id!)}/native-context`,undefined,token)
      current();const value=context.body as Record<string,unknown>|null
      if(context.status!==200||!value||value.child_id!==binding.child_id||value.scope!==binding.scope||value.native_required!==false)throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
      const generation=await this.transport.call('/v1/parent/privacy-operations/generation',undefined,token)
      current();if(generation.status!==200||(generation.body as {generation?:unknown}).generation!==binding.expected_generation)throw Error('NATIVE_PRIVACY_GENERATION_CHANGED')
    }
    this.drafts!.erase(binding)
  }
  private async finishDraft(binding:DraftPrivacyBinding,token:string,claim:NativePrivacyMetadata|null):Promise<void>{await this.eraseDraft(binding,token,claim);this.drafts!.finish(binding)}
  private async recoverDraft(binding:DraftPrivacyBinding,token:string,claim:NativePrivacyMetadata|null):Promise<NativePrivacyResponse>{
    // Job-only legacy retries require explicit replay of that exact original job,
    // not an invented operation ID or native permit.
    if(binding.request_id===null){
      if(this.drafts!.dispatchState(binding)!=='prepared')throw Error('DRAFT_PRIVACY_ORIGINAL_RETRY_REQUIRED')
      const context=await this.transport.call(`/v1/parent/deletions/${encodeURIComponent(binding.deletion_id!)}/native-context`,undefined,token)
      const value=context.body as Record<string,unknown>|null
      if(context.status!==200)return context
      if(!value||value.child_id!==binding.child_id||value.scope!==binding.scope||value.native_required!==false)throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
      const current=await this.transport.call('/v1/parent/privacy-operations/generation',undefined,token)
      const generation=(current.body as {generation?:unknown}).generation
      if(current.status===200&&typeof generation==='string'&&generation.trim()&&generation.length<=128)this.drafts!.finish(binding)
      return current
    }
    const result=await this.transport.call(`/v1/parent/privacy-operations/${encodeURIComponent(binding.request_id)}`,undefined,token)
    if(result.status===200){
      if(this.committedDraft(binding,result))await this.finishDraft(binding,token,claim)
      else if(this.failedDraft(binding,result))this.drafts!.finish(binding)
      else if(this.drafts!.dispatchState(binding)==='prepared'&&receiptStatus(result.body)==='superseded')this.drafts!.finish(binding)
    }else if(result.status===404&&this.drafts!.dispatchState(binding)==='prepared'){
      // This fresh parent-authenticated read plus our durable non-dispatch fact
      // releases only the local fence. It grants no erase and sends no request.
      const current=await this.transport.call('/v1/parent/privacy-operations/generation',undefined,token)
      const generation=(current.body as {generation?:unknown}).generation
      if(current.status===200&&typeof generation==='string'&&generation.trim()&&generation.length<=128)this.drafts!.finish(binding)
    }
    return result
  }
  private async operation(metadata:NativePrivacyMetadata,token:string):Promise<NativePrivacyResponse>{
    return this.transport.call(`/v1/parent/privacy-operations/${encodeURIComponent(metadata.request_id)}`,undefined,token)
  }
  private async resolve(metadata:NativePrivacyMetadata,token:string,explicit:boolean):Promise<NativePrivacyResponse>{
    let permit=await this.transport.permit(metadata,token,false)
    if(permit.nonce!==metadata.nonce)throw Error('NATIVE_PRIVACY_SCOPE_CHANGED')
    if(['missing','claimed'].includes(permit.status)&&explicit)permit=await this.transport.permit(metadata,token,true)
    if(!['finished','failed','cancelled'].includes(permit.status))throw Error('NATIVE_PRIVACY_PENDING')
    const operation=await this.operation(metadata,token)
    // Show the existing exact finalization action, retaining the native cleanup
    // barrier until that original receipt is actually committed.
    if(operation.status===200&&receiptStatus(operation.body)==='finalization_required')return operation
    const committed=permit.status==='finished'||(operation.status===200&&receiptStatus(operation.body)==='committed')
    const draft=this.drafts?.active()
    let releaseFailed=false
    if(draft&&this.draftBinding(metadata)){
      if(draft.request_id!==metadata.request_id||draft.command_sha256!==metadata.command_sha256)throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
      if(committed){
        if(!this.committedDraft(draft,operation))throw Error('DRAFT_PRIVACY_PENDING')
        await this.eraseDraft(draft,token,metadata)
      }else if(['failed','cancelled'].includes(permit.status)&&this.failedDraft(draft,operation))releaseFailed=true
      else return operation
    }
    this.journal.finishPrivacy(metadata,committed)
    if(draft&&this.draftBinding(metadata)&&(committed||releaseFailed))this.drafts!.finish(draft)
    return operation
  }
  async recover(requestId:string,token:string,explicit=false):Promise<NativePrivacyResponse>{
    if(this.running)throw Error('NATIVE_PRIVACY_BUSY')
    const metadata=this.journal.activePrivacy()
    if(!metadata){
      const draft=this.drafts?.active()
      if(draft){
        if(draft.request_id!==requestId&&!(draft.request_id===null&&draft.deletion_id===requestId))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
        const running=this.recoverDraft(draft,token,metadata);this.running=running
        try{return await running}finally{if(this.running===running)this.running=null}
      }
      return this.transport.call(`/v1/parent/privacy-operations/${encodeURIComponent(requestId)}`,undefined,token)
    }
    if(metadata.request_id!==requestId)throw Error('NATIVE_PRIVACY_SCOPE_CHANGED')
    const binding=this.draftBinding(metadata)
    if(binding&&this.drafts){
      const prior=this.drafts.active()
      this.drafts.begin(prior?{...binding,deletion_id:prior.deletion_id}:binding)
    }
    const running=this.resolve(metadata,token,explicit);this.running=running
    try{return await running}finally{if(this.running===running)this.running=null}
  }
  execute(command:NativePrivacyCommand,token:string):Promise<NativePrivacyResponse>{
    if(this.running)return Promise.reject(Error('NATIVE_PRIVACY_BUSY'))
    const running=this.perform(command,token)
    this.running=running
    void running.finally(()=>{if(this.running===running)this.running=null}).catch(()=>undefined)
    return running
  }
  private async knowledge(command:NativePrivacyCommand,token:string,binding:DraftPrivacyBinding,retry:boolean,claim:NativePrivacyMetadata|null):Promise<NativePrivacyResponse>{
    const drafts=this.drafts!
    const pending=drafts.active()
    if(retry&&pending?.request_id){
      const original=await this.transport.call(`/v1/parent/privacy-operations/${encodeURIComponent(pending.request_id)}`,undefined,token)
      if(original.status!==200||!this.receiptMatches(pending,original.body))throw Error('DRAFT_PRIVACY_RECEIPT_MISMATCH')
      const receipt=original.body as {reference?:{type?:unknown;id?:unknown}}
      if(receipt.reference?.type!=='deletion_job'||receipt.reference.id!==binding.deletion_id||pending.child_id!==binding.child_id||pending.scope!==binding.scope)throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
      binding={...pending,deletion_id:binding.deletion_id,command_sha256:binding.command_sha256}
    }
    if(binding.request_id===null){
      const current=await this.transport.call('/v1/parent/privacy-operations/generation',undefined,token)
      if(current.status!==200)return current
      const generation=(current.body as {generation?:unknown}).generation
      if(typeof generation!=='string'||!generation.trim()||generation.length>128)throw Error('DRAFT_PRIVACY_RECEIPT_MISMATCH')
      if(pending&&pending.expected_generation!==generation)throw Error('NATIVE_PRIVACY_GENERATION_CHANGED')
      binding={...binding,expected_generation:generation}
    }
    drafts.begin(binding,retry)
    if(binding.request_id!==null){
      const current=await this.transport.call('/v1/parent/privacy-operations/generation',undefined,token)
      if(current.status!==200)return current
      if((current.body as {generation?:unknown}).generation!==binding.expected_generation)throw Error('NATIVE_PRIVACY_GENERATION_CHANGED')
      const original=await this.transport.call(`/v1/parent/privacy-operations/${encodeURIComponent(binding.request_id)}`,undefined,token)
      if(original.status===200){
        if(this.committedDraft(binding,original)){
          await this.finishDraft(binding,token,claim);return {status:200,body:{operation_receipt:original.body,replayed:true}}
        }
        if(this.failedDraft(binding,original)&&!retry)this.drafts!.finish(binding)
        else if(receiptStatus(original.body)==='superseded'&&drafts.dispatchState(binding)==='prepared')this.drafts!.finish(binding)
        if(!retry||receiptStatus(original.body)==='finalization_required')return {status:200,body:{operation_receipt:original.body,replayed:true}}
        if(receiptStatus(original.body)!=='failed')throw Error('DRAFT_PRIVACY_ORIGINAL_UNRESOLVED')
      }else if(original.status!==404||retry)throw Error('DRAFT_PRIVACY_ORIGINAL_UNRESOLVED')
    }
    drafts.markDispatched(binding)
    const result=await this.transport.call(command.path,command.body,token)
    if(binding.request_id!==null){
      const original=await this.transport.call(`/v1/parent/privacy-operations/${encodeURIComponent(binding.request_id)}`,undefined,token)
      if(original.status===200&&this.committedDraft(binding,original))await this.finishDraft(binding,token,claim)
    }else if(result.status===200){
      const job=result.body as Record<string,unknown>|null
      if(!job||job.deletion_id!==binding.deletion_id||job.child_id!==binding.child_id||job.scope!=='knowledge'||job.target_type!=='child_scope')throw Error('DRAFT_PRIVACY_RECEIPT_MISMATCH')
      if(job.status==='completed'){
        // The existing authenticated native-context checks original generation and
        // target again. Never accept a completed job from another child/scope.
        const context=await this.transport.call(`/v1/parent/deletions/${encodeURIComponent(binding.deletion_id!)}/native-context`,undefined,token)
        const value=context.body as Record<string,unknown>|null
        if(context.status!==200||!value||value.child_id!==binding.child_id||value.scope!=='knowledge'||value.native_required!==false)throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
        const current=await this.transport.call('/v1/parent/privacy-operations/generation',undefined,token)
        if(current.status!==200||(current.body as {generation?:unknown}).generation!==binding.expected_generation)throw Error('NATIVE_PRIVACY_GENERATION_CHANGED')
        await this.finishDraft(binding,token,claim)
      }
    }
    return result
  }
  private async perform(command:NativePrivacyCommand,token:string):Promise<NativePrivacyResponse>{
    const originalClaim=this.journal.activePrivacy()
    if(!token||typeof token!=='string'||token.length>4096||!command||typeof command.path!=='string'||!command.body||Array.isArray(command.body))throw Error('NATIVE_PRIVACY_COMMAND_INVALID')
    const deletion=/^\/v1\/parent\/children\/([^/]+)\/delete$/.exec(command.path)
    const retry=/^\/v1\/parent\/deletions\/([^/]+)\/retry$/.exec(command.path)
    const kind=deletion||retry?'delete':command.path==='/v1/parent/backups/restore'?'restore':command.path==='/v1/parent/backups/portable/restore'?'portable_restore':null
    if(!kind)throw Error('NATIVE_PRIVACY_COMMAND_INVALID')
    let operation=command.body.operation as Record<string,unknown>|undefined
    let childId=deletion?decodeURIComponent(deletion[1]):null
    let scope=deletion?command.body.scope as string:null
    if(retry){
      const context=await this.transport.call(`/v1/parent/deletions/${retry[1]}/native-context`,undefined,token)
      if(context.status!==200)return context
      const value=context.body as {operation:Record<string,unknown>;child_id:string;scope:string;native_required:boolean}
      if(!value.native_required){
        if(this.drafts&&value.scope==='knowledge')return this.knowledge(command,token,{schema:1,kind:'delete',child_id:value.child_id,
          scope:'knowledge',request_id:null,expected_generation:null,cleanup_manifest_sha256:null,deletion_id:decodeURIComponent(retry[1]),
          command_sha256:createHash('sha256').update(canonical([command.path,command.body])).digest('hex')},true,originalClaim)
        return this.transport.call(command.path,command.body,token)
      }
      operation=value.operation;childId=value.child_id;scope=value.scope
    }
    if(kind==='delete'&&!['media','all_history'].includes(scope??'')&&!(this.drafts&&scope==='knowledge'))return this.transport.call(command.path,command.body,token)
    const metadata:NativePrivacyMetadata={nonce:randomUUID(),request_id:operation?.request_id as string,
      expected_generation:operation?.expected_generation as string,kind,child_id:childId,scope,
      command_sha256:createHash('sha256').update(canonical([command.path,command.body])).digest('hex'),
      cleanup_manifest_sha256:operation?.cleanup_manifest_sha256 as string}
    if(!validNativePrivacy(metadata))throw Error('NATIVE_PRIVACY_COMMAND_INVALID')
    if(this.drafts&&scope==='knowledge')return this.knowledge(command,token,this.draftBinding(metadata)!,false,originalClaim)
    const pending=this.journal.activePrivacy()
    if(originalClaim===null?pending!==null:pending===null||canonical(pending)!==canonical(originalClaim))throw Error('NATIVE_PRIVACY_SCOPE_CHANGED')
    if(pending){
      if(pending.request_id!==metadata.request_id||pending.command_sha256!==metadata.command_sha256)throw Error('NATIVE_PRIVACY_PENDING')
      const draft=this.draftBinding(pending);if(draft&&this.drafts)this.drafts.begin(draft,!!retry)
      const original=await this.resolve(pending,token,true)
      return original.status===200?{status:200,body:{operation_receipt:original.body,replayed:true}}:original
    }
    const draft=this.draftBinding(metadata,retry?decodeURIComponent(retry[1]):null)
    if(draft&&this.drafts)this.drafts.begin(draft,!!retry)
    this.journal.beginPrivacy(metadata)
    let dispatched=false
    try{
      const current=await this.transport.call('/v1/parent/privacy-operations/generation',undefined,token)
      if(current.status!==200){this.journal.finishPrivacy(metadata,false);return current}
      if((current.body as {generation:string}).generation!==metadata.expected_generation)throw Error('NATIVE_PRIVACY_GENERATION_CHANGED')
      const original=await this.operation(metadata,token)
      if(original.status===200&&['committed','failed','superseded'].includes(receiptStatus(original.body)??'')&&!(retry&&receiptStatus(original.body)==='failed')){
        if(draft&&this.drafts&&this.committedDraft(draft,original))await this.eraseDraft(draft,token,metadata)
        this.journal.finishPrivacy(metadata,false)
        if(draft&&this.drafts&&(receiptStatus(original.body)==='committed'||this.failedDraft(draft,original)
          ||receiptStatus(original.body)==='superseded'&&this.drafts.dispatchState(draft)==='prepared'))this.drafts.finish(draft)
        return {status:200,body:{operation_receipt:original.body,replayed:true}}
      }
      if(original.status!==404&&!(retry&&original.status===200&&receiptStatus(original.body)==='failed'))throw Error('NATIVE_PRIVACY_ORIGINAL_UNRESOLVED')
      for(const intent of this.journal.privacyIntents(metadata)){
        const proof=await this.journal.cancel(intent.intentId)
        if(!proof.closed)throw Error('NATIVE_PRIVACY_RESOURCE_UNCONFIRMED')
        if(proof.noActivity)continue
        if(!proof.binding||proof.sessionId!==proof.binding.activityId)throw Error('NATIVE_PRIVACY_RESOURCE_UNCONFIRMED')
        await this.transport.end(proof.binding)
        await this.journal.finish(intent.intentId)
      }
      await this.transport.closeLegacy(childId,token)
      if(draft&&this.drafts)this.drafts.markDispatched(draft)
      dispatched=true
      const result=await this.transport.call(command.path,command.body,token,metadata)
      // Even a HTTP error needs a durable permit fence: a lost response is not
      // evidence that a destructive request never reached the backend.
      await this.resolve(metadata,token,true)
      return result
    }catch(error){
      if(!dispatched){
        // The live main knows it did not send the destructive POST. The server
        // fence also prevents a later original-key request, without clearing data.
        try{await this.resolve(metadata,token,true)}catch{/* Original barrier remains durable for explicit parent recovery. */}
      }
      throw error
    }
  }
}
