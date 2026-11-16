import { AdaptiveVoiceActivityDetector } from './voiceActivityDetector'
import type { QwenRealtimeInputCapture } from './qwenRealtimeVoice'
import type {QwenCanonicalAdmission,QwenCurriculumTutorDispatch,QwenNativeLocalSafetyControl} from './qwenNativeProtocol'
import { qwenNativeDiagnosticInstructions } from './qwenNativeProtocol'
import type {NativeActivationSegment} from './nativeActivationAudio'
import type { QwenNativeConversation } from './qwenNativeConversation'

type Segment = { capture: QwenRealtimeInputCapture; packets: Uint8Array[]; samples: number; sending: boolean;
  itemId: string | null; duration: number; quietMs: number; inputEndAt:number; admitted: boolean; superseded: boolean; interruptedAtOnset: boolean;
  explicit?: boolean; voiced?: boolean }
type Request = { itemId: string; superseded: boolean; inputEndAt:number; createdAt?:number; continuation?:boolean;
  diagnosticCapture?: QwenRealtimeInputCapture }
type Options = {
  canAppend: () => boolean
  canRespond: () => boolean
  current: () => boolean
  capture: () => QwenRealtimeInputCapture | undefined
  ledger: () => QwenNativeConversation | null
  send: (event: Record<string, unknown>) => void
  speechStarted: () => void
  replyAudioScheduled: () => boolean
  interruptReply: (reason: 'speech_onset' | 'confirmed_input') => void
  inputCompleted: (text: string, capture: QwenRealtimeInputCapture) => void
  onWorkspaceInput?: (text: string, capture: QwenRealtimeInputCapture, voiceInputId: string) => boolean
  onCurriculumTutor?: (text: string, capture: QwenRealtimeInputCapture, dispatch: QwenCurriculumTutorDispatch) => boolean
  currentDiagnosticAnswerTool?: (capture: QwenRealtimeInputCapture) => string | undefined
  responseBound: (responseId: string, allowed: boolean) => void
  failed: (message: string) => void
  status: (phase: 'listening' | 'transcribing', speechActive: boolean, pending: number) => void
  inputDiscarded?: () => void
  explicitCaptureLimit?: () => void
  explicitCaptureDiscarded?: () => void
  localSafety?:(control:QwenNativeLocalSafetyControl)=>void
}
const identity = (value: unknown): value is string => typeof value === 'string'
  && /^[\x21-\x7e]{1,160}$/.test(value)
const base64 = (bytes: Uint8Array): string => {
  let raw = ''
  for (let i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i])
  return btoa(raw)
}

/** Local endpointing or explicit capture, with raw audio semantics in Qwen.
 * One explicit commit and one explicit response request may be outstanding.
 * No provider field or current/latest transcript is invented to bind tools. */
