import type { QwenRealtimeInputCapture } from './qwenRealtimeVoice'
import { parseQwenNativeProposal, qwenRuntimeTools,isQwenNativeLocalSafetyControl, isQwenCurriculumTutorDispatch, qwenNativeFormatRejection, qwenNativeFormatCorrectionExhausted, type QwenCanonicalAdmission,
  type QwenNativeIdentity, type QwenNativeBoundRuntime, type QwenNativePersistedInput, type QwenNativeToolReceipt } from './qwenNativeProtocol'

type Input = { itemId: string; sourceKind: 'live' | 'persisted_native'|'household_local_transcript'; capture: QwenRealtimeInputCapture | null;
  restoredCurrent?: () => boolean; originalSourceKind?:'household_local_transcript'; utterance?: string;safetyBlocked?:boolean;
  admission?: Promise<QwenCanonicalAdmission>; ready: Promise<QwenCanonicalAdmission | null>;
  finishReady: (value: QwenCanonicalAdmission | null) => void;
  previousActionId?: string; tail: Promise<void>; unresolved: boolean; formatRejections?: number; formatCreatedAt: number }
type Response = { input: Input; generationDone: boolean; retired: boolean; text: string }
type Call = { fingerprint: string; identity: QwenNativeIdentity & { call_id: string };
  result: Promise<QwenNativeToolReceipt>; settled: boolean; unresolved: boolean }
const id = (value: unknown): value is string => typeof value === 'string' && value.length > 0
  && value.length <= 160 && /^[\x21-\x7e]+$/.test(value)
const denied = (reason: string): QwenNativeToolReceipt => ({ version: 1, proposal_status: 'conflict',
  execution: { status: 'failed', location: 'server' }, public_result: { status: reason } })

const FORMAT_BUDGET_KEY = 'hsc-qwen-tool-format-budget-v1'
const FORMAT_RECOVERY_WINDOW_MS = 10 * 60_000
const FORMAT_BUDGET_TTL_MS = 30 * 60_000
const FORMAT_BUDGET_CAPACITY = 256
/** Only immutable canonical voice IDs and saturating counters survive transport
 * replacement. No source text, arguments, execution result, token or lease. */
function formatBudget(voiceId:string,createdAt:number,minimum:number,increment=false):number|null {
  try {
    const storage=globalThis.localStorage,now=Date.now()
    if(!storage||!id(voiceId))return null
    const raw=storage.getItem(FORMAT_BUDGET_KEY)
    if(raw!==null&&raw.length>100_000)return null
    const saved=raw===null?{version:1,entries:[]}:JSON.parse(raw)
    if(!saved||Object.keys(saved).sort().join(',')!=='entries,version'||saved.version!==1
      ||!Array.isArray(saved.entries)||saved.entries.length>FORMAT_BUDGET_CAPACITY)return null
    const unique=new Set<string>()
    for(const item of saved.entries){
      if(!item||Object.keys(item).sort().join(',')!=='count,expires_at,voice_input_id'||!id(item.voice_input_id)
        ||unique.has(item.voice_input_id)||![1,2].includes(item.count)||!Number.isFinite(item.expires_at)
        ||item.expires_at<=0||item.expires_at>now+FORMAT_BUDGET_TTL_MS+60_000)return null
      unique.add(item.voice_input_id)
    }
    const entries=saved.entries.filter((item:{expires_at:number})=>item.expires_at>now) as Array<{voice_input_id:string;count:number;expires_at:number}>
    let item=entries.find(row=>row.voice_input_id===voiceId)
    let count=Math.max(minimum,item?.count??0)
    if(increment){
      // Expiring metadata cannot renew an old input's correction allowance.
      if(!Number.isFinite(createdAt)||createdAt>now+60_000||now-createdAt>FORMAT_RECOVERY_WINDOW_MS)return null
      if(!item&&entries.length>=FORMAT_BUDGET_CAPACITY)return null
      count=Math.min(2,count+1)
      if(item)item.count=count
      else{item={voice_input_id:voiceId,count,expires_at:now+FORMAT_BUDGET_TTL_MS};entries.push(item)}
    }
    if(increment||entries.length!==saved.entries.length){
      const text=JSON.stringify({version:1,entries})
      storage.setItem(FORMAT_BUDGET_KEY,text)
      if(storage.getItem(FORMAT_BUDGET_KEY)!==text)return null
    }
    return count
  } catch { return null } // Storage problems disable format recovery, not valid tools.
}

