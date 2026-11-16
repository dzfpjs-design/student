import { CaptureFrameMetadataCounter, type FrameMetadata } from './currentRealtimeCaptureMetadata'
import { getCompanionPlaybackControlAdmission, type CompanionPlaybackLease } from './companionVoice'
import { PlaybackControlAdmission } from './playbackControlAdmission'
export { MAX_LOCAL_CONTROL_SEGMENTS } from './playbackControlAdmission'
import { AdaptiveVoiceActivityDetector } from './voiceActivityDetector'
import { microphoneEchoMode } from './microphoneEchoCancellation'
import { ProtectedPlaybackBarrier, protectedPlaybackBarrier } from './protectedPlaybackBarrier'
import { isPetPcmControlReference, type PetPcmControlReference } from './petPlaybackReference'
import { matchWakeWord } from './wakeWordRuntime'

export type LocalPlaybackControlResult = { status: string; control: 'stop_playback' | 'wake_companion' | null; playback_lease_id: string;
  playback_generation: number; capture_epoch: number; cloud_used: false; persisted: false; learning_evidence: false }
export type LocalPlaybackControlPayload = { child_id: string; playback_lease_id: string; playback_generation: number;
  capture_epoch: number; source_kind: string; current_playback_text?: string; pcm_reference?: PetPcmControlReference; attention_wake_word?: string; wav_base64: string }
export type ProtectedCaptureOwner = { sessionId: string; childId: string; localTrack: MediaStreamTrack } & (
  { inputKind: 'realtime'; cloudTrack: MediaStreamTrack } | { inputKind: 'segmented'; captureEpoch: symbol; submissionBlocked: boolean })
type Owner = ProtectedCaptureOwner & { lease: CompanionPlaybackLease }
type Options = { current: () => Owner | null; capture?: () => ProtectedCaptureOwner | null; barrier?: ProtectedPlaybackBarrier; recognize: (session: string, payload: LocalPlaybackControlPayload, signal: AbortSignal) => Promise<LocalPlaybackControlResult>;
  admission?: (owner: Owner) => PlaybackControlAdmission | null;
  attentionWakeWord?: () => string | undefined; onAttentionWake?: () => void;
  cancelExact: (id: string, generation: number) => boolean; holdCloud: (hold: boolean) => void; failClosed: () => void; now?: () => number }
