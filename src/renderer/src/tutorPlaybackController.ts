import { ApiError, localApi } from './api'
import type { CompanionSpeechOptions, CompanionSpeechResult } from './companionVoice'
import type { TutorBlackboardPresentation, TutorPlaybackCheckpoint } from './types'

type Operation = 'begin' | 'control' | 'segment'
type Command = 'pause' | 'resume' | 'previous' | 'restart' | 'cancel'
type Speech = (text: string, session: string, result: (value: CompanionSpeechResult) => void,
  started: () => void, options: CompanionSpeechOptions) => void
type Pending = { childId: string; sessionId: string; operation: Operation; values: Record<string, unknown> }

export function clearTutorPlaybackLocalState(childId: string): void {
  const prefix = `hsc-tutor-playback-pending-v1:${encodeURIComponent(childId)}:`
  for (const key of Object.keys(localStorage)) if (key.startsWith(prefix)) localStorage.removeItem(key)
  window.dispatchEvent(new CustomEvent('hsc-tutor-playback-forget', { detail: { childId } }))
}

/** One runtime owner; saved delivery is not evidence of understanding. */
export class TutorPlaybackController {
  private checkpoint: TutorPlaybackCheckpoint | null = null
  private queue: Promise<unknown> = Promise.resolve()
  private lease = 0
  private cancelling = false
  private disposed = false
  private textOnlyDisplay: { playbackId: string; index: number } | null = null
  private policy: CompanionSpeechOptions = { localOnly: true }
  private localNarrationActive = false
  private readonly key: string

  constructor(private readonly sessionId: string, private readonly childId: string, private readonly ports: {
    speak: Speech; cancel: () => void; allowed: () => boolean;
    localNarration?: (active: boolean) => void;
    display: (state: TutorPlaybackCheckpoint, revealed: number) => void;
    error: (message: string) => void; response?: (state:TutorPlaybackCheckpoint,text:string,current:()=>boolean,sourceAgentTurnId?:string)=>ReturnType<typeof import('./ordinaryResponseGrant').ordinaryPromptPlayback>|undefined; delivery?: (value: CompanionSpeechResult) => void;
  }) { this.key = `hsc-tutor-playback-pending-v1:${encodeURIComponent(childId)}:${encodeURIComponent(sessionId)}` }

  private valid(lease: number): boolean { return !this.disposed && lease === this.lease && this.ports.allowed() }
  private setLocalNarration(active: boolean): void {
    if (active === this.localNarrationActive) return
    this.localNarrationActive = active
    this.ports.localNarration?.(active)
  }
  private publish(state: TutorPlaybackCheckpoint): void {
    if (state.child_id !== this.childId || state.session_id !== this.sessionId) throw new Error('TUTOR_PLAYBACK_OWNER_MISMATCH')
    this.checkpoint = state
    const textCount = this.textOnlyDisplay?.playbackId === state.playback_id && this.textOnlyDisplay.index === state.segment_index
      ? state.segment_index + 1 : 0
    if (!this.disposed) this.ports.display(state, Math.max(textCount, state.segment_index + Number(state.segment_started)))
  }
  private serial<T>(work: () => Promise<T>): Promise<T> {
    const result = this.queue.catch(() => undefined).then(work)
    this.queue = result
    return result
  }
  private pending(): Pending | null {
    const raw = localStorage.getItem(this.key)
    if (!raw) return null
    const item = JSON.parse(raw) as Pending
    if (item.childId !== this.childId || item.sessionId !== this.sessionId
      || !['begin', 'control', 'segment'].includes(item.operation) || !item.values
      || typeof item.values.client_action_id !== 'string') throw new Error('TUTOR_PLAYBACK_RECOVERY_INVALID')
    return item
  }
  private async write(operation: Operation, values: Record<string, unknown>): Promise<TutorPlaybackCheckpoint> {
    const pending = this.pending()
    if (pending) throw new Error('TUTOR_PLAYBACK_RECEIPT_PENDING')
    localStorage.setItem(this.key, JSON.stringify({ childId: this.childId, sessionId: this.sessionId, operation, values }))
    const result = await localApi.mutateTutorPlayback(this.sessionId, operation, values)
    this.publish(result.playback)
    localStorage.removeItem(this.key)
    return result.playback
  }
  private async recover(): Promise<Pending | null> {
    const pending = this.pending()
    if (pending) {
      let result
      try { result = await localApi.mutateTutorPlayback(this.sessionId, pending.operation, pending.values) }
      catch (error) {
        if (error instanceof ApiError && ['STALE_GENERATION', 'SOURCE_INVALIDATED', 'SOURCE_VERSION_CONFLICT', 'TUTOR_PLAYBACK_NOT_FOUND'].includes(error.code ?? '')) {
          // A definitive fencing response cannot be retried into a newer lease.
          localStorage.removeItem(this.key)
          return null
        }
        throw error
      }
      this.publish(result.playback)
      localStorage.removeItem(this.key)
    }
    return pending
  }
  private binding(state: TutorPlaybackCheckpoint): Record<string, unknown> {
    return { playback_id: state.playback_id, source_version: state.source_version,
      generation: state.generation, expected_revision: state.revision, client_action_id: crypto.randomUUID() }
  }
  private stopAudio(): void {
    this.lease++
    if (this.cancelling) return
    this.cancelling = true
    try { this.ports.cancel() } finally { this.cancelling = false; this.setLocalNarration(false) }
  }
  private fail(): void {
    this.stopAudio()
    if (!this.disposed) this.ports.error('讲解进度没有确认保存，已暂停。说“继续刚才的讲解”时会先核对原记录，不会跳过未讲完的内容。')
  }