/** Input ownership is explicit. This ledger never selects a latest/FIFO transcript. */
export class QwenNativeConversation {
  private readonly inputs = new Map<string, Input>()
  private readonly responses = new Map<string, Response>()
  private readonly restoredVoices = new Set<string>()
  private readonly calls = new Map<string, Call>()
  private readonly formatRejectedCallIds = new Set<string>()
  private closed = false
  constructor(private readonly supplierSessionId: string, private readonly runtime: QwenNativeBoundRuntime,
    private readonly current: (capture: QwenRealtimeInputCapture) => boolean, private readonly hostConnectionId: string) {
    if (!id(supplierSessionId) || !id(hostConnectionId)) throw Error('QWEN_NATIVE_SESSION_ID_UNCONFIRMED')
    qwenRuntimeTools(runtime.catalog)
  }
  startInput(itemId: string, capture: QwenRealtimeInputCapture): boolean {
    if (this.closed || !id(itemId) || !capture || capture.source !== 'accepted_onset'
      || !this.current(capture) || this.inputs.has(itemId) || this.inputs.size >= 256) return false
    let finishReady!: Input['finishReady']
    const ready = new Promise<QwenCanonicalAdmission | null>(resolve => { finishReady = resolve })
    this.inputs.set(itemId, { itemId, sourceKind:'live', capture: structuredClone(capture), ready, finishReady,
      tail: Promise.resolve(), unresolved: false, formatCreatedAt:capture.capturedAt })
    return true
  }
  private inputCurrent(input: Input): boolean {
    return !this.closed && !input.safetyBlocked && (input.sourceKind === 'live' && input.capture
      ? this.current(input.capture) : input.sourceKind !== 'live' && input.restoredCurrent?.() === true)
  }
  /** Restore only a canonical unprocessed native original from fresh host-only
   * context. No live onset, new timestamp, ASR grant or admission is fabricated. */
  restoreInput(itemId: string, original: QwenNativePersistedInput, currentOwner: () => boolean): boolean {
    if (this.closed || !currentOwner() || !id(itemId) || !id(original.voice_input_id)
      || !id(original.client_action_id) || typeof original.created_at !== 'string' || original.created_at.length > 100
      || !Number.isFinite(Date.parse(original.created_at)) || typeof original.text !== 'string'
      || !original.text.trim() || original.text.length > 12000 || this.inputs.has(itemId)
      || this.restoredVoices.has(original.voice_input_id) || this.inputs.size >= 256) return false
    const receipt = { voice_input_id: original.voice_input_id }
    this.restoredVoices.add(original.voice_input_id)
    this.inputs.set(itemId, { itemId, sourceKind: 'persisted_native', capture: null, restoredCurrent: currentOwner,originalSourceKind:original.source_kind,
      utterance: original.text, ready: Promise.resolve(receipt), finishReady: () => undefined,
      tail: Promise.resolve(), unresolved: false, formatCreatedAt:Date.parse(original.created_at) })
    return true
  }
  /** A server-witnessed household transcript is text provenance, not cloud ASR,
   * a recovered native original or a new live audio onset. */
  startHouseholdInput(itemId:string,voiceInputId:string,text:string,currentOwner:()=>boolean,createdAt=0):boolean{
    if(this.closed||!currentOwner()||!id(itemId)||!id(voiceInputId)||!text.trim()||text.length>16000
      ||this.inputs.has(itemId)||this.restoredVoices.has(voiceInputId)||this.inputs.size>=256)return false
    this.restoredVoices.add(voiceInputId)
    const receipt={voice_input_id:voiceInputId}
    this.inputs.set(itemId,{itemId,sourceKind:'household_local_transcript',originalSourceKind:'household_local_transcript',capture:null,restoredCurrent:currentOwner,
      utterance:text,ready:Promise.resolve(receipt),finishReady:()=>undefined,tail:Promise.resolve(),unresolved:false,formatCreatedAt:createdAt})
    return true
  }
  completeInput(itemId: string, utterance: string): Promise<QwenCanonicalAdmission> | null {
    const input = this.inputs.get(itemId)
    if (this.closed || !input?.capture || !this.inputCurrent(input) || typeof utterance !== 'string'
      || !utterance.trim() || utterance.length > 16000) return null
    const original = utterance.trim()
    if (input.admission) return input.utterance === original ? input.admission : null
    input.utterance = original
    const captured=input.capture
    input.admission = Promise.resolve().then(() => this.runtime.admit(original, structuredClone(captured), this.hostConnectionId))
      .then(receipt => {
        if (this.closed || !this.inputCurrent(input) || !id(receipt?.voice_input_id)) {
          throw Error('QWEN_NATIVE_INPUT_ADMISSION_UNCONFIRMED')
        }
        if(receipt.local_control!==undefined){
          if(!isQwenNativeLocalSafetyControl(receipt.local_control))throw Error('QWEN_NATIVE_SAFETY_RECEIPT_INVALID')
          input.safetyBlocked=true
          return {voice_input_id:receipt.voice_input_id,local_control:structuredClone(receipt.local_control)}
        }
        return { voice_input_id: receipt.voice_input_id,
          ...(isQwenCurriculumTutorDispatch(receipt.curriculum_tutor) ? { curriculum_tutor: structuredClone(receipt.curriculum_tutor) } : {}) }
      })
    // Consumers still receive the rejection; avoid an unhandled event callback rejection.
    void input.admission.then(input.finishReady, () => input.finishReady(null))
    return input.admission
  }
  /** Must be backed by a provider reference or an explicit host request owner. */
  bindResponse(responseId: string, inputItemId: string): boolean {
    const input = this.inputs.get(inputItemId)
    if (this.closed || !id(responseId) || !input || !this.inputCurrent(input)
      || this.responses.has(responseId) || this.responses.size >= 512) return false
    this.responses.set(responseId, { input, generationDone: false, retired: false, text: '' })
    return true
  }
  hasResponse(responseId: string): boolean {
    const response = this.responses.get(responseId)
    return this.responseOwned(responseId) && !response!.generationDone
  }
  ownsResponse(responseId: string): boolean { return this.responseOwned(responseId) }
  private responseOwned(responseId: string): boolean {
    const response = this.responses.get(responseId)
    return !this.closed && !!response && !response.retired && this.inputCurrent(response.input)
  }
  private async identity(responseId: string): Promise<QwenNativeIdentity | null> {
    const response = this.responses.get(responseId), input = response?.input
    if (this.closed || !response || !input || !this.responseOwned(responseId)) return null
    let timer: ReturnType<typeof setTimeout> | undefined
    const admission = await Promise.race([input.ready, new Promise<null>(resolve => {
      timer = setTimeout(() => resolve(null), 15000)
    })]).finally(() => { if (timer !== undefined) clearTimeout(timer) })
    if (!admission) return null
    if (this.closed || !this.inputCurrent(input)) return null
    return { version: 1, host_connection_id: this.hostConnectionId,
      ...(input.sourceKind !== 'live' ? { input_source: input.sourceKind } : {}),
      supplier_session_id: this.supplierSessionId, input_item_id: input.itemId,
      response_id: responseId, voice_input_id: admission.voice_input_id }
  }
  private exhaustedFormatRejection(): QwenNativeToolReceipt {
    return {...denied('tool_format_recovery_exhausted'), not_admitted: true, format_correction: 'exhausted'}
  }
  private formatRejection(input: Input, receipt: QwenNativeToolReceipt, voiceId:string): QwenNativeToolReceipt {
    if (!qwenNativeFormatRejection(receipt) || !this.inputCurrent(input)) return receipt
    const previous=input.formatRejections??0
    input.formatRejections=Math.min(2,previous+1)
    const persisted=formatBudget(voiceId,input.formatCreatedAt,previous,true)
    if(persisted===null){input.formatRejections=2;return {...receipt,format_correction:'exhausted'}}
    input.formatRejections=Math.max(input.formatRejections,persisted)
    return {...receipt, format_correction: input.formatRejections === 1 ? 'retry_once' : 'exhausted'}
  }
  async propose(responseId: string, callId: string, toolName: string, rawArguments: string): Promise<QwenNativeToolReceipt> {
    const response = this.responses.get(responseId)
    const callKey = `${responseId}:${callId}`
    if (!this.responseOwned(responseId) || !response || !id(callId)
      || response.generationDone && !this.calls.has(callKey)) return denied('source_unconfirmed')
    // A correction must use a fresh call identity. A prior accepted/uncertain
    // effect can never be relabelled as an unadmitted formatting failure.
    if (this.formatRejectedCallIds.has(callId)) return denied('call_identity_conflict')
    for (const prior of this.calls.values()) {
      if (prior.identity.call_id === callId && prior.identity.response_id !== responseId) return denied('call_identity_conflict')
    }
    const parsed = parseQwenNativeProposal(this.runtime.catalog, toolName, rawArguments)
    if (!parsed) {
      if (this.calls.has(callKey)) return denied('call_identity_conflict')
      if (this.calls.size + this.formatRejectedCallIds.size >= 1024) return denied('call_capacity_reached')
      this.formatRejectedCallIds.add(callId)
      const input = response.input
      // Wait for the original input's prior effect; an uncertain result cannot
      // be converted into a new automatic retry by malformed later parameters.
      const work = input.tail.then(async () => {
        if (!this.responseOwned(responseId) || !this.inputCurrent(input)) return denied('source_unconfirmed')
        if (input.unresolved) return denied('previous_execution_unconfirmed')
        const identity=await this.identity(responseId)
        if(!identity||!this.responseOwned(responseId)||!this.inputCurrent(input))return denied('source_unconfirmed')
        input.formatRejections=Math.max(input.formatRejections??0,formatBudget(identity.voice_input_id,input.formatCreatedAt,0)??0)
        if ((input.formatRejections ?? 0) >= 2) return this.exhaustedFormatRejection()
        return this.formatRejection(input, {...denied('tool_arguments_invalid'), not_admitted: true},identity.voice_input_id)
      })
      input.tail = work.then(() => undefined)
      const result = await work
      return this.inputCurrent(input) && this.responseOwned(responseId) ? result : denied('source_unconfirmed')
    }
    const identity = await this.identity(responseId)
    // Provider generation may finish before admission. That does not revoke
    // a tool which was issued while its response/source was still current.
    if (!identity || !this.responseOwned(responseId)) return denied('source_unconfirmed')
    const fingerprint = JSON.stringify({ toolName, rawArguments })
    const existing = this.calls.get(callKey)
    if (existing) {
      if (existing.fingerprint !== fingerprint) return denied('call_identity_conflict')
      if (!existing.settled || !existing.unresolved) return existing.result
      // An uncertain side effect is never submitted a second time.
      try {
        const receipt = await this.runtime.readReceipt(existing.identity)
        if (this.closed || !this.inputCurrent(response.input)) return denied('source_unconfirmed')
        existing.unresolved = ['pending', 'unknown'].includes(receipt.execution.status)
        existing.result = Promise.resolve(receipt)
        response.input.unresolved = existing.unresolved
        if (receipt.action_id) response.input.previousActionId = receipt.action_id
        return receipt
      } catch { return { version: 1, proposal_status: 'replayed', execution: { status: 'unknown', location: 'server' }, public_result: { status: 'unknown' } } }
    }
    if (this.calls.size + this.formatRejectedCallIds.size >= 1024) return denied('call_capacity_reached')
    const input = response.input
    const work = input.tail.then(async () => {
      if (!this.responseOwned(responseId) || !this.inputCurrent(input)) return denied('source_unconfirmed')
      if (input.unresolved) return denied('previous_execution_unconfirmed')
      input.formatRejections=Math.max(input.formatRejections??0,formatBudget(identity.voice_input_id,input.formatCreatedAt,0)??0)
      if ((input.formatRejections ?? 0) >= 2) return this.exhaustedFormatRejection()
      input.unresolved = true
      try {
        const result = await this.runtime.propose({ ...identity, call_id: callId, ...parsed,
          ...(input.previousActionId ? { previous_action_id: input.previousActionId } : {}) })
        input.unresolved = ['pending', 'unknown'].includes(result.execution.status)
        if (result.action_id) input.previousActionId = result.action_id
        return this.formatRejection(input, result,identity.voice_input_id)
      } catch {
        return { version: 1, proposal_status: 'admitted', execution: { status: 'unknown', location: 'server' },
          public_result: { status: 'unknown' } } as QwenNativeToolReceipt
      }
    })
    const call: Call = { fingerprint, identity: { ...identity, call_id: callId }, result: work, settled: false, unresolved: true }
    this.calls.set(callKey, call)
    input.tail = work.then(result => { call.settled = true; call.unresolved = ['pending', 'unknown'].includes(result.execution.status) }, () => undefined)
    const result = await work
    return this.closed || !this.inputCurrent(input) ? denied('source_unconfirmed') : result
  }
  appendDialogue(responseId: string, delta: string): boolean {
    const response = this.responses.get(responseId)
    if (!response || !this.hasResponse(responseId) || typeof delta !== 'string'
      || response.text.length + delta.length > 12000) return false
    response.text += delta
    return true
  }
  replaceDialogue(responseId: string, text: string): boolean {
    const response = this.responses.get(responseId)
    if (!response || !this.hasResponse(responseId) || typeof text !== 'string' || text.length > 12000) return false
    response.text = text
    return true
  }
  async finishDialogue(responseId: string, terminal: 'completed' | 'interrupted' | 'failed'): Promise<boolean> {
    const response = this.responses.get(responseId)
    if (!response || !this.hasResponse(responseId)) return false
    response.generationDone = true
    if (!response.text.trim()) return true
    const identity = await this.identity(responseId)
    if (!identity || this.closed || !this.inputCurrent(response.input)) return false
    await this.runtime.recordDialogue({ ...identity, text: response.text, status: 'unverified', terminal })
    return true
  }
  close(): void {
    this.closed = true
    for (const input of this.inputs.values()) input.finishReady(null)
    this.responses.clear(); this.inputs.clear(); this.calls.clear(); this.formatRejectedCallIds.clear()
  }
  /** Interruption/owner changes retire source permission; generation EOS does not. */
  retireResponse(responseId: string): void {
    const response = this.responses.get(responseId)
    if (response) response.retired = true
  }
}

/** No AgentTurn, local result body, identity, score or diagnostic is sent to Qwen. */
export function qwenNativePublicToolOutput(receipt: QwenNativeToolReceipt): string {
  const correction = (qwenNativeFormatRejection(receipt) || qwenNativeFormatCorrectionExhausted(receipt)) && receipt.format_correction ? {
    state: receipt.format_correction, retry_allowed: receipt.format_correction === 'retry_once',
    instruction: receipt.format_correction === 'retry_once'
      ? '本次参数格式被明确拒绝，没有执行。基于原请求换新call_id自行纠正最多一次，不要求孩子重复。生成完整严格JSON，顶层为args且只含对应schema字段；普通控制没有目标变更时省略objective。不得重试已接纳或状态未知的操作。'
      : '本输入的自动格式纠错次数已用完，不再调用工具重试。这项被拒的操作没有执行，原请求已保留；其他操作仅按各自真实回执说明。'
  } : undefined
  return JSON.stringify({ version: 1, proposal_status: receipt.proposal_status,
    execution_status: receipt.execution.status, ...(receipt.not_admitted === true ? { not_admitted: true } : {}),
    public_result: {...receipt.public_result, ...(correction ? {argument_recovery: correction} : {})} })
}
