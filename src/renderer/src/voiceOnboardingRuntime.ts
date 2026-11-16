import { validateDeferredVoiceOnboardingProgress } from './voiceInteractionHandoff'

export type VoiceOnboardingStep = 'wake_practice' | 'study_practice' | 'document_practice' | 'finish_practice' | 'completed' | 'deferred'
export type VoiceOnboardingProgress = { childId: string; version: 1; step: VoiceOnboardingStep; updatedAt: string }
export type VoiceOnboardingBridge = {
  getVoiceOnboarding: (childId: string) => Promise<VoiceOnboardingProgress | null>
  saveVoiceOnboarding: (progress: VoiceOnboardingProgress) => Promise<VoiceOnboardingProgress>
}
export type VoiceOnboardingSnapshot = {
  phase: 'loading' | 'naming' | 'active' | 'saving' | 'error' | 'idle'
  step: VoiceOnboardingStep | null
  prompt: string
  silent: boolean
}

const steps: VoiceOnboardingStep[] = ['wake_practice', 'study_practice', 'document_practice', 'finish_practice', 'completed', 'deferred']
const compact = (value: string): string => value.replace(/[\s，。！？、,.!?；;：:]/g, '')
export const isVoiceTutorialRequest = (text: string): boolean => /^(重新教程|重新学习教程|开始教程|语音教程|再学一遍)$/.test(compact(text))

export const deferredEntryReminder = '刚才带进来的请求还没有执行，请重新说一次。'
const unnamedDeferredPrompt = '好，练习以后再学。暂时还没设置唤醒名字，你可以直接说学习请求；想起名字时说“重新教程”。'

// Only literal, reviewed tutorial copy may use the configured authorized voice.
// Names, candidate confirmations and arbitrary input default to local synthesis.
export function voiceOnboardingSpeechPolicy(message: string): { localOnly: boolean } {
  const fixed = ['study_practice', 'document_practice', 'finish_practice', 'completed', 'deferred'] as const
  const prompts = [...fixed.map((step) => voiceOnboardingPrompt(step, '')), unnamedDeferredPrompt]
  return { localOnly: !prompts.some((prompt) => message === prompt || message === prompt + deferredEntryReminder) }
}

export function voiceOnboardingPrompt(step: VoiceOnboardingStep, wakeWord: string): string {
  return {
    wake_practice: `名字“${wakeWord}”已经记好了。等我说完，请单独叫我一声。想跳过，可以说“${wakeWord}，跳过教程”。`,
    study_practice: '听到你叫我了。练习说一句：今天数学学了分数，作业是做三道题。这句只练习，不会记成作业。',
    document_practice: '说清楚了。再练习说：帮我看一下这道题。这次不会打开高拍仪。',
    finish_practice: '会求助了。最后练习说：暂停学习，或者结束学习。这里只练习；真要停止所有设备，请说“停止全部监听”。',
    completed: '练习完成。以后直接叫我就能聊天。想再练习，说“重新教程”。',
    deferred: '好，以后再学。想继续时说“重新教程”。',
  }[step]
}

function validate(value: unknown, childId: string): VoiceOnboardingProgress {
  const record = value as Partial<VoiceOnboardingProgress> | null
  if (!record || record.childId !== childId || record.version !== 1 || !steps.includes(record.step as VoiceOnboardingStep)
    || typeof record.updatedAt !== 'string' || !Number.isFinite(Date.parse(record.updatedAt))) throw new Error('INVALID_ONBOARDING_RECEIPT')
  return record as VoiceOnboardingProgress
}

/** Cloud decisions drive progress; the desktop store remains the system of record. */
export class VoiceOnboardingRuntime {
  snapshot: VoiceOnboardingSnapshot = { phase: 'loading', step: null, prompt: '正在恢复语音练习进度。', silent: true }
  private generation = 0
  private disposed = false
  private pending: VoiceOnboardingStep | null = null
  private preparedDeferral: VoiceOnboardingProgress | null = null
  private inFlight = false
  private stopped = false
  private deferAfterSave: { silent: boolean } | null = null
  private settled = new Set<() => void>()

  constructor(private readonly childId: string, private wakeWord: string, private readonly bridge: VoiceOnboardingBridge,
    private readonly changed: (snapshot: VoiceOnboardingSnapshot) => void, private readonly grade = '') {}

  private namingPrompt(): string {
    const gradePrefix = this.grade ? `已经进入并固定到${this.grade}学习档案。` : ''
    return `${gradePrefix}现在先给我起个短名字吧，用两到十二个字，例如“小知”。请直接说出来，我会再请你确认后保存。`
  }

  private publish(snapshot: VoiceOnboardingSnapshot): void {
    if (this.disposed || this.stopped) return
    this.snapshot = snapshot
    this.changed(snapshot)
  }