  async restore(): Promise<void> {
    this.disposed = false
    const lease = this.lease
    const state = (await localApi.tutorPlayback(this.sessionId)).playback
    if (!this.disposed && lease === this.lease && state) this.publish(state)
    // A reload never starts speech or retries a write automatically.
  }

  present(turnId: string, presentation: TutorBlackboardPresentation, policy: CompanionSpeechOptions): Promise<void> {
    this.stopAudio()
    this.textOnlyDisplay = null
    const lease = this.lease
    this.policy = policy
    return this.serial(async () => {
      await this.recover()
      if (!this.valid(lease)) return
      let state = (await localApi.tutorPlayback(this.sessionId)).playback
      if (!this.valid(lease)) return
      if (state && state.turn_id !== turnId && ['paused', 'playing'].includes(state.status)) {
        await this.write('control', { ...this.binding(state), command: 'cancel' })
        state = null
      }
      if (!state || state.turn_id !== turnId || ['cancelled', 'invalidated'].includes(state.status)) {
        // A new explicit presentation after cancellation is a new playback.
        // The journal, not the tutor turn, owns retry identity for this begin.
        state = await this.write('begin', { turn_id: turnId, presentation, client_action_id: crypto.randomUUID() })
      }
      if (!this.valid(lease)) return
      this.publish(state)
      if (state.status === 'playback_finished') return
      state = await this.write('control', { ...this.binding(state), command: 'resume' })
      if (this.valid(lease)) this.play(state, lease)
    }).catch(error => { if (this.valid(lease)) this.fail(); throw error })
  }

  command(command: Command, binding?: { playbackId: string; sourceVersion: string }): Promise<TutorPlaybackCheckpoint | null> {
    this.stopAudio()
    this.textOnlyDisplay = null
    const lease = this.lease
    this.policy = { localOnly: true }
    return this.serial(async () => {
      const recovered = await this.recover()
      if (!this.valid(lease)) return null
      let state = (await localApi.tutorPlayback(this.sessionId)).playback
      if (!state) return null
      if (binding && (state.playback_id !== binding.playbackId || state.source_version !== binding.sourceVersion)) {
        throw new Error('TUTOR_PLAYBACK_BINDING_CHANGED')
      }
      if (!(recovered?.operation === 'control' && recovered.values.command === command)) {
        state = await this.write('control', { ...this.binding(state), command })
      } else this.publish(state)
      if (command === 'resume' && this.valid(lease) && state.status === 'playing') this.play(state, lease)
      return state
    }).catch(error => { if (this.valid(lease)) this.fail(); throw error })
  }

