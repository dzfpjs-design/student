import { canonicalCandidatePayload } from './textbookCandidateCommands'
import { ApiError } from './api'
import type { ParentDraftRecoverySnapshot, ParentDraftRecoveryPrepared } from '../../shared/parentDraftRecovery'
import type { ParentDraftRecoveryConfiguration } from './parentDraftRecoveryPort'

export type PaperObjectiveDraft = {
  question_number: string
  full_question: string
  official_answers: string[]
  candidate_node_id: string | null
  paper: { capture_ref: string; source_version: string }
  answer_key: { capture_ref: string; source_version: string }
  raw_editing?: { full_question:string;official_answers_text:string }
}
export type TextbookEditingPayload = {
  page: Record<string, unknown>
  capture_ref: string
  capture_version: string
  review_drafts: Record<string, { text: string; source_version: string }>
  action_ids: Record<string, string>
  objective_draft?: PaperObjectiveDraft | null
}
type DraftReceipt = {
  child_id:string;scope:string;storage_generation:string
  revision: number; payload: TextbookEditingPayload | null
  last_action_id: string | null; last_expected_revision: number | null
  updated_at: string | null; stale_refs: string[]; cloud_transmission_allowed: false
}
type Api = (path: string, init?: RequestInit) => Promise<unknown>
type Phase = 'loading' | 'saved' | 'saving' | 'invalid' | 'rejected' | 'error' | 'conflict'
export type TextbookDraftComparison = {
  local: TextbookEditingPayload | null
  saved: TextbookEditingPayload | null
  generationChanged: boolean
}
const canonical = (value: TextbookEditingPayload | null): string => value === null ? 'null' : canonicalCandidatePayload(value)
export const objectiveTextLength = (text:string):number => Array.from(text).length

function validObjective(value: PaperObjectiveDraft | null | undefined): boolean {
  if (value == null) return true
  const raw=value.raw_editing
  const rawValid=raw===undefined||(raw&&Object.keys(raw).sort().join(',')==='full_question,official_answers_text'
    &&typeof raw.full_question==='string'&&objectiveTextLength(raw.full_question)<=100000&&typeof raw.official_answers_text==='string'&&objectiveTextLength(raw.official_answers_text)<=100000)
  const answers=typeof raw?.official_answers_text==='string'?raw.official_answers_text.split('\n'):undefined
  return typeof value === 'object' && Object.keys(value).filter(key=>key!=='raw_editing').sort().join(',') === 'answer_key,candidate_node_id,full_question,official_answers,paper,question_number'
    &&!!rawValid&&(!raw||(value.full_question===(objectiveTextLength(raw.full_question)<=1200?raw.full_question:'')
      &&JSON.stringify(value.official_answers)===JSON.stringify(answers!.length<=24&&answers!.every(answer=>objectiveTextLength(answer)<=200)?answers:[])))
    && typeof value.question_number === 'string' && objectiveTextLength(value.question_number) <= 40
    && typeof value.full_question === 'string' && objectiveTextLength(value.full_question) <= 1200
    && Array.isArray(value.official_answers) && value.official_answers.length <= 24
    && value.official_answers.every(answer => typeof answer === 'string' && objectiveTextLength(answer) <= 200)
    && (value.candidate_node_id === null || typeof value.candidate_node_id === 'string' && value.candidate_node_id.length > 0 && value.candidate_node_id.length <= 120)
    && [value.paper,value.answer_key].every(ref => ref && typeof ref === 'object'
      && Object.keys(ref).sort().join(',') === 'capture_ref,source_version'
      && typeof ref.capture_ref === 'string' && ref.capture_ref.length > 0 && ref.capture_ref.length <= 80
      && typeof ref.source_version === 'string' && ref.source_version.length > 0 && ref.source_version.length <= 80)
    && value.paper.capture_ref !== value.answer_key.capture_ref
}
function validEditing(value:TextbookEditingPayload|null):boolean {
  return validObjective(value?.objective_draft)&&new TextEncoder().encode(JSON.stringify(value)).byteLength<=1000000
}