export class QwenManualAudio {
  private detector = new AdaptiveVoiceActivityDetector()
  private preRoll: Float32Array[] = []
  private preRollSamples = 0
  private rate = 0
  private resampleAt = 0
  private recording: Segment | null = null
  private queued: Segment[] = []
  private committed: Segment | null = null
  private expectedResponse: Request | null = null
  private continuation: Request | null = null
  private activeResponse: string | null = null
  private responseInputs = new Map<string, string>()
  private responseTiming = new Map<string,Request>()
  private seenResponses = new Set<string>()
  private seenInputs = new Set<string>()
  private disposed = false
  private epoch = 0
  private inputTimer: ReturnType<typeof setTimeout> | null = null
  private responseTimer: ReturnType<typeof setTimeout> | null = null
  constructor(private readonly options: Options) {}
  private allowed(): boolean { return !this.disposed && this.options.current() && this.options.canAppend() }
  private clearPreRoll(): void {
    for (const frame of this.preRoll) frame.fill(0)
    this.preRoll = []; this.preRollSamples = 0; this.resampleAt = 0; this.detector.reset()
  }
  private clearPackets(segment: Segment | null): void {
    if (segment) { for (const packet of segment.packets) packet.fill(0); segment.packets = [] }
  }
  gateChanged(): void {
    if (this.allowed()) { this.pump(); return }
    const partial = this.recording?.sending === true
    const explicit = this.recording?.explicit === true
    const discarded = this.recording !== null || this.queued.length > 0
    this.clearPackets(this.recording); this.recording = null
    for (const queued of this.queued) this.clearPackets(queued)
    this.queued = []; this.clearPreRoll()
    // Clear only the uncommitted supplier buffer. A submitted input keeps its
    // canonical admission; no interrupted audio is replayed when the gate opens.
    if (partial && this.options.current()) this.safeSend({ type: 'input_audio_buffer.clear' })
    if (discarded) { this.options.inputDiscarded?.(); this.options.status('listening', false, this.pendingCount()) }
    if (explicit) this.options.explicitCaptureDiscarded?.()
  }
  private safeSend(event: Record<string, unknown>, audio = false): boolean {
    if (this.disposed || !this.options.current() || audio && !this.allowed()) return false
    try { this.options.send(event); return true } catch { this.fail('实时语音数据通道未完成发送，刚才未确认的内容需要重说。'); return false }
  }
  private encode(frame: Float32Array, rate: number): Uint8Array {
    const samples: number[] = [], step = rate / 16000
    let at = this.resampleAt
    for (; at < frame.length; at += step) samples.push(Math.max(-1, Math.min(1, frame[Math.floor(at)])))
    this.resampleAt = at - frame.length
    const bytes = new Uint8Array(samples.length * 2), view = new DataView(bytes.buffer)
    for (let i = 0; i < samples.length; i++) view.setInt16(i * 2, Math.round(samples[i] * (samples[i] < 0 ? 32768 : 32767)), true)
    return bytes
  }
  private appendPacket(segment: Segment, packet: Uint8Array): void {
    segment.samples += packet.length / 2
    if (segment.sending) {
      if (!this.safeSend({ type: 'input_audio_buffer.append', audio: base64(packet) }, true)) segment.sending = false
      packet.fill(0)
    } else segment.packets.push(packet)
  }
  private interruptReply(reason: 'speech_onset' | 'confirmed_input', preserveCommitted = false): void {
    if (this.expectedResponse) this.expectedResponse.superseded = true
    if (this.continuation) this.continuation.superseded = true
    if (reason === 'speech_onset' && this.committed && !preserveCommitted) this.committed.superseded = true
    this.activeResponse = null
    this.options.interruptReply(reason)
  }
  private discardRecording(): void {
    const recording = this.recording
    this.recording = null
    this.clearPackets(recording)
    this.clearPreRoll()
    if (recording?.sending && this.options.current()) this.safeSend({ type: 'input_audio_buffer.clear' })
  }
  discardAutomaticCapture(): void {
    if (!this.recording?.explicit) this.discardRecording()
    this.queued = this.queued.filter(segment => {
      if (segment.explicit) return true
      this.clearPackets(segment)
      return false
    })
  }
  interruptPlayingReply(): void {
    if (this.options.replyAudioScheduled()) this.interruptReply('speech_onset', true)
  }
  beginExplicitTurn(): boolean {
    if (!this.allowed() || this.recording?.explicit) return false
    this.discardAutomaticCapture()
    const capture = this.options.capture()
    if (!capture || capture.source !== 'accepted_onset' || capture.reference.local_recovery_only === true) return false
    this.recording = { capture: structuredClone(capture), packets: [], samples: 0, sending: false,
      itemId: null, duration: 0, quietMs: 0, inputEndAt: performance.now(), admitted: false, superseded: false,
      interruptedAtOnset: false, explicit: true, voiced: false }
    this.options.speechStarted()
    this.options.status('listening', true, this.pendingCount())
    return true
  }
  finishExplicitTurn(): boolean {
    const segment = this.recording
    if (!segment?.explicit) return false
    if (!this.allowed() || !segment.voiced || segment.samples < 1600) {
      this.cancelExplicitTurn()
      return false
    }
    segment.inputEndAt = performance.now()
    this.finishRecording(segment)
    return true
  }
  cancelExplicitTurn(): void {
    if (!this.recording?.explicit) return
    this.discardRecording()
    this.options.status('listening', false, this.pendingCount())
    this.pump()
  }
  processFrame(frame: Float32Array, rate: number, mode: 'auto' | 'explicit' = 'auto'): void {
    if (this.disposed || !frame.length || frame.length > 8192 || !Number.isFinite(rate) || rate < 8000 || rate > 96000) return
    if (!this.allowed()) { this.gateChanged(); return }
    if (mode === 'explicit' ? !this.recording?.explicit : this.recording?.explicit) return
    if (this.rate && this.rate !== rate) { this.fail('麦克风采样格式发生变化，正在重新建立语音连接。'); return }
    this.rate = rate
    const duration = frame.length / rate * 1000
    let energy = 0
    for (const sample of frame) energy += sample * sample
    const activity = this.detector.observe(Math.sqrt(energy / frame.length), duration)
    if (mode === 'explicit') {
      const segment = this.recording!
      segment.duration += duration
      if (segment.duration >= (segment.sending ? 120_000 : 30_000)) {
        this.cancelExplicitTurn()
        this.options.explicitCaptureLimit?.()
        return
      }
      segment.voiced ||= activity.voiced
      this.appendPacket(segment, this.encode(frame, rate))
      if (!segment.sending && segment.voiced) this.pump()
      return
    }
    if (!this.recording && activity.speechDetected && activity.voiced) {
      const capture = this.options.capture()
      if (!capture || capture.source !== 'accepted_onset') { this.clearPreRoll(); return }
      if (capture.reference.local_recovery_only === true) {
        this.clearPreRoll(); this.fail('这段采集来源仅限本机处理，没有交给云语音。请重新开始一段新的发言。'); return
      }
      this.options.speechStarted()
      // Before reply audio, a microphone onset is provisional. Preserve the
      // preceding turn if this candidate yields only noise or empty ASR.
      const interruptedAtOnset = this.options.replyAudioScheduled()
      if (interruptedAtOnset) this.interruptReply('speech_onset')
      if (!this.allowed()) return
      // An automatic onset may be ambient audio or yield to an explicit turn.
      // Keep its PCM local until endpointing finalizes it.
      const segment: Segment = { capture: structuredClone(capture), packets: [], samples: 0, sending: false,
        itemId: null, duration: 0, quietMs: 0, inputEndAt:performance.now(), admitted: false, superseded: false, interruptedAtOnset }
      this.recording = segment; this.resampleAt = 0
      for (const previous of this.preRoll) { this.appendPacket(segment, this.encode(previous, rate)); previous.fill(0) }
      this.preRoll = []; this.preRollSamples = 0
      this.options.status('listening', true, this.pendingCount())
    }
    const segment = this.recording
    if (!segment) {
      const copy = frame.slice(); this.preRoll.push(copy); this.preRollSamples += copy.length
      const maximum = Math.floor(rate * 0.5)
      while (this.preRollSamples > maximum) {
        const first = this.preRoll[0], drop = Math.min(first.length, this.preRollSamples - maximum)
        if (drop === first.length) this.preRoll.shift(); else this.preRoll[0] = first.slice(drop)
        first.fill(0); this.preRollSamples -= drop
      }
      return
    }
    this.appendPacket(segment, this.encode(frame, rate))
    if(activity.voiced)segment.inputEndAt=performance.now()
    segment.duration += duration
    segment.quietMs = activity.voiced ? 0 : segment.quietMs + duration
    if (segment.duration < 30000 && segment.quietMs < 800) return
    this.finishRecording(segment)
  }
  private finishRecording(segment: Segment): void {
    this.recording = null; this.detector.reset(); this.resampleAt = 0
    if (segment.sending) this.commit(segment)
    else {
      if (this.queued.length >= 4) { this.clearPackets(segment); this.fail('前面的语音仍在等待确认，已暂停收音。未确认的一句请稍后重说。'); return }
      this.queued.push(segment); this.pump()
    }
  }
  private commit(segment: Segment): void {
    if (this.disposed) { this.clearPackets(segment); return }
    if (!this.allowed() || this.committed || this.expectedResponse) { segment.sending = false; this.queued.push(segment); return }
    this.committed = segment
    if (!this.safeSend({ type: 'input_audio_buffer.commit' }, true)) return
    const epoch = this.epoch
    this.inputTimer = setTimeout(() => { if (!this.disposed && epoch === this.epoch && this.committed === segment) {
      this.fail('刚才的语音没有取得完整转写和保存回执，正在恢复连接；未确认的原话请重说。')
    } }, 30000)
    this.options.status('transcribing', false, this.pendingCount())
  }
  discardUnsentCommit():void{
    if(this.committed?.itemId!==null)return
    this.clearPackets(this.committed);this.committed=null
    if(this.inputTimer!==null)clearTimeout(this.inputTimer);this.inputTimer=null
    this.options.inputDiscarded?.();this.pump()
  }
  inputCommitted(itemId: string): boolean {
    const segment = this.committed, ledger = this.options.ledger()
    if (this.disposed || !identity(itemId) || !segment || !ledger || segment.itemId !== null
      || this.seenInputs.has(itemId) || this.seenInputs.size >= 256) return false
    if (!ledger.startInput(itemId, segment.capture)) return false
    this.seenInputs.add(itemId); segment.itemId = itemId
    return true
  }
  ownsCommittedInput(itemId: string | undefined): boolean {
    return !this.disposed && this.options.current() && !!itemId && this.committed?.itemId === itemId
  }
  async inputCompleted(itemId: string, transcript: string): Promise<boolean> {
    const segment = this.committed, ledger = this.options.ledger(), epoch = this.epoch
    if (!segment || segment.itemId !== itemId || !ledger || segment.admitted) return false
    // An explicit provider completion without recognized words is not a missing
    // completion. Retire only this segment; a newer recording/queue keeps its
    // own onset, packets and identity. Keep symbols and explicit ASCII mathematical
    // operators whose Unicode category is punctuation, not symbol.
    const compact = transcript.replace(/[\p{Z}\p{Cf}\s]/gu, '')
    if (!/^[-/*%:]+$/.test(compact) && !compact.replace(/\p{P}/gu, '')) {
      if (this.inputTimer !== null) clearTimeout(this.inputTimer)
      this.inputTimer = null; this.committed = null; this.clearPackets(segment)
      if (this.options.current()) {
        this.options.status('listening', this.recording !== null, this.pendingCount())
        this.pump()
      }
      return false
    }
    segment.admitted = true
    const admission = ledger.completeInput(itemId, transcript)
    if (!admission) { this.fail('这句话的原始采集身份未能核对，没有提交新的操作。'); return false }
    let receipt:QwenCanonicalAdmission
    try { receipt=await admission } catch { this.fail('原话的保存回执尚未确认，没有执行请求。'); return false }
    const stillOwnsInput = () => !this.disposed && epoch === this.epoch && this.committed === segment && this.options.current()
    if (!stillOwnsInput()) return false
    if (this.inputTimer !== null) clearTimeout(this.inputTimer)
    this.inputTimer = null
    this.options.inputCompleted(transcript, structuredClone(segment.capture))
    if (!stillOwnsInput()) return false
    if(receipt.local_control){this.dispose();this.options.localSafety?.(receipt.local_control);return true}
    if (this.expectedResponse) { this.fail('实时回复归属尚未确认，未执行新的操作。'); return false }
    if (!segment.superseded && !segment.interruptedAtOnset) this.interruptReply('confirmed_input')
    if (!stillOwnsInput()) return false
    this.committed = null
    if (!segment.superseded && !this.recording && !this.queued.length
      && this.options.onWorkspaceInput?.(transcript, structuredClone(segment.capture), receipt.voice_input_id)) {
      this.options.status('listening', false, this.pendingCount())
      this.pump()
      return true
    }
    if (!segment.superseded && receipt.curriculum_tutor
      && this.options.onCurriculumTutor?.(transcript, structuredClone(segment.capture), receipt.curriculum_tutor)) {
      this.options.status('listening', false, this.pendingCount())
      this.pump()
      return true
    }
    const diagnosticCapture = segment.capture.reference.question_reference?.kind === 'diagnostic' ? segment.capture : undefined
    this.continuation = { itemId, inputEndAt:segment.inputEndAt, superseded: segment.superseded, diagnosticCapture }
    this.pump()
    return true
  }
  private pump(): void {
    if (this.disposed || !this.options.current() || this.expectedResponse || this.committed) return
    if (this.continuation) {
      if (!this.options.canRespond()) return
      const request = this.continuation; this.continuation = null
      if (request.superseded) { this.pump(); return }
      request.createdAt=performance.now();this.expectedResponse = request
      const diagnosticTool = request.diagnosticCapture && this.options.currentDiagnosticAnswerTool?.(request.diagnosticCapture)
      const response = diagnosticTool ? { instructions: qwenNativeDiagnosticInstructions(diagnosticTool) } : undefined
      if (!this.safeSend({ type: 'response.create', ...(response ? { response } : {}) })) this.expectedResponse = null
      else {
        const epoch = this.epoch
        this.responseTimer = setTimeout(() => {
          if (!this.disposed && epoch === this.epoch && this.expectedResponse === request) {
            this.fail('实时回复没有返回对应的回合身份，正在恢复连接；原话已保存，没有自动重做。')
          }
        }, 12000)
      }
      return
    }
    if (!this.allowed()) return
    const next = this.queued.shift()
    if (next) {
      next.sending = true
      for (const packet of next.packets) { if (!this.safeSend({ type: 'input_audio_buffer.append', audio: base64(packet) }, true)) return; packet.fill(0) }
      next.packets = []; this.commit(next); return
    }
    if (this.recording?.explicit && !this.recording.sending && this.recording.voiced) {
      this.recording.sending = true
      for (const packet of this.recording.packets) { if (!this.safeSend({ type: 'input_audio_buffer.append', audio: base64(packet) }, true)) return; packet.fill(0) }
      this.recording.packets = []
    }
  }
  hasSeenResponse(responseId: string): boolean { return this.seenResponses.has(responseId) }
  responseCreated(responseId: string): boolean {
    if (this.disposed || !identity(responseId) || this.seenResponses.has(responseId) || this.seenResponses.size >= 512) return false
    this.seenResponses.add(responseId)
    const request = this.expectedResponse
    this.expectedResponse = null
    if (this.responseTimer !== null) clearTimeout(this.responseTimer)
    this.responseTimer = null
    if (!request || !this.options.ledger()?.bindResponse(responseId, request.itemId)) {
      this.options.responseBound(responseId, false); return false
    }
    this.responseInputs.set(responseId, request.itemId);this.responseTiming.set(responseId,request)
    const allowed = !request.superseded && this.options.current() && this.options.canRespond()
    if (!allowed) this.options.ledger()?.retireResponse(responseId)
    else this.activeResponse = responseId
    this.options.responseBound(responseId, allowed)
    this.pump()
    return allowed
  }
  requestContinuation(responseId: string): boolean {
    const itemId = this.responseInputs.get(responseId)
    if (this.disposed || !itemId || this.activeResponse !== responseId || !this.options.current()) return false
    this.continuation = { itemId, inputEndAt:this.responseTiming.get(responseId)?.inputEndAt??performance.now(), continuation:true,superseded: false }; this.pump(); return true
  }
  acceptActivationAudio(segment:NativeActivationSegment,current:()=>boolean):boolean{
    if(!current()||!this.allowed()||segment.overflow||!segment.frames.length||this.pendingCount()||this.expectedResponse||this.continuation||this.activeResponse&&this.options.ledger()?.ownsResponse(this.activeResponse)
      ||segment.capture.source!=='accepted_onset'||segment.capture.reference.local_recovery_only===true)return false
    if(this.rate&&this.rate!==segment.rate)return false
    this.activeResponse=null;this.clearPreRoll();this.options.speechStarted()
    this.recording={capture:structuredClone(segment.capture),packets:[],samples:0,sending:false,itemId:null,duration:0,quietMs:0,inputEndAt:performance.now(),admitted:false,superseded:false,interruptedAtOnset:false}
    for(const frame of segment.frames){if(!current()||!this.allowed()){this.gateChanged();return false}this.processFrame(frame,segment.rate);frame.fill(0)}
    segment.frames=[];return !this.disposed
  }
  retireResponseEligibility():void{
    if(this.committed)this.committed.superseded=true
    if(this.expectedResponse)this.expectedResponse.superseded=true
    if(this.continuation)this.continuation.superseded=true
    if(this.activeResponse)this.options.ledger()?.retireResponse(this.activeResponse)
  }
  requestRestoredResponse(itemId:string):boolean{
    if(this.disposed||!identity(itemId)||!this.options.current()||!this.options.canRespond()||this.pendingCount()||this.expectedResponse||this.continuation||this.activeResponse)return false
    this.continuation={itemId,inputEndAt:Number.NaN,superseded:false};this.pump();return true
  }
  isContinuation(responseId:string):boolean{return this.responseTiming.get(responseId)?.continuation===true}
  audioLatency(responseId:string,now:number):Record<string,number>{
    const t=this.responseTiming.get(responseId);return t?.createdAt===undefined?{}:{...(Number.isFinite(t.inputEndAt)?{latency_ms:Math.max(0,Math.round(now-t.inputEndAt))}:{}),first_audio_ms:Math.max(0,Math.round(now-t.createdAt))}
  }
  pendingCount(): number { return this.queued.length + (this.recording ? 1 : 0) + (this.committed ? 1 : 0) }
  hasPendingWork(): boolean { return this.pendingCount() > 0 || this.expectedResponse !== null || this.continuation !== null }
  private fail(message: string): void { if (!this.disposed) { this.dispose(); this.options.failed(message) } }
  dispose(): void {
    this.disposed = true; this.epoch++
    if (this.inputTimer !== null) clearTimeout(this.inputTimer)
    if (this.responseTimer !== null) clearTimeout(this.responseTimer)
    this.inputTimer = null; this.clearPackets(this.recording)
    this.responseTimer = null
    for (const queued of this.queued) this.clearPackets(queued)
    this.recording = null; this.queued = []; this.clearPreRoll()
    this.expectedResponse = null; this.continuation = null; this.activeResponse = null
  }
}
