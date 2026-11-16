import { getCompanionPlaybackControlAdmission, subscribeCompanionVoice, type CompanionPlaybackLease } from './companionVoice'
import { PlaybackControlAdmission } from './playbackControlAdmission'
import { microphoneEchoMode } from './microphoneEchoCancellation'
import { ProtectedPlaybackBarrier, protectedPlaybackBarrier } from './protectedPlaybackBarrier'
import { encodeControlWav, type LocalPlaybackControlPayload, type LocalPlaybackControlResult, type ProtectedCaptureOwner } from './protectedPlaybackControl'
import { matchWakeWord } from './wakeWordRuntime'

export const MAX_ATTENTION_WINDOWS = 512
const WINDOW_MS = 2400, STRIDE_MS = 1000, MIN_INTERVAL_MS = 750
type Owner = ProtectedCaptureOwner & { lease: CompanionPlaybackLease }
type Snapshot = { owner: Owner; word: string; rate: number; admission: PlaybackControlAdmission }
type Budget = { windows: number; lastAt: number }
// The original lease admission survives name/track/tap replacement. This ledger
// neither reads nor changes the original stop-control counters or quiet rules.
const budgets = new WeakMap<PlaybackControlAdmission, Budget>()
type Options = {
  current: () => Owner | null; capture: () => ProtectedCaptureOwner | null; word: () => string | undefined
  recognize: (session: string, payload: LocalPlaybackControlPayload, signal: AbortSignal) => Promise<LocalPlaybackControlResult>
  cancelExact: (id: string, generation: number) => boolean; holdInput: (held: boolean) => void; onWake: () => void
  admission?: (owner: Owner) => PlaybackControlAdmission | null; barrier?: ProtectedPlaybackBarrier; now?: () => number
}
/** Local attention only: one existing PCM fanout, bounded fresh rolling audio. */
export class LocalAttentionControl {
  private readonly barrier: ProtectedPlaybackBarrier
  private readonly now: () => number
  private snapshot: Snapshot | null = null
  private chunks: Float32Array[] = []
  private samples = 0
  private received = 0
  private sentAtSample = 0
  private frameAt = 0
  private request: AbortController | null = null
  private epoch = 0
  private disposed = false
  private cancelling = false
  private readonly unsubscribe: () => void
  private echoCache: { id: string; generation: number; text: string; word: string; rejected: boolean } | null = null
  constructor(private readonly options: Options) {
    this.barrier=options.barrier??protectedPlaybackBarrier;this.now=options.now??(()=>performance.now())
    this.unsubscribe=subscribeCompanionVoice(()=>{
      const owner=this.options.current(),cache=this.echoCache
      if(!this.owned(owner)||(cache&&(cache.id!==owner.lease.id||cache.generation!==owner.lease.generation||cache.text!==owner.lease.text))){
        if(this.cancelling){this.snapshot=null;this.clearFrames();this.echoCache=null}
        else this.retire()
      }
    })
  }
  private registeredWord(): string | undefined {
    let word: string | undefined
    try { word=this.options.word() } catch { this.echoCache=null;return undefined }
    if(typeof word!=='string'||!/^[a-zA-Z0-9\u3400-\u9fff]{2,12}$/.test(word)||/^\d+$/.test(word)){this.echoCache=null;return undefined}
    return word
  }
  private word(owner: Owner): string | undefined {
    const word=this.registeredWord()
    if(!word)return undefined
    const text=owner.lease.text
    if(typeof text!=='string'||!text.trim()||text.length>16000){this.echoCache=null;return undefined}
    const c=this.echoCache
    if(!c||c.id!==owner.lease.id||c.generation!==owner.lease.generation||c.text!==text||c.word!==word){
      this.echoCache={id:owner.lease.id,generation:owner.lease.generation,text,word,rejected:matchWakeWord(text,word).matched}
    }
    return this.echoCache!.rejected?undefined:word
  }
  private owned(owner: Owner | null): owner is Owner {
    if(!owner||owner.lease.kind!=='local_sapi'||owner.lease.sessionId!==owner.sessionId
        ||owner.localTrack.readyState!=='live'||owner.localTrack.enabled!==true)return false
    if(owner.inputKind==='segmented'?owner.submissionBlocked!==true:owner.cloudTrack.readyState!=='live'||owner.cloudTrack.enabled!==false)return false
    return ['all','browser-selected','remote-only'].includes(microphoneEchoMode(owner.localTrack))
  }
  private sameCapture(a: ProtectedCaptureOwner,b: ProtectedCaptureOwner): boolean {
    return a.sessionId===b.sessionId&&a.childId===b.childId&&a.localTrack===b.localTrack&&a.inputKind===b.inputKind
      &&(a.inputKind==='segmented'&&b.inputKind==='segmented'?a.captureEpoch===b.captureEpoch:
        a.inputKind==='realtime'&&b.inputKind==='realtime'&&a.cloudTrack===b.cloudTrack)
  }
  private same(a: Snapshot,b: Owner | null): boolean {
    return this.owned(b)&&this.sameCapture(a.owner,b)&&a.owner.lease.id===b.lease.id&&a.owner.lease.generation===b.lease.generation
      &&a.owner.lease.text===b.lease.text&&this.word(b)===a.word
  }
  private clearFrames(): void {
    for(const frame of this.chunks)frame.fill(0)
    this.chunks=[];this.samples=0;this.received=0;this.sentAtSample=0;this.frameAt=0
  }
  private retire(clearEcho=true): void { this.epoch++;this.request?.abort();this.request=null;this.snapshot=null;this.clearFrames();if(clearEcho)this.echoCache=null }
  processFrame(frame: Float32Array,rate: number): void {
    if(this.disposed||!frame.length||frame.length>8192||!Number.isFinite(rate)||rate<8000||rate>96000)return
    const owner=this.options.current(),now=this.now()
    if(!this.owned(owner)){this.retire();return}
    const word=this.word(owner)
    if(!word){this.retire(false);return}
    const admission=this.options.admission?this.options.admission(owner):getCompanionPlaybackControlAdmission(owner.lease.id,owner.lease.generation,owner.sessionId,owner.childId)
    if(!admission||!admission.bind(owner.lease.id,owner.lease.generation,owner.sessionId,owner.childId)){this.retire();return}
    if(!this.snapshot||!this.same(this.snapshot,owner)||this.snapshot.rate!==rate||this.snapshot.admission!==admission){
      this.retire(false);this.snapshot={owner:{...owner,lease:{...owner.lease}},word,rate,admission}
    }
    if(this.frameAt&&now-this.frameAt>500){this.clearFrames()}
    this.frameAt=now
    const limit=Math.floor(rate*WINDOW_MS/1000),copy=frame.slice()
    this.chunks.push(copy);this.samples+=copy.length;this.received+=copy.length
    while(this.samples>limit){
      const first=this.chunks[0],drop=Math.min(first.length,this.samples-limit)
      if(drop===first.length)this.chunks.shift();else this.chunks[0]=first.slice(drop)
      first.fill(0);this.samples-=drop
    }
    // Keep replacing the ring during decoding. Never queue old windows.
    if(this.request||this.samples<limit)return
    const budget=budgets.get(admission)??{windows:0,lastAt:-Infinity}
    budgets.set(admission,budget)
    if(budget.windows>=MAX_ATTENTION_WINDOWS||now-budget.lastAt<MIN_INTERVAL_MS
        ||(this.sentAtSample>0&&this.received-this.sentAtSample<rate*STRIDE_MS/1000))return
    budget.windows++;budget.lastAt=now;this.sentAtSample=this.received
    void this.dispatch(this.snapshot!,encodeControlWav(this.chunks,rate))
  }
  private async dispatch(captured: Snapshot,wav: string): Promise<void> {
    if(!this.same(captured,this.options.current()))return
    const request=new AbortController(),epoch=++this.epoch;this.request=request
    const deadline=this.now()+5000
    let timer:ReturnType<typeof setTimeout>|undefined
    const timeout=new Promise<null>(resolve=>{
      timer=setTimeout(()=>{
        request.abort()
        if(this.request===request&&this.epoch===epoch){this.request=null;this.epoch++}
        resolve(null)
      },5000)
    })
    try{
      const lease=captured.owner.lease
      const result=await Promise.race([this.options.recognize(captured.owner.sessionId,{child_id:captured.owner.childId,playback_lease_id:lease.id,
        playback_generation:lease.generation,capture_epoch:epoch,source_kind:'local_sapi',current_playback_text:lease.text!,
        attention_wake_word:captured.word,wav_base64:wav},request.signal),timeout])
      if(result===null)return
      if(this.disposed||request.signal.aborted||this.request!==request||epoch!==this.epoch||this.now()>=deadline
          ||!this.same(captured,this.options.current())||result.status!=='recognized'
          ||!['wake_companion','stop_playback'].includes(result.control??'')||result.playback_lease_id!==lease.id
          ||result.playback_generation!==lease.generation||result.capture_epoch!==epoch
          ||result.cloud_used!==false||result.persisted!==false||result.learning_evidence!==false)return
      const existing=this.barrier.token(),token=existing??this.barrier.begin(captured.owner,lease.id,lease.generation,this.now())
      if(!token)return
      this.options.holdInput(true)
      let cancelled=false
      try{this.cancelling=true;cancelled=this.options.cancelExact(lease.id,lease.generation)}finally{this.cancelling=false}
      if(!cancelled){
        if(!existing)this.barrier.rollback(token)
        if(!this.barrier.held())this.options.holdInput(false)
        return
      }
      // No await: this one exact current cancellation owns only a short local
      // attention response. The independent original tail barrier stays held.
      if(result.control==='wake_companion'&&!this.disposed&&!request.signal.aborted&&this.request===request&&epoch===this.epoch
          &&this.options.current()===null&&this.registeredWord()===captured.word){
        const current=this.options.capture()
        if(current&&current.localTrack.readyState==='live'&&current.localTrack.enabled===true&&this.sameCapture(captured.owner,current)
            &&(current.inputKind==='segmented'?current.submissionBlocked===true:current.cloudTrack.readyState==='live'&&current.cloudTrack.enabled===false))this.options.onWake()
      }
    }catch{/* Local fixed controls only; no transcript, retry, provider body or persisted record. */}
    finally{if(timer!==undefined)clearTimeout(timer);if(this.request===request)this.request=null}
  }
  dispose(): void { this.disposed=true;this.unsubscribe();this.retire() }
}