  async load(): Promise<void> {
    if (this.inFlight || this.disposed || this.stopped) return
    this.inFlight = true
    const generation = this.generation
    let handedToSave = false
    this.publish({ ...this.snapshot, phase: 'loading', silent: true })
    try {
      const value = await this.bridge.getVoiceOnboarding(this.childId)
      if (generation !== this.generation) return
      const saved = value === null ? null : validate(value, this.childId)
      if (this.deferAfterSave) return
      if (saved && ['completed', 'deferred'].includes(saved.step)) {
        this.publish({ phase: 'idle', step: saved.step, prompt: this.wakeWord ? '' : unnamedDeferredPrompt, silent: Boolean(this.wakeWord) })
      } else if (!this.wakeWord) {
        this.publish({ phase: 'naming', step: null, prompt: this.namingPrompt(), silent: false })
      } else if (saved) {
        this.publish({ phase: ['completed', 'deferred'].includes(saved.step) ? 'idle' : 'active', step: saved.step,
          prompt: voiceOnboardingPrompt(saved.step, this.wakeWord), silent: ['completed', 'deferred'].includes(saved.step) })
      } else {
        this.inFlight = false
        handedToSave = true
        await this.save('wake_practice')
      }
    } catch {
      if (generation === this.generation) this.publish({ ...this.snapshot, phase: 'error', prompt: '教程进度没能读到。请说“重试”；练习不会提交成任务。', silent: false })
    } finally { if (!handedToSave) await this.finishSave(generation) }
  }

  async named(wakeWord: string): Promise<void> {
    this.wakeWord = wakeWord
    if (this.snapshot.phase === 'naming') await this.save('wake_practice')
  }

  private async save(step: VoiceOnboardingStep, silent = false): Promise<void> {
    if (this.inFlight || this.disposed || this.stopped) return
    this.inFlight = true
    this.pending = step
    const generation = this.generation
    this.publish({ ...this.snapshot, phase: 'saving', prompt: '正在保存练习进度。', silent: true })
    try {
      const requested: VoiceOnboardingProgress = step === 'deferred' && this.preparedDeferral
        ? { ...this.preparedDeferral } : { childId: this.childId, version: 1, step, updatedAt: new Date().toISOString() }
      const requestedAt = requested.updatedAt
      const saved = validate(await this.bridge.saveVoiceOnboarding(requested), this.childId)
      if (generation !== this.generation) return
      if (saved.step !== step || this.preparedDeferral && step === 'deferred' && saved.updatedAt !== requestedAt) throw new Error('ONBOARDING_STEP_NOT_SAVED')
      const preparedSaved = step === 'deferred' && this.preparedDeferral !== null
      // A skip heard during this exact deferral cannot replace its server-bound timestamp.
      if (preparedSaved && this.deferAfterSave) {
        silent = silent || this.deferAfterSave.silent
        this.deferAfterSave = null
      }
      this.pending = null
      this.preparedDeferral = null
      if (!this.deferAfterSave) {
        const naming = step === 'wake_practice' && !this.wakeWord
        this.publish({ phase: naming ? 'naming' : ['completed', 'deferred'].includes(step) ? 'idle' : 'active', step: naming ? null : step,
          prompt: naming ? this.namingPrompt()
            : step === 'deferred' && !this.wakeWord ? unnamedDeferredPrompt : voiceOnboardingPrompt(step, this.wakeWord), silent })
      }
    } catch {
      if (generation === this.generation) this.publish({ ...this.snapshot, phase: 'error', prompt: '这一步还没保存好。请说“重试”，我不会提前进入下一步。', silent: false })
    } finally { await this.finishSave(generation) }
  }

  private async finishSave(generation: number): Promise<void> {
    if (generation !== this.generation) return
    this.inFlight = false
    const deferred = this.deferAfterSave
    this.deferAfterSave = null
    if (deferred) await this.save('deferred', deferred.silent)
    if (!this.inFlight) { this.settled.forEach((resolve) => resolve()); this.settled.clear() }
  }

  async whenSettled(): Promise<void> {
    while (this.inFlight && !this.disposed && !this.stopped) await new Promise<void>((resolve) => this.settled.add(resolve))
  }

  async deferPrepared(progress: VoiceOnboardingProgress): Promise<boolean> {
    let prepared: VoiceOnboardingProgress
    try { prepared = { ...validateDeferredVoiceOnboardingProgress(progress, this.childId) } }
    catch { return false }
    await this.whenSettled()
    if (this.disposed || this.stopped || this.preparedDeferral && this.preparedDeferral.updatedAt !== prepared.updatedAt) return false
    this.preparedDeferral = prepared
    await this.save('deferred')
    return !this.disposed && !this.stopped && this.pending === null && this.preparedDeferral === null
      && this.snapshot.phase === 'idle' && this.snapshot.step === 'deferred'
  }

  async applyAction(action: 'skip' | 'repeat' | 'advance', silent = false): Promise<boolean> {
    await this.whenSettled()
    if (this.disposed || this.stopped) return false
    if (action === 'skip') await this.save('deferred', silent)
    else if (action === 'repeat') this.publish({ ...this.snapshot, silent: false })
    else if (this.snapshot.phase === 'error') {
      if (this.pending) await this.save(this.pending)
      else await this.load()
    } else if (this.snapshot.phase === 'active' && this.snapshot.step) {
      const next = steps[steps.indexOf(this.snapshot.step) + 1]
      if (next && next !== 'deferred') await this.save(next)
    }
    return this.snapshot.phase !== 'error'
  }