function receipt(value: unknown, childId: string, scope: string): DraftReceipt {
  const draft = (value as { draft?: DraftReceipt } | null)?.draft
  if (!draft || draft.child_id !== childId || draft.scope !== scope || typeof draft.storage_generation !== 'string' || draft.storage_generation.length < 8
    || !Number.isSafeInteger(draft.revision) || draft.revision < 0 || draft.cloud_transmission_allowed !== false
    || !Array.isArray(draft.stale_refs) || !draft.stale_refs.every(ref => typeof ref === 'string')
    || !(draft.payload === null || (typeof draft.payload === 'object' && draft.payload.page
      && typeof draft.payload.capture_ref === 'string' && typeof draft.payload.capture_version === 'string'
      && draft.payload.review_drafts && draft.payload.action_ids && validObjective(draft.payload.objective_draft)
      && (scope === 'capture' || draft.payload.objective_draft == null)))) throw new Error('草稿回执不完整，未覆盖原稿。')
  return draft
}

/** Server acknowledgement and encrypted native recovery are separate receipts. */
export class ParentTextbookDraftJournal {
  readonly path: string
  state: { phase: Phase; payload: TextbookEditingPayload | null; staleRefs: string[]; message: string }
    = { phase: 'loading', payload: null, staleRefs: [], message: '正在恢复本机草稿。' }
  private acknowledged: DraftReceipt | null = null
  private latest: TextbookEditingPayload | null = null
  private pending: { expected_revision: number; expected_generation:string; client_action_id: string; payload: TextbookEditingPayload | null } | null = null
  private running: Promise<void> | null = null
  private loading: Promise<DraftReceipt> | null = null
  private conflictPreview: { receipt: DraftReceipt; local: TextbookEditingPayload | null } | null = null
  private listeners = new Set<() => void>()
  private authorityTurn = 0
  private authorityStamp: number
  private mode: 'active' | 'suspended' | 'reconciling' = 'active'
  private nativeLease: ParentDraftRecoveryPrepared | null = null
  private nativeSequence = 0
  private protectedSnapshot: string | null = null
  private needsNativeRead = false
  private nativeRestored = false
  get requiresExplicitRecoveryEdit():boolean { return this.nativeRestored }
  recoveryMessage = ''
  get recoveryAvailable(): boolean { return this.needsNativeRead }
  private snapshot(): ParentDraftRecoverySnapshot {
    return structuredClone({schema:1,childId:this.childId,scope:this.scope,acknowledged:this.acknowledged,latest:this.latest,pending:this.pending})
  }
  private dirty(snapshot=this.snapshot()):boolean {
    return snapshot.pending!==null||canonical(snapshot.latest)!==canonical(snapshot.acknowledged?.payload??null)
  }
  private checkpoint(): boolean {
    if(this.recovery==='memory-only')return true
    const lease=this.nativeLease,turn=this.authorityTurn
    try {
      const snapshot=this.snapshot(),encoded=canonicalCandidatePayload(snapshot)
      if(encoded===this.protectedSnapshot)return true
      if(!this.recovery||!this.nativeLease||this.needsNativeRead)throw Error('NATIVE_DRAFT_RECOVERY_UNAVAILABLE')
      const sequence=++this.nativeSequence
      const ack=this.recovery.checkpoint(this.nativeLease.leaseId,sequence,snapshot)
      const stored=ack.stored,ackSequence=ack.sequence,localRevision=ack.localRevision
      if(this.nativeLease!==lease||this.authorityTurn!==turn||this.nativeSequence!==sequence)return false
      if(stored!==true||ackSequence!==sequence||!Number.isSafeInteger(localRevision)||localRevision<=this.nativeLease.localRevision)throw Error('NATIVE_DRAFT_RECOVERY_ACK_INVALID')
      this.nativeLease={...this.nativeLease,localRevision:ack.localRevision};this.protectedSnapshot=encoded
      this.recoveryMessage=this.dirty(snapshot)?'未确认编辑已加密保护在这台电脑；这不表示已获服务保存回执。':'已保存草稿的本机加密保护已确认。'
      return true
    } catch {
      if(this.nativeLease!==lease||this.authorityTurn!==turn)return false
      this.protectedSnapshot=null
      this.recoveryMessage=this.dirty()?'尚未确认这台电脑的加密保护；请保留窗口并重试，不要强退或重启。':'本机额外加密保护尚未确认；已确认的服务保存回执仍保留。'
      return false
    }
  }
  private prepared(value: ParentDraftRecoveryPrepared): ParentDraftRecoveryPrepared {
    if(!value||typeof value.leaseId!=='string'||!value.leaseId||!Number.isSafeInteger(value.localRevision)||value.localRevision<0||typeof value.hasRecovery!=='boolean'||value.protection!=='native_encrypted')throw Error('本机恢复回执不完整。')
    return value
  }
  private async prepareRecovery(): Promise<void> {
    if(this.recovery==='memory-only'||this.nativeLease)return
    if(!this.recovery)throw Error('当前客户端不支持加密草稿保护，尚未开放编辑。')
    const turn=this.authorityTurn,prepared=this.prepared(await this.recovery.prepare())
    if(!this.current(turn))throw Error('家长身份已变化，未接纳恢复权限。')
    this.nativeLease=prepared;this.nativeSequence=0
    if(prepared.hasRecovery){
      this.needsNativeRead=true;this.mode='suspended'
      this.recoveryMessage='这台电脑保留了未确认草稿。请明确恢复并核对，未读取原文或重发。'
      this.state={...this.state,phase:'error',message:this.recoveryMessage};this.emit()
      throw Error('请明确恢复这台电脑保留的草稿。')
    }
  }
  private validateSnapshot(snapshot: ParentDraftRecoverySnapshot): void {
    if(!snapshot||Object.keys(snapshot).sort().join(',')!=='acknowledged,childId,latest,pending,schema,scope'||snapshot.schema!==1||snapshot.childId!==this.childId||snapshot.scope!==this.scope)throw Error('恢复草稿不属于当前孩子或用途。')
    const finite=(value:unknown,depth=0):boolean=>depth<=64&&(value===null||typeof value==='string'||typeof value==='boolean'||typeof value==='number'&&Number.isFinite(value)||Array.isArray(value)&&value.every(x=>finite(x,depth+1))||typeof value==='object'&&Object.getPrototypeOf(value)===Object.prototype&&Object.values(value as object).every(x=>finite(x,depth+1)))
    if(!finite(snapshot)||new TextEncoder().encode(JSON.stringify(snapshot)).byteLength>4*1024*1024)throw Error('恢复草稿格式或容量无法核对。')
    if(snapshot.acknowledged){receipt({draft:snapshot.acknowledged},this.childId,this.scope);if(!validEditing(snapshot.acknowledged.payload))throw Error('已确认草稿超出当前恢复容量。')}
    for(const value of [snapshot.latest,snapshot.pending?.payload??null]){
      if(!validEditing(value)||value!==null&&(!value.page||typeof value.capture_ref!=='string'||typeof value.capture_version!=='string'||!value.review_drafts||!value.action_ids||this.scope==='language'&&value.objective_draft!=null))throw Error('恢复原文未通过当前草稿校验。')
    }
    const request=snapshot.pending
    if(request&&(!snapshot.acknowledged||Object.keys(request).sort().join(',')!=='client_action_id,expected_generation,expected_revision,payload'||request.expected_generation!==snapshot.acknowledged.storage_generation||request.expected_revision!==snapshot.acknowledged.revision||typeof request.client_action_id!=='string'||!request.client_action_id||request.client_action_id.length>160))throw Error('原保存请求无法精确核对，未建立新请求。')
  }
  private async reopenRecovery(turn:number): Promise<void> {
    if(this.recovery==='memory-only')return
    if(!this.recovery)throw Error('当前客户端不支持加密草稿恢复。')
    const prepared=this.prepared(await this.recovery.prepare())
    if(!this.current(turn,true))throw Error('身份再次变化，未读取恢复草稿。')
    let lease=prepared
    if(prepared.hasRecovery){
      const recovered=await this.recovery.read(prepared.localRevision)
      if(!this.current(turn,true))throw Error('身份再次变化，未接纳恢复原文。')
      lease=this.prepared(recovered);this.validateSnapshot(recovered.snapshot)
      // An extant Host may contain newer, explicitly unprotected RAM after disk failure.
      // Never replace that labour with an older capsule.
      if(this.acknowledged===null&&this.latest===null&&this.pending===null){
        this.acknowledged=structuredClone(recovered.snapshot.acknowledged)
        this.latest=structuredClone(recovered.snapshot.latest);this.pending=structuredClone(recovered.snapshot.pending)
        this.state={...this.state,payload:structuredClone(this.latest)}
      }
    }
    this.nativeLease=lease;this.nativeSequence=0;this.needsNativeRead=false;this.protectedSnapshot=null
    this.nativeRestored=true
    this.checkpoint()
  }
  async discardNativeRecovery(explicit:true):Promise<void> {
    if(explicit!==true||!this.needsNativeRead||!this.nativeLease||!this.recovery||this.recovery==='memory-only')throw Error('请先核对本机恢复草稿。')
    const revision=this.nativeLease.localRevision,turn=++this.authorityTurn
    this.authorityStamp=this.authorityVersion();this.mode='reconciling'
    try {
      const prepared=this.prepared(await this.recovery.discard(revision,true))
      if(!this.current(turn,true))throw Error('身份再次变化，未接纳放弃结果。')
      this.nativeLease=prepared;this.nativeSequence=0;this.needsNativeRead=false;this.protectedSnapshot=null
      this.nativeRestored=true
      this.acknowledged=null;this.latest=null;this.pending=null;this.running=null;this.loading=null
      this.state={phase:'loading',payload:null,staleRefs:[],message:'已明确放弃本机恢复稿，正在读取已保存版本。'}
      this.mode='active';await this.load();this.emit()
    }catch(error){if(this.current(turn,true)){this.mode='suspended';this.emit()};throw error}
  }
  get authoritySuspended(): boolean { return this.mode !== 'active' }
  suspend(revokeNative=true): void {
    this.checkpoint();this.authorityTurn++;this.mode='suspended';this.conflictPreview=null
    if(revokeNative)this.revokeRecovery()
    this.emit()
  }
  revokeRecovery(): void {
    if(this.recovery&&this.recovery!=='memory-only')try{this.recovery.revoke()}catch{/* lock still takes priority */}
    this.nativeLease=null
  }
  keepEditing(payload: TextbookEditingPayload | null): void {
    this.latest=structuredClone(payload); this.state={...this.state,payload:structuredClone(payload)}
    this.checkpoint()
  }
  private current(turn: number, reconciling=false): boolean {
    return turn===this.authorityTurn && this.authorityStamp===this.authorityVersion()
      && (this.mode==='active'||reconciling&&this.mode==='reconciling')
  }
  editingApi(): Api {
    const turn=this.authorityTurn
    return (path,init)=>this.requestActive(path,init,turn)
  }
  async requestActive(path: string, init?: RequestInit, turn=this.authorityTurn): Promise<unknown> {
    if(!this.current(turn))throw Error('家长身份已锁定，原草稿仍在本窗口。')
    const result=await this.api(path,init)
    if(!this.current(turn))throw Error('家长身份已变化，原请求仍待核对。')
    return result
  }
  private startDrain(): void {
    const turn=this.authorityTurn
    const running=Promise.resolve().then(()=>this.drain(turn)).finally(()=>{if(this.running===running)this.running=null})
    this.running=running
  }
  async reconcileAuthority(): Promise<void> {
    const turn=++this.authorityTurn
    this.authorityStamp=this.authorityVersion();this.mode='reconciling';this.conflictPreview=null
    try {
      await this.reopenRecovery(turn)
      if(!this.current(turn,true))throw Error('身份再次变化，尚未恢复草稿。')
      const actual=receipt(await this.api(this.path),this.childId,this.scope)
      if(!this.current(turn,true))throw Error('家长身份再次变化，尚未恢复草稿。')
      const previous=this.acknowledged, request=this.pending
      if(!previous){
        const retained=this.latest;this.accept(actual)
        this.latest=structuredClone(retained??actual.payload)
        const matches=canonical(this.latest)===canonical(actual.payload)
        this.state={...this.state,payload:structuredClone(this.latest),phase:matches?'saved':'rejected',message:matches?'草稿已从本机恢复。':'本机恢复的原文尚未送出，请核对后明确保存。'}
      }
      else {
        const committed=request && actual.storage_generation===request.expected_generation
          && actual.revision===request.expected_revision+1 && actual.last_expected_revision===request.expected_revision
          && actual.last_action_id===request.client_action_id && canonical(actual.payload)===canonical(request.payload)
        const unchanged=actual.storage_generation===previous.storage_generation&&actual.revision===previous.revision
          && canonical(actual.payload)===canonical(previous.payload)
        if(committed||unchanged){
          if(committed)this.accept(actual)
          else if(!request)this.acknowledged=structuredClone(actual)
          this.state={...this.state,payload:structuredClone(this.latest),staleRefs:actual.stale_refs,
            phase:this.pending?'error':canonical(this.latest)===canonical(actual.payload)?'saved':'rejected',
            message:this.pending?'原保存请求尚未确认提交；本窗口原文保留，请明确重试原请求。':canonical(this.latest)===canonical(actual.payload)?'草稿已核对恢复。':'本窗口较新的原文尚未保存；请核对后明确保存。'}
        } else {
          // A never-sent local draft also needs the existing explicit comparison.
          // Never replace a real unresolved action or borrow the new generation.
          this.pending??={expected_revision:previous.revision,expected_generation:previous.storage_generation,
            client_action_id:`textbook-draft-${crypto.randomUUID()}`,payload:structuredClone(this.latest)}
          this.state={...this.state,phase:'conflict',payload:structuredClone(this.latest),message:'储存版本或服务已变化；本窗口原文保留，请对照两份草稿，未重发旧请求。'}
        }
      }
      this.checkpoint();this.mode='active';this.running=null;this.loading=null;this.emit()
    } catch(error){if(this.current(turn,true)){this.mode='suspended';this.emit()};throw error}
  }
  constructor(private api: Api, private childId: string, private scope: 'capture' | 'language', private authorityVersion:()=>number=()=>0, private recovery:ParentDraftRecoveryConfiguration=undefined) {
    this.authorityStamp=authorityVersion()
    this.path = `/v1/parent/knowledge-graph/textbook-capture/${encodeURIComponent(childId)}/drafts/${scope}`
  }
  subscribe(listener: () => void): () => void { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  get storageGeneration(): string | null { return this.acknowledged?.storage_generation ?? null }
  hasUnconfirmedEditing(payload:TextbookEditingPayload|null):boolean {
    if(!this.dirty({...this.snapshot(),latest:payload}))return false
    if(this.recovery!=='memory-only'&&this.protectedSnapshot===canonicalCandidatePayload({...this.snapshot(),latest:payload}))return false
    return !this.acknowledged||!!this.pending||!!this.running||this.state.phase!=='saved'
      ||canonical(payload)!==canonical(this.acknowledged.payload)
  }
  private emit(): void { this.listeners.forEach(listener => listener()) }
  load(): Promise<DraftReceipt> {
    if(this.authoritySuspended) return Promise.reject(new Error('请重新解锁并核对原草稿。'))
    if (this.loading) return this.loading
    const loading=this.loadOnce().finally(() => { if(this.loading===loading)this.loading=null })
    this.loading=loading
    return loading
  }
  private async loadOnce(): Promise<DraftReceipt> {
    const turn=this.authorityTurn
    if (this.running || this.pending) throw new Error('尚有未核对请求，不能丢弃内存草稿。')
    this.state = { ...this.state, phase:'loading',message:'正在恢复本机草稿。' }; this.emit()
    try {
      await this.prepareRecovery()
      if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
      const result = receipt(await this.requestActive(this.path),this.childId,this.scope)
      if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
      this.acknowledged = structuredClone(result); this.latest = structuredClone(result.payload)
      this.checkpoint()
      this.state = {phase:'saved',payload:structuredClone(result.payload),staleRefs:result.stale_refs,
        message:result.stale_refs.length ? '草稿已恢复，但原页状态已变化；请核对原页后再提交。' : '草稿已从本机恢复。'}
      this.emit(); return result
    } catch (error) {
      if(this.current(turn)){
        if(this.recovery!=='memory-only')this.recoveryMessage='尚未确认这台电脑的加密草稿保护；请重试或使用支持此能力的客户端，未开放编辑。'
        this.state = {...this.state,phase:'error',message:'草稿无法恢复，原记录未覆盖。'}; this.emit()
      }; throw error
    }
  }
  update(payload: TextbookEditingPayload | null): void {
    if (!this.acknowledged) throw new Error('草稿尚未恢复，不能保存。')
    this.conflictPreview = null
    this.keepEditing(payload)
    if(this.authoritySuspended)return
    if (this.state.phase === 'conflict' || this.state.phase === 'error' || this.state.phase === 'rejected') { this.emit(); return }
    if (!validEditing(payload)) { this.invalidEditing(); return }
    this.state = {...this.state,phase:'saving',message:'正在保存草稿，尚未获得本机回执。'}; this.emit()
    if (!this.running) this.startDrain()
  }
  private accept(result: DraftReceipt): void {
    this.acknowledged = structuredClone(result); this.pending = null
    this.state = {...this.state,staleRefs:result.stale_refs}
    this.checkpoint()
  }
  private invalidEditing(): void {
    this.state = {...this.state,phase:'invalid',message:'编辑超过草稿保存范围或结构无法核对，仍留在本窗口，尚未保存；已保存版本未覆盖。单个原始文本上限为十万字，整份草稿上限为 1MB。'}
    this.emit()
  }
  private async drain(turn=this.authorityTurn): Promise<void> {
    if(!this.current(turn))return
    while (this.acknowledged && (this.pending || canonical(this.latest) !== canonical(this.acknowledged.payload))) {
      if(!this.current(turn))return
      // Finish an already-sent valid request first; never submit unfinished invalid editing.
      if (!this.pending && !validEditing(this.latest)) { this.invalidEditing(); return }
      this.pending ??= {expected_revision:this.acknowledged.revision,expected_generation:this.acknowledged.storage_generation,client_action_id:`textbook-draft-${crypto.randomUUID()}`,payload:structuredClone(this.latest)}
      const request = this.pending
      if(!this.checkpoint()){
        if(!this.current(turn))return
        this.state={...this.state,phase:'error',message:'原保存尚未发出；请核对本机保护状态后明确重试。'};this.emit();return
      }
      if(!this.current(turn))return
      const matches = (result: DraftReceipt): boolean => result.revision === request.expected_revision + 1
        && result.storage_generation === request.expected_generation
        && result.last_expected_revision === request.expected_revision && result.last_action_id === request.client_action_id
        && canonical(result.payload) === canonical(request.payload)
      try {
        const result = receipt(await this.requestActive(this.path,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)}),this.childId,this.scope)
        if(!this.current(turn))return
        if (!matches(result)) throw new Error('草稿回执不属于本次保存。')
        this.accept(result)
      } catch (error) {
        if(!this.current(turn))return
        // A transport error is not proof of failure. Read once, never re-submit automatically.
        let actual: DraftReceipt | null = null
        try { actual = receipt(await this.requestActive(this.path),this.childId,this.scope) } catch { /* preserve the exact pending request */ }
        if(!this.current(turn))return
        if (actual && matches(actual)) { this.accept(actual); continue }
        const conflict = actual && (actual.revision !== request.expected_revision || actual.storage_generation !== request.expected_generation)
        // Only an explicit product rejection plus unchanged durable authority proves
        // this request did not commit. Transport failures retain the exact pending action.
        if (!conflict && actual && error instanceof ApiError && error.code === 'TEXTBOOK_DRAFT_CONFLICT'
          && (error.status === 409 || error.status === 422)) {
          this.accept(actual)
          this.state = {...this.state,phase:'rejected',message:'本次保存被拒，已保存版本未覆盖。文字仍在本窗口；请刷新并核对原页或修正输入，再保存。'}
          this.emit(); return
        }
        this.state = {...this.state,phase:conflict?'conflict':'error',
          message:conflict?'另一个窗口或删除操作已改变草稿；本窗口未覆盖新版本。':'草稿未获保存回执，编辑仍在本窗口；请恢复连接后重试。'}
        this.emit(); return
      }
    }
    if(!this.current(turn))return
    this.state = {...this.state,phase:'saved',message:this.state.staleRefs.length
      ? '草稿已保存；原页状态已变化，请核对原回执。' : '草稿已保存到本机。'}; this.emit()
  }
  async flush(): Promise<void> {
    const turn=this.authorityTurn
    if(this.authoritySuspended)throw Error('请重新解锁并核对原草稿。')
    await this.running
    if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
    if(this.authoritySuspended)throw Error('请重新解锁并核对原草稿。')
    if (!this.acknowledged || this.pending || canonical(this.latest) !== canonical(this.acknowledged.payload)
      || this.state.phase === 'invalid' || this.state.phase === 'rejected' || this.state.phase === 'error' || this.state.phase === 'conflict') throw new Error(this.state.message)
  }
  async retry(): Promise<void> {
    const turn=this.authorityTurn
    if(this.authoritySuspended)throw Error('请重新解锁并核对原草稿。')
    await this.running
    if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
    if (this.state.phase === 'conflict') throw new Error(this.state.message)
    if (!this.acknowledged) { await this.load(); return }
    this.state = {...this.state,phase:'saving',message:'正在核对原保存请求。'}; this.emit()
    this.startDrain()
    await this.flush()
  }
  async inspectConflict(): Promise<TextbookDraftComparison> {
    const turn=this.authorityTurn
    await this.running
    if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
    if (this.state.phase !== 'conflict' || !this.pending) throw new Error('尚未确认版本冲突，不能放弃未知保存请求。')
    const request = this.pending, local = structuredClone(this.latest)
    this.conflictPreview = null
    const result = receipt(await this.requestActive(this.path), this.childId, this.scope)
    if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
    if (this.state.phase !== 'conflict' || request !== this.pending || canonical(local) !== canonical(this.latest)
      || (result.storage_generation === request.expected_generation && result.revision <= request.expected_revision)) {
      throw new Error('草稿状态已变化，请重新核对；本窗口输入仍保留。')
    }
    this.conflictPreview = {receipt: structuredClone(result), local}
    return {local: structuredClone(local), saved: structuredClone(result.payload),
      generationChanged: result.storage_generation !== request.expected_generation}
  }
  async adoptSavedConflict(): Promise<TextbookEditingPayload | null> {
    const turn=this.authorityTurn
    await this.running
    if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
    const preview = this.conflictPreview, request = this.pending
    if (this.state.phase !== 'conflict' || !preview || !request || canonical(preview.local) !== canonical(this.latest)) {
      throw new Error('请先核对两份草稿；尚未放弃本窗口输入。')
    }
    const actual = receipt(await this.requestActive(this.path), this.childId, this.scope)
    if(!this.current(turn))throw Error('家长身份已变化，原草稿仍待核对。')
    // An explicit choice is bound to the exact versions shown, never to a later GET.
    if (this.state.phase !== 'conflict' || preview !== this.conflictPreview || request !== this.pending
      || canonical(preview.local) !== canonical(this.latest)
      || canonicalCandidatePayload(actual) !== canonicalCandidatePayload(preview.receipt)) {
      this.conflictPreview = null
      throw new Error('已保存版本或本窗口输入已变化，请重新核对；没有覆盖任何版本。')
    }
    this.accept(actual); this.latest = structuredClone(actual.payload); this.conflictPreview = null;this.checkpoint()
    this.state = {phase:'saved',payload:structuredClone(actual.payload),staleRefs:actual.stale_refs,
      message:actual.stale_refs.length ? '已采用核对过的本机草稿；原页状态已变化，请核对原页。' : '已采用核对过的本机草稿，没有重新提交或覆盖它。'}
    this.emit(); return structuredClone(actual.payload)
  }
}
