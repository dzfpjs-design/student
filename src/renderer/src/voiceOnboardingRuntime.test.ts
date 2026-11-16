import { describe, expect, it, vi } from 'vitest'
import { deferredEntryReminder, voiceOnboardingPrompt, voiceOnboardingSpeechPolicy, VoiceOnboardingRuntime, type VoiceOnboardingProgress } from './voiceOnboardingRuntime'

const record = (step: VoiceOnboardingProgress['step']): VoiceOnboardingProgress => ({ childId: 'child-a', version: 1, step, updatedAt: '2026-09-05T12:00:00Z' })
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function fixture(saved: VoiceOnboardingProgress | null = null, name = '小知') {
  const changed = vi.fn()
  const bridge = { getVoiceOnboarding: vi.fn().mockResolvedValue(saved), saveVoiceOnboarding: vi.fn(async (value: VoiceOnboardingProgress) => value) }
  const runtime = new VoiceOnboardingRuntime('child-a', name, bridge, changed)
  return { runtime, bridge, changed }
}

describe('voice tutorial progress', () => {
  it('persists the exact prepared deferral before advancing and retains its timestamp after disk failure', async () => {
    const { runtime, bridge } = fixture(record('study_practice'))
    await runtime.load()
    const progress: VoiceOnboardingProgress = { ...record('deferred'), updatedAt: '2026-10-15T10:20:30.123Z' }
    bridge.saveVoiceOnboarding.mockRejectedValueOnce(new Error('disk full'))
    expect(await runtime.deferPrepared(progress)).toBe(false)
    expect(runtime.snapshot).toMatchObject({ phase: 'error', step: 'study_practice' })
    await runtime.receive('重试')
    expect(runtime.snapshot).toMatchObject({ phase: 'idle', step: 'deferred' })
    expect(bridge.saveVoiceOnboarding.mock.calls.map(([value]) => value)).toEqual([progress, progress])
  })

  it('rejects a mismatched prepared timestamp receipt and does not advance before the exact receipt', async () => {
    const { runtime, bridge } = fixture(record('study_practice'))
    await runtime.load()
    const progress: VoiceOnboardingProgress = { ...record('deferred'), updatedAt: '2026-10-15T10:20:30.123Z' }
    const pending = deferred<VoiceOnboardingProgress>()
    bridge.saveVoiceOnboarding.mockReturnValueOnce(pending.promise)
    const applying = runtime.deferPrepared(progress)
    await Promise.resolve()
    expect(runtime.snapshot).toMatchObject({ phase: 'saving', step: 'study_practice' })
    pending.resolve({ ...progress, updatedAt: '2026-10-15T10:20:30.124Z' })
    expect(await applying).toBe(false)
    expect(runtime.snapshot).toMatchObject({ phase: 'error', step: 'study_practice' })
    expect(await runtime.deferPrepared(progress)).toBe(true)
    expect(bridge.saveVoiceOnboarding).toHaveBeenLastCalledWith(progress)
  })

  it.each([{ childId: 'child-b' }, { step: 'completed' }, { updatedAt: '2026-10-15T10:20:30Z' }])('rejects invalid prepared progress %j without writing', async changed => {
    const { runtime, bridge } = fixture(record('study_practice'))
    await runtime.load()
    expect(await runtime.deferPrepared({ ...record('deferred'), ...changed } as VoiceOnboardingProgress)).toBe(false)
    expect(bridge.saveVoiceOnboarding).not.toHaveBeenCalled()
    expect(runtime.snapshot.step).toBe('study_practice')
  })

  it('does not overwrite the server timestamp when another skip arrives during its save', async () => {
    const { runtime, bridge } = fixture(record('study_practice'))
    await runtime.load()
    const progress: VoiceOnboardingProgress = { ...record('deferred'), updatedAt: '2026-10-15T10:20:30.123Z' }
    const pending = deferred<VoiceOnboardingProgress>()
    bridge.saveVoiceOnboarding.mockReturnValueOnce(pending.promise)
    const applying = runtime.deferPrepared(progress)
    await Promise.resolve()
    await runtime.receive('跳过教程')
    pending.resolve(progress)
    expect(await applying).toBe(true)
    expect(bridge.saveVoiceOnboarding).toHaveBeenCalledExactlyOnceWith(progress)
  })

  it.each(['小知', ''])('persists a restarted deferred tutorial and restores its phase after reload: %s', async (name) => {
    const { runtime, bridge } = fixture(record('deferred'), name)
    await runtime.load()
    const pending = deferred<VoiceOnboardingProgress>()
    bridge.saveVoiceOnboarding.mockReturnValueOnce(pending.promise)
    const restarting = runtime.restart()
    await Promise.resolve()
    expect(runtime.snapshot.phase).toBe('saving')
    pending.resolve(record('wake_practice'))
    expect(await restarting).toBe(true)
    expect(runtime.snapshot).toMatchObject({ phase: name ? 'active' : 'naming', step: name ? 'wake_practice' : null })
    expect(bridge.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
    const restored = fixture(record('wake_practice'), name)
    await restored.runtime.load()
    expect(restored.runtime.snapshot.phase).toBe(runtime.snapshot.phase)
    expect(restored.bridge.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it('does not report a restart when storage fails or privacy cancels its late receipt', async () => {
    const { runtime, bridge } = fixture(record('deferred'))
    await runtime.load()
    bridge.saveVoiceOnboarding.mockRejectedValueOnce(new Error('disk full'))
    expect(await runtime.restart()).toBe(false)
    expect(runtime.snapshot.phase).toBe('error')
    const pending = deferred<VoiceOnboardingProgress>()
    bridge.saveVoiceOnboarding.mockReturnValueOnce(pending.promise)
    const restarting = runtime.restart()
    await Promise.resolve()
    runtime.suspend()
    pending.resolve(record('wake_practice'))
    expect(await restarting).toBe(false)
  })
  it('permits only exact fixed tutorial copy to request the authorized voice', () => {
    for (const step of ['study_practice', 'document_practice', 'finish_practice', 'completed', 'deferred'] as const) {
      const prompt = voiceOnboardingPrompt(step, '')
      expect(voiceOnboardingSpeechPolicy(prompt)).toEqual({ localOnly: false })
      expect(voiceOnboardingSpeechPolicy(prompt + deferredEntryReminder)).toEqual({ localOnly: false })
      expect(voiceOnboardingSpeechPolicy(prompt + '我家的秘密')).toEqual({ localOnly: true })
    }
    expect(voiceOnboardingSpeechPolicy(voiceOnboardingPrompt('wake_practice', '私有名字'))).toEqual({ localOnly: true })
    expect(voiceOnboardingSpeechPolicy('我听到的唤醒名字是“私有名字”。')).toEqual({ localOnly: true })
    expect(voiceOnboardingSpeechPolicy('今天数学学了分数，作业是做三道题')).toEqual({ localOnly: true })
  })
  it('starts naming without saving progress before the name is confirmed', async () => {
    const { runtime, bridge } = fixture(null, '')
    await runtime.load()
    expect(runtime.snapshot.phase).toBe('naming')
    expect(bridge.saveVoiceOnboarding).not.toHaveBeenCalled()
    await runtime.named('小知')
    expect(runtime.snapshot.step).toBe('wake_practice')
  })

  it('keeps an existing name and offers a skippable practice', async () => {
    const { runtime } = fixture()
    await runtime.load()
    expect(runtime.snapshot.phase).toBe('active')
    expect(runtime.snapshot.prompt).toContain('小知')
    expect(runtime.snapshot.prompt).toContain('跳过')
  })

  it('requires a wake event instead of accepting the wake name as transcript evidence', async () => {
    const { runtime, bridge } = fixture(record('wake_practice'))
    await runtime.load()
    await runtime.receive('小知')
    expect(bridge.saveVoiceOnboarding).not.toHaveBeenCalled()
    await runtime.wakeDetected()
    expect(runtime.snapshot.step).toBe('study_practice')
  })

  it('waits for the saved receipt and does not double-submit wake events', async () => {
    const { runtime, bridge } = fixture(record('wake_practice'))
    await runtime.load()
    const saved = deferred<VoiceOnboardingProgress>()
    bridge.saveVoiceOnboarding.mockReturnValueOnce(saved.promise)
    const first = runtime.wakeDetected()
    await runtime.wakeDetected()
    expect(runtime.snapshot.step).toBe('wake_practice')
    expect(runtime.snapshot.phase).toBe('saving')
    expect(bridge.saveVoiceOnboarding).toHaveBeenCalledTimes(1)
    saved.resolve(record('study_practice'))
    await first
    expect(runtime.snapshot.step).toBe('study_practice')
  })

  it('practices classroom, homework, camera help and finishing without saving any practice text', async () => {
    const { runtime, bridge } = fixture(record('study_practice'))
    await runtime.load()
    await runtime.receive('今天数学学了分数')
    expect(runtime.snapshot.step).toBe('study_practice')
    await runtime.receive('今天数学学了分数，作业是做三道题。')
    await runtime.receive('帮我看一下这道题')
    await runtime.receive('结束学习')
    expect(runtime.snapshot).toMatchObject({ phase: 'idle', step: 'completed' })
    expect(bridge.saveVoiceOnboarding.mock.calls.map(([saved]) => saved.step)).toEqual(['document_practice', 'finish_practice', 'completed'])
    for (const [saved] of bridge.saveVoiceOnboarding.mock.calls) expect(Object.keys(saved).sort()).toEqual(['childId', 'step', 'updatedAt', 'version'])
  })

  it('retains the last confirmed step after save failure and retries the same target', async () => {
    const { runtime, bridge } = fixture(record('wake_practice'))
    await runtime.load()
    bridge.saveVoiceOnboarding.mockRejectedValueOnce(new Error('disk full'))
    await runtime.wakeDetected()
    expect(runtime.snapshot).toMatchObject({ phase: 'error', step: 'wake_practice' })
    await runtime.receive('重试')
    expect(runtime.snapshot.step).toBe('study_practice')
  })

  it.each(['study_practice', 'document_practice', 'finish_practice', 'completed', 'deferred'] as const)('restores %s without resetting progress', async (step) => {
    const { runtime, bridge } = fixture(record(step))
    await runtime.load()
    expect(runtime.snapshot.step).toBe(step)
    expect(bridge.saveVoiceOnboarding).not.toHaveBeenCalled()
  })

  it('keeps an unnamed deferred tutorial deferred on reload', async () => {
    const { runtime } = fixture(record('deferred'), '')
    await runtime.load()
    expect(runtime.snapshot.phase).toBe('idle')
    expect(runtime.snapshot.prompt).toContain('可以直接说学习请求')
    expect(await runtime.receive('今天的作业')).toBe(false)
    await runtime.receive('重新教程')
    expect(runtime.snapshot.phase).toBe('naming')
  })

  it('serializes a stop requested during save and never announces the next exercise', async () => {
    const { runtime, bridge, changed } = fixture(record('wake_practice'))
    await runtime.load()
    const saved = deferred<VoiceOnboardingProgress>()
    bridge.saveVoiceOnboarding.mockReturnValueOnce(saved.promise)
    const running = runtime.wakeDetected()
    await runtime.receive('停止教程')
    saved.resolve(record('study_practice'))
    await running
    expect(runtime.snapshot).toMatchObject({ phase: 'idle', step: 'deferred', silent: true })
    expect(changed.mock.calls.some(([snapshot]) => snapshot.phase === 'active' && snapshot.step === 'study_practice')).toBe(false)
  })

  it.each(['suspend', 'dispose'] as const)('ignores late save callbacks after %s', async (method) => {
    const { runtime, bridge, changed } = fixture(record('wake_practice'))
    await runtime.load()
    const saved = deferred<VoiceOnboardingProgress>()
    bridge.saveVoiceOnboarding.mockReturnValueOnce(saved.promise)
    const running = runtime.wakeDetected()
    runtime[method]()
    const count = changed.mock.calls.length
    saved.resolve(record('study_practice'))
    await running
    expect(changed).toHaveBeenCalledTimes(count)
  })

  it('does not replace corrupt or cross-child progress with defaults', async () => {
    const { runtime, bridge } = fixture({ ...record('study_practice'), childId: 'child-b' })
    await runtime.load()
    expect(runtime.snapshot.phase).toBe('error')
    expect(bridge.saveVoiceOnboarding).not.toHaveBeenCalled()
  })
})
