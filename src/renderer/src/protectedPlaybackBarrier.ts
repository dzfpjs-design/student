export type PlaybackTailScope = { sessionId: string; childId: string }
type Tail = PlaybackTailScope & { token: symbol; leaseId: string; generation: number; originalDeadline: number; deadline: number;
  observerSessionId: string | null; observerChildId: string | null;
  failed: boolean; everFailed: boolean; recoveryWindows: number; epoch: symbol | null; quietMs: number; quietStartedAt: number | null }
export const PLAYBACK_TAIL_FAILURE = '播放尾部静音尚未确认，麦克风已停止。家长可用现有麦克风控件重试或停止。'
/** Only one finite metadata barrier. Never stores audio, recognizer results or capture objects. */
export class ProtectedPlaybackBarrier {
  private tail: Tail | null = null
  held(): boolean { return this.tail !== null }
  token(): symbol | null { return this.tail?.token ?? null }
  private valid(scope: PlaybackTailScope): boolean { return !!scope.sessionId.trim() && !!scope.childId.trim() }
  private observes(scope: PlaybackTailScope): boolean { return !!this.tail && this.tail.observerSessionId === scope.sessionId && this.tail.observerChildId === scope.childId }
  begin(scope: PlaybackTailScope, leaseId: string, generation: number, now: number): symbol | null {
    if (this.tail || !this.valid(scope)) return null
    const token=Symbol();this.tail={...scope,token,leaseId,generation,originalDeadline:now+5000,deadline:now+5000,
      observerSessionId:null,observerChildId:null,failed:false,everFailed:false,recoveryWindows:0,epoch:null,quietMs:0,quietStartedAt:null};return token
  }
  rollback(token: symbol): void { if(this.tail?.token===token)this.tail=null }
  tick(now: number): boolean {
    if(this.tail&&now>=this.tail.deadline&&!this.tail.failed){
      const windowKind=this.tail.deadline===this.tail.originalDeadline?'initial':'recovery'
      this.tail.failed=true;this.tail.everFailed=true;this.tail.quietMs=0;this.tail.quietStartedAt=null
      // A disposed old tap cannot erase expiry: the next preflight records it too.
      void window.desktop?.recordRendererDiagnostic?.({event:'voice.capture.protected_tail_window_failed',level:'warn',
        details:{window:windowKind,recovery_windows:this.tail.recoveryWindows}}).catch(()=>undefined)
    }
    return this.tail?.failed ?? false
  }
  mayOpen(scope: PlaybackTailScope, now: number): boolean {
    this.tick(now)
    return this.valid(scope) && (!this.tail || !this.tail.failed || this.tail.recoveryWindows<2)
  }
  reserveOpen(scope: PlaybackTailScope, now: number): boolean {
    if(!this.mayOpen(scope,now))return false
    const t=this.tail
    if(t?.failed){t.recoveryWindows++;t.deadline=now+5000;t.failed=false;t.epoch=null;t.quietMs=0;t.quietStartedAt=null}
    return true
  }
  explicitRetry(scope: PlaybackTailScope, now: number): void {
    this.tick(now)
    if(this.valid(scope)&&this.tail?.failed){this.tail.recoveryWindows=0;this.tail.epoch=null;this.tail.quietMs=0;this.tail.quietStartedAt=null}
  }
  claim(scope: PlaybackTailScope, epoch: symbol, now: number): boolean {
    this.tick(now)
    const t=this.tail;if(!t||!this.valid(scope))return false
    if(t.failed){if(t.recoveryWindows>=2)return false;t.recoveryWindows++;t.deadline=now+5000;t.failed=false}
    // A newly registered owner can observe quiet after a child/session switch.
    // The original cancellation scope/deadline and global recovery quota stay intact.
    t.observerSessionId=scope.sessionId;t.observerChildId=scope.childId
    t.epoch=epoch;t.quietMs=0;t.quietStartedAt=null;return true
  }
  observe(scope: PlaybackTailScope, epoch: symbol, voiced: boolean, durationMs: number, now: number): 'held'|'released'|'failed' {
    this.tick(now)
    const t=this.tail;if(!t)return 'released'
    if(!this.observes(scope)||t.epoch!==epoch)return 'held'
    if(t.failed)return 'failed'
    if(voiced){t.quietMs=0;t.quietStartedAt=null}else{if(t.quietStartedAt===null)t.quietStartedAt=now;t.quietMs+=durationMs}
    if(t.quietMs>=850&&t.quietStartedAt!==null&&now-t.quietStartedAt>=850){this.tail=null;return 'released'}
    return 'held'
  }
  remainingMs(now: number): number { return this.tail?Math.max(0,this.tail.deadline-now):0 }
}
export const protectedPlaybackBarrier = new ProtectedPlaybackBarrier()