  async restart(): Promise<boolean> {
    await this.whenSettled()
    if (this.disposed || this.stopped) return false
    // Even an unnamed restart must replace deferred durably before prompting.
    // load() interprets this marker as naming while no wake name exists.
    await this.save('wake_practice')
    return !this.disposed && !this.stopped && this.pending === null
      && (this.snapshot.phase === 'naming' || (this.snapshot.phase === 'active' && this.snapshot.step === 'wake_practice'))
  }

  async wakeDetected(): Promise<void> {
    if (this.snapshot.phase === 'active' && this.snapshot.step === 'wake_practice') await this.save('study_practice')
  }

  async receive(text: string): Promise<boolean> {
    if (this.disposed || this.stopped) return true
    const heard = compact(text)
    if (this.inFlight) {
      if (/^(跳过|跳过教程|以后再学|稍后再学|停止|停止教程|退出教程)$/.test(heard)) this.deferAfterSave = { silent: /停止/.test(heard) }
      return true
    }
    if (/^(跳过|跳过教程|以后再学|稍后再学|停止|停止教程|退出教程)$/.test(heard)) {
      if (this.snapshot.phase === 'idle') return false
      await this.save('deferred', /停止/.test(heard))
      return true
    }
    if (isVoiceTutorialRequest(text)) {
      if (!this.wakeWord) {
        this.publish({ phase: 'naming', step: null, prompt: '先给我起个短名字吧。我会请你确认后再保存。', silent: false })
      } else await this.save('wake_practice')
      return true
    }
    if (this.snapshot.phase === 'error') {
      if (/^(重试|重试保存|再试一次)$/.test(heard)) {
        if (this.pending) await this.save(this.pending)
        else await this.load()
      }
      return true
    }
    if (this.snapshot.phase === 'naming') return false
    if (this.snapshot.phase === 'idle') return false
    const step = this.snapshot.step
    if (step === 'study_practice' && /(?:今天|课堂|课上).*(?:学|讲)/.test(heard) && /作业.{2,}/.test(heard)) await this.save('document_practice')
    else if (step === 'document_practice' && /(?:帮我|请你|能不能).*(?:看|讲|拍).*(?:题|页)|(?:高拍仪).*(?:帮|看|题)/.test(heard)) await this.save('finish_practice')
    else if (step === 'finish_practice' && /^(暂停学习|暂停一下|休息一下|结束学习|今天学完了)$/.test(heard)) await this.save('completed')
    else this.publish({ ...this.snapshot, prompt: step ? voiceOnboardingPrompt(step, this.wakeWord) : this.snapshot.prompt, silent: false })
    return true
  }

  suspend(): void { this.stopped = true; this.generation += 1; this.settled.forEach((resolve) => resolve()); this.settled.clear() }
  dispose(): void { this.disposed = true; this.generation += 1; this.settled.forEach((resolve) => resolve()); this.settled.clear() }
}

// One queue owns tutorial routing and its handoff to Agent execution. Cancellation
// invalidates queued work; it never retries a request with an unknown outcome.
export class VoiceInputQueue {
  private active = false
  private controller = new AbortController()
  private jobs: Array<{ run: (signal: AbortSignal) => Promise<void>; resolve: () => void; reject: (cause: unknown) => void }> = []
  get pendingCount(): number { return this.jobs.length }
  get running(): boolean { return this.active }

  enqueue(run: (signal: AbortSignal) => Promise<void>): Promise<void> {
    const result = new Promise<void>((resolve, reject) => { this.jobs.push({ run, resolve, reject }) })
    void this.drain()
    return result
  }

  private async drain(): Promise<void> {
    if (this.active) return
    this.active = true
    while (this.jobs.length) {
      const job = this.jobs.shift()!
      const signal = this.controller.signal
      try { await job.run(signal); job.resolve() } catch (cause) { job.reject(cause) }
    }
    this.active = false
  }

  cancel(): void {
    this.controller.abort()
    this.controller = new AbortController()
    this.jobs.splice(0).forEach((job) => job.resolve())
  }
}

/** Bound read-only routing even when a transport ignores AbortSignal. */
export async function resolveVoiceInteraction<T>(request: (signal: AbortSignal) => Promise<T>, signal: AbortSignal, timeoutMs = 20_000): Promise<T> {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  let abort!: () => void
  const cancelled = new Promise<never>((_resolve, reject) => {
    abort = () => { controller.abort(); reject(new DOMException('Voice interaction cancelled', 'AbortError')) }
    if (signal.aborted) abort()
    else signal.addEventListener('abort', abort, { once: true })
    timer = setTimeout(() => { controller.abort(); reject(new Error('VOICE_INTERACTION_TIMEOUT')) }, timeoutMs)
  })
  try { return await Promise.race([cancelled, signal.aborted ? cancelled : request(controller.signal)]) }
  finally { clearTimeout(timer); signal.removeEventListener('abort', abort) }
}