  interrupt(): void {
    if (this.cancelling || this.disposed) return
    const previous = this.checkpoint
    this.stopAudio()
    const lease = this.lease
    void this.serial(async () => {
      // An unconfirmed callback is recovered only by an explicit child action.
      if (this.disposed || this.pending()) return
      const state = (await localApi.tutorPlayback(this.sessionId)).playback
      if (this.disposed || lease !== this.lease) return
      if (state?.status === 'playing') await this.write('control', { ...this.binding(state), command: 'pause' })
    }).catch(() => {
      // A late read failure for an absent/older board must not stop new speech.
      if (!this.disposed && lease === this.lease && previous?.status === 'playing') {
        this.ports.error('讲解已停止播放，但暂停记录尚未确认。继续时会先核对原记录。')
      }
    })
  }

  private pauseUndelivered(state: TutorPlaybackCheckpoint, lease: number): void {
    if (!this.valid(lease)) return
    this.lease++
    this.setLocalNarration(false)
    // The arbiter already owns cancellation. Do not stop another admitted voice.
    void this.serial(async () => {
      if (this.disposed || this.pending()) return
      const current = (await localApi.tutorPlayback(this.sessionId)).playback
      if (current?.status === 'playing' && current.playback_id === state.playback_id
        && current.source_version === state.source_version && current.generation === state.generation) {
        await this.write('control', { ...this.binding(current), command: 'pause' })
      }
    }).catch(() => {
      if (!this.disposed) this.ports.error('讲解已停止播放，但暂停记录尚未确认。继续时会先核对原记录。')
    })
  }

  private play(state: TutorPlaybackCheckpoint, lease: number): void {
    if (!this.valid(lease)) {
      if (lease === this.lease) this.setLocalNarration(false)
      return
    }
    const presentation = state.presentation
    const segment = state.status === 'playing' ? presentation?.segments[state.segment_index] : undefined
    if (!presentation || !segment) { this.setLocalNarration(false); return }
    // Keep one protected interval across all segments and their save receipts.
    this.setLocalNarration(this.policy.localOnly === true)
    const response=state.segment_index===presentation.segments.length-1?this.ports.response?.(state,segment.narration,()=>this.valid(lease)&&(this.policy.isCurrent?.()??true),this.policy.sourceAgentTurnId):undefined
    let started = false
    let ended = false
    this.ports.speak(segment.narration, this.sessionId, result => {
      if (!this.valid(lease)) return
      this.ports.delivery?.(result)
      if (result === 'text_only') {
        response?.grant.revoke()
        this.textOnlyDisplay = { playbackId: state.playback_id, index: state.segment_index }
        this.ports.display(state, state.segment_index + 1)
        this.pauseUndelivered(state, lease)
      }
    }, () => {
      if (!this.valid(lease) || started || ended) return
      started = true
      response?.started()
      this.ports.display(state, state.segment_index + 1)
      void this.serial(async () => {
        if (!this.valid(lease)) return
        await this.write('segment', { ...this.binding(state), segment_id: segment.segment_id, event: 'started' })
      }).catch(() => { if (this.valid(lease)) this.fail() })
    }, { ...this.policy, priority: 'lesson',
      isCurrent: () => this.valid(lease) && (this.policy.isCurrent?.() ?? true),
      onNotDelivered: () => {response?.grant.revoke();this.pauseUndelivered(state, lease)}, onPlaybackEnd: () => {
      if (!this.valid(lease) || !started || ended) return
      ended = true
      response?.grant.naturalEnd()
      void this.serial(async () => {
        if (!this.valid(lease) || !this.checkpoint) return
        const next = await this.write('segment', { ...this.binding(this.checkpoint), segment_id: segment.segment_id, event: 'ended' })
        if (this.valid(lease)) {
          if(response&&next.status==='playback_finished'&&next.playback_id===state.playback_id&&next.source_version===state.source_version&&next.generation===state.generation)response.grant.confirmDelivery()
          else response?.grant.revoke()
          this.play(next, lease)
        } else response?.grant.revoke()
      }).catch(() => {response?.grant.revoke();if (this.valid(lease)) this.fail()})
    } })
  }

  dispose(): void { this.disposed = true; this.stopAudio() }
  forget(): void { this.dispose(); localStorage.removeItem(this.key) }
}