export function encodeControlWav(chunks: Float32Array[], rate: number): string {
  const size = chunks.reduce((n, x) => n + x.length, 0), input = new Float32Array(size);let offset = 0
  for (const c of chunks) { input.set(c, offset);offset += c.length }
  const count = Math.min(40000, Math.floor(size * 16000 / rate)), bytes = new Uint8Array(44 + count * 2), view = new DataView(bytes.buffer)
  const word = (at: number, s: string): void => { for (let i = 0; i < s.length; i++) bytes[at + i] = s.charCodeAt(i) }
  word(0,'RIFF');view.setUint32(4,36+count*2,true);word(8,'WAVE');word(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true)
  view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);word(36,'data');view.setUint32(40,count*2,true)
  for (let i = 0; i < count; i++) { const sample = Math.max(-1,Math.min(1,input[Math.min(size-1,Math.floor(i*rate/16000))]??0));view.setInt16(44+i*2,Math.round(sample*(sample<0?32768:32767)),true) }
  let text='';for (const byte of bytes) text+=String.fromCharCode(byte);return btoa(text)
}
/** One existing microphone source; no second getUserMedia or capture owner. */
export class ProtectedPlaybackControl {
  private detector = new AdaptiveVoiceActivityDetector()
  private context: AudioContext | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private processor: ScriptProcessorNode | null = null
  private gain: GainNode | null = null
  private chunks: Float32Array[] = []
  private preRoll: Float32Array[] = []
  private preRollSamples = 0
  private preRollRate = 0
  private preRollAt = 0
  private captured: Owner | null = null
  private capturedAttentionWord: string | undefined
  private attentionEchoCache: {id:string;generation:number;text:string;word:string;rejected:boolean}|null=null
  private bufferedMs = 0
  private startedAt = 0
  private lastVoiceAt = 0
  private epoch = 0
  private request: AbortController | null = null
  private requestOwner: Owner | null = null
  private admissionOwner: Owner | null = null
  private admission: PlaybackControlAdmission | null = null
  private captureEpoch = Symbol()
  private tailOwner: ProtectedCaptureOwner | null = null
  private tailEpoch = Symbol()
  private observedTail: symbol | null = null
  private tailFailed = false
  private tailHeld = false
  private cooldownTimer: ReturnType<typeof setTimeout> | null = null
  private disposed = false
  private metadataFrames: CaptureFrameMetadataCounter | null = null
  private metadataStream: MediaStream | null = null
  private readonly barrier: ProtectedPlaybackBarrier
  private readonly now: () => number
  constructor(private readonly options: Options) { this.now=options.now??(()=>performance.now());this.barrier=options.barrier??protectedPlaybackBarrier }
  async attach(stream: MediaStream, onFrame?: (frame: Float32Array, rate: number) => void): Promise<void> {
    if (this.disposed || this.context) return
    const context=new AudioContext();this.context=context
    try{
      const source=context.createMediaStreamSource(stream);this.source=source
      const processor=context.createScriptProcessor(1024,1,1);this.processor=processor
      const gain=context.createGain();this.gain=gain;gain.gain.value=0
      this.metadataStream=stream;this.metadataFrames=new CaptureFrameMetadataCounter(stream,context,processor)
      processor.onaudioprocess=e=>{this.metadataFrames?.observe(e,performance.now());const frame=new Float32Array(e.inputBuffer.getChannelData(0));this.processFrame(frame,context.sampleRate);onFrame?.(frame,context.sampleRate)}
      source.connect(processor);processor.connect(gain);gain.connect(context.destination)
      if (context.state==='suspended') await context.resume()
      if (this.disposed) this.disconnect()
    }catch(error){this.disconnect();throw error}
  }
  readCaptureFrameMetadata(stream: MediaStream, root: MediaStreamTrack, now: number): FrameMetadata | null {
    if(this.disposed||stream!==this.metadataStream||!this.context||!this.source||!this.processor||!this.gain||this.gain.gain.value!==0||typeof this.processor.onaudioprocess!=='function')return null
    return this.metadataFrames?.read(stream,root,now)??null
  }
  private captureOwned(owner: ProtectedCaptureOwner | null): owner is ProtectedCaptureOwner {
    return !!owner&&owner.localTrack.readyState==='live'&&owner.localTrack.enabled===true
      &&(owner.inputKind==='realtime'?owner.cloudTrack.readyState==='live'&&owner.cloudTrack.enabled===false:owner.submissionBlocked===true)
  }
  private captureEligible(owner: ProtectedCaptureOwner | null): owner is ProtectedCaptureOwner {
    if(!this.captureOwned(owner))return false
    // Effective AEC permits only ephemeral local stop recognition/quiet observation.
    // It does not grant external-playback cloud duplex or acoustic quality evidence.
    const mode=microphoneEchoMode(owner.localTrack)
    return mode==='all'||mode==='browser-selected'||mode==='remote-only'
  }
  private eligible(owner: Owner | null): owner is Owner {
    if (!this.captureEligible(owner) || !('lease' in owner) || owner.lease.sessionId !== owner.sessionId) return false
    if (owner.lease.kind === 'local_sapi') return !!owner.lease.text?.trim() && owner.lease.text.length <= 16000
    const ref = owner.lease.pcmReference
    return owner.lease.kind === 'local_pcm' && owner.lease.text === null && isPetPcmControlReference(ref)
      && ref.session_id === owner.sessionId && ref.child_id === owner.childId
  }
  private sameCapture(a: ProtectedCaptureOwner, b: ProtectedCaptureOwner): boolean {
    return a.sessionId===b.sessionId&&a.childId===b.childId&&a.localTrack===b.localTrack&&a.inputKind===b.inputKind
      &&(a.inputKind==='realtime'&&b.inputKind==='realtime'?a.cloudTrack===b.cloudTrack:a.inputKind==='segmented'&&b.inputKind==='segmented'&&a.captureEpoch===b.captureEpoch)
  }
  private same(a: Owner, b: Owner | null): boolean {
    return this.eligible(b)&&this.sameCapture(a,b)&&a.lease.id===b.lease.id&&a.lease.generation===b.lease.generation&&a.lease.text===b.lease.text&&a.lease.pcmReference===b.lease.pcmReference
  }
  private attentionWord(owner: Owner): string | undefined {
    if (owner.lease.kind !== 'local_sapi' || !this.options.onAttentionWake) return undefined
    let word: string | undefined
    try { word = this.options.attentionWakeWord?.() } catch { return undefined }
    if (typeof word !== 'string' || !/^[a-zA-Z0-9\u3400-\u9fff]{2,12}$/.test(word) || /^\d+$/.test(word)) return undefined
    // Reject a reflected name in the original SAPI text before recognition;
    // the backend independently applies its finite phonetic echo filter too.
    const text=owner.lease.text??'',cache=this.attentionEchoCache
    if(!cache||cache.id!==owner.lease.id||cache.generation!==owner.lease.generation||cache.text!==text||cache.word!==word){
      this.attentionEchoCache={id:owner.lease.id,generation:owner.lease.generation,text,word,rejected:matchWakeWord(text,word).matched}
    }
    return this.attentionEchoCache!.rejected ? undefined : word
  }
  private observeTail(activity: {voiced: boolean}, duration: number, now: number): boolean {
    const token=this.barrier.token()
    if(!token){if(this.tailHeld){this.tailHeld=false;this.observedTail=null;this.clearCooldownTimer();this.options.holdCloud(false);this.clear()}return false}
    this.tailHeld=true;this.options.holdCloud(true);this.request?.abort();this.clear()
    const capture=this.options.capture ? this.options.capture() : this.options.current()
    if(this.captureOwned(capture)){
      if(!this.tailOwner||!this.sameCapture(this.tailOwner,capture)){this.tailOwner={...capture};this.tailEpoch=Symbol();this.observedTail=null}
      if(this.observedTail!==token){
        this.observedTail=token
        if(!this.barrier.claim(capture,this.tailEpoch,now)){this.tailFailed=true;this.options.failClosed();return true}
        this.tailFailed=false;this.clearCooldownTimer()
      }
      const result=this.captureEligible(capture)?this.barrier.observe(capture,this.tailEpoch,activity.voiced,duration,now):this.barrier.tick(now)?'failed':'held'
      if(result==='released'){this.tailHeld=false;this.observedTail=null;this.clearCooldownTimer();this.options.holdCloud(false);this.clear();return true}
      if(result==='failed'&&!this.tailFailed){this.tailFailed=true;this.options.failClosed()}
    }
    if(this.cooldownTimer===null&&!this.tailFailed){
      this.cooldownTimer=setTimeout(()=>{this.cooldownTimer=null;if(!this.disposed&&this.barrier.held()){this.barrier.tick(this.now());this.tailFailed=true;this.options.failClosed()}},this.barrier.remainingMs(now))
    }
    return true
  }
  private clearPreRoll(): void {
    for(const frame of this.preRoll)frame.fill(0)
    this.preRoll=[];this.preRollSamples=0;this.preRollRate=0;this.preRollAt=0
  }
  /** Only already-admitted frames from this instance's current source/lease. */
  private rememberPreRoll(frame: Float32Array, rate: number, now: number): void {
    if(this.preRollRate!==rate||now-this.preRollAt>160)this.clearPreRoll()
    this.preRollRate=rate;this.preRollAt=now
    const limit=Math.floor(rate*0.16),copy=frame.slice(Math.max(0,frame.length-limit))
    this.preRoll.push(copy);this.preRollSamples+=copy.length
    while(this.preRollSamples>limit){
      const first=this.preRoll[0],drop=Math.min(first.length,this.preRollSamples-limit)
      if(drop===first.length)this.preRoll.shift()
      else this.preRoll[0]=first.slice(drop)
      first.fill(0);this.preRollSamples-=drop
    }
  }
  private clear(): void { this.clearPreRoll();this.bufferedMs=0;this.chunks=[];this.captured=null;this.capturedAttentionWord=undefined;this.startedAt=0;this.lastVoiceAt=0;this.detector.reset() }
  /** Public for the existing source fanout; a frame only segments, never authorizes a command. */
  processFrame(frame: Float32Array, rate: number): void {
    if(this.disposed||!frame.length||!Number.isFinite(rate)||rate<8000||rate>96000||frame.length>8192)return
    const now=this.now();let energy=0;for(const v of frame)energy+=v*v
    let activity=this.detector.observe(Math.sqrt(energy/frame.length),frame.length/rate*1000)
    if(!this.options.current())this.attentionEchoCache=null
    if(this.observeTail(activity,frame.length/rate*1000,now))return
    const owner=this.options.current()
    if(!this.eligible(owner)){this.request?.abort();this.clear();this.attentionEchoCache=null;return}
    if(this.request){if(this.requestOwner&&!this.same(this.requestOwner,owner))this.request.abort();return}
    const admission=this.options.admission ? this.options.admission(owner) : getCompanionPlaybackControlAdmission(owner.lease.id,owner.lease.generation,owner.sessionId,owner.childId)
    if(!admission){this.clear();return}
    if(!this.admissionOwner||!this.same(this.admissionOwner,owner)||this.admission!==admission){
      this.admissionOwner={...owner,lease:{...owner.lease}};this.admission=admission;this.captureEpoch=Symbol();this.clear();activity=this.detector.observe(Math.sqrt(energy/frame.length),frame.length/rate*1000)
    }
    if(!admission.admitFrame(this.captureEpoch,activity.voiced,frame.length/rate*1000,now)){this.clear();return}
    if(this.captured&&!this.same(this.captured,owner)){this.clear();return}
    if(!this.captured&&activity.voiced){
      if(this.preRollRate!==rate||now-this.preRollAt>160)this.clearPreRoll()
      this.captured={...owner,lease:{...owner.lease}};this.startedAt=now
      this.capturedAttentionWord=this.attentionWord(owner)
      this.chunks=this.preRoll.slice();this.bufferedMs=this.preRollSamples/rate*1000
    }
    if(!this.captured){this.rememberPreRoll(frame,rate,now);return}
    this.chunks.push(frame);this.bufferedMs+=frame.length/rate*1000;if(activity.voiced)this.lastVoiceAt=now
    if(now-this.startedAt>2500||this.bufferedMs>2500){this.admission?.consume(this.captureEpoch,now);this.clear();return}
    if(activity.speechDetected&&now-this.lastVoiceAt>=240&&now-this.startedAt>=120)void this.dispatch(rate)
  }
  private async dispatch(rate: number): Promise<void> {
    const captured=this.captured;if(!captured||!this.same(captured,this.options.current())){this.clear();return}
    if(!this.admission?.consume(this.captureEpoch,this.now())){this.clear();return}
    const attentionWord=this.capturedAttentionWord
    const allowAttention=attentionWord !== undefined && this.attentionWord(captured) === attentionWord
    const wav=encodeControlWav(this.chunks,rate),epoch=++this.epoch,request=new AbortController();this.request=request;this.requestOwner=captured;this.clear()
    const deadline=this.now()+5000,timer=setTimeout(()=>request.abort(),5000)
    try{
      const reference = captured.lease.kind === 'local_pcm'
        ? { source_kind: 'local_pcm', pcm_reference: captured.lease.pcmReference! }
        : { source_kind: 'local_sapi', current_playback_text: captured.lease.text! }
      const result=await this.options.recognize(captured.sessionId,{child_id:captured.childId,playback_lease_id:captured.lease.id,playback_generation:captured.lease.generation,capture_epoch:epoch,...reference,
        ...(allowAttention?{attention_wake_word:attentionWord}:{}),wav_base64:wav},request.signal)
      const attention=result.control==='wake_companion'
      if(this.disposed||this.now()>=deadline||request.signal.aborted||this.request!==request||epoch!==this.epoch||!this.same(captured,this.options.current())
        ||result.status!=='recognized'||(result.control!=='stop_playback'&&!attention)||result.playback_lease_id!==captured.lease.id||result.playback_generation!==captured.lease.generation||result.capture_epoch!==epoch
        ||result.cloud_used!==false||result.persisted!==false||result.learning_evidence!==false)return
      if(attention&&(!allowAttention||this.attentionWord(captured)!==attentionWord))return
      // Install the shared barrier before ending the exact protected output.
      const token=this.barrier.begin(captured,captured.lease.id,captured.lease.generation,this.now())
      if(!token)return
      this.options.holdCloud(true)
      if(!this.options.cancelExact(captured.lease.id,captured.lease.generation)){this.barrier.rollback(token);if(!this.barrier.held())this.options.holdCloud(false);return}
      this.observeTail({voiced:true},0,this.now())
      // observeTail intentionally aborts this now-consumed recognition request.
      // No await occurs between the validated exact cancel and this callback.
      if(attention&&!this.disposed&&this.request===request&&epoch===this.epoch
          &&this.options.current()===null&&this.options.attentionWakeWord?.()===attentionWord){
        const current=this.options.capture?.()
        if(this.captureOwned(current??null)&&this.sameCapture(captured,current!))this.options.onAttentionWake?.()
      }
    }catch{/* No raw transcript/provider body or retry. */}
    finally{clearTimeout(timer);if(this.request===request){this.request=null;this.requestOwner=null}}
  }
  private clearCooldownTimer(): void { if(this.cooldownTimer!==null)clearTimeout(this.cooldownTimer);this.cooldownTimer=null }
  private disconnect(): void {
    this.metadataFrames=null;this.metadataStream=null
    if(this.processor)this.processor.onaudioprocess=null
    this.source?.disconnect();this.processor?.disconnect();this.gain?.disconnect();void this.context?.close().catch(()=>undefined)
    this.context=null;this.source=null;this.processor=null;this.gain=null
  }
  dispose(): void { this.disposed=true;this.clearCooldownTimer();this.epoch++;this.request?.abort();this.request=null;this.requestOwner=null;this.attentionEchoCache=null;this.clear();this.disconnect() }
}
