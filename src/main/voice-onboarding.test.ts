import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { VoiceOnboardingStore, validateVoiceOnboarding, type VoiceOnboarding } from './voice-onboarding'

describe('voice tutorial persistence', () => {
  let directory: string
  const state: VoiceOnboarding = { childId: 'child-primary-4', version: 1, step: 'wake_practice', updatedAt: '2026-09-05T00:00:00Z' }
  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'hsc-onboarding-')) })
  afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

  it('resumes across store restart and isolates concurrent children', async () => {
    const store = new VoiceOnboardingStore(directory)
    expect(await store.load(state.childId)).toBeNull()
    await Promise.all([store.save(state), store.save({ ...state, childId: 'child-junior-3', step: 'completed' })])
    const resumed = new VoiceOnboardingStore(directory)
    expect((await resumed.load(state.childId))?.step).toBe('wake_practice')
    expect((await resumed.load('child-junior-3'))?.step).toBe('completed')
  })

  it('serializes progress writes and stores no transcript or audio', async () => {
    const store = new VoiceOnboardingStore(directory)
    await Promise.all([store.save(state), store.save({ ...state, step: 'study_practice' }), store.save({ ...state, step: 'deferred' })])
    expect((await store.load(state.childId))?.step).toBe('deferred')
    expect(Object.keys(JSON.parse(await readFile(join(directory, 'voice-onboarding', `${state.childId}.json`), 'utf8'))).sort())
      .toEqual(['childId', 'step', 'updatedAt', 'version'])
  })

  it('fails closed without erasing damaged progress', async () => {
    const store = new VoiceOnboardingStore(directory)
    await store.save(state)
    const path = join(directory, 'voice-onboarding', `${state.childId}.json`)
    await writeFile(path, 'damaged', 'utf8')
    await expect(store.load(state.childId)).rejects.toThrow('原记录已保留')
    expect(await readFile(path, 'utf8')).toBe('damaged')
  })

  it('rejects traversal, invalid steps, versions, dates and mismatched records', async () => {
    const store = new VoiceOnboardingStore(directory)
    for (const childId of ['../escape', '..', 'x/y', '']) await expect(store.load(childId)).rejects.toThrow('标识')
    expect(() => validateVoiceOnboarding({ ...state, version: 2 } as unknown as VoiceOnboarding)).toThrow('进度无效')
    expect(() => validateVoiceOnboarding({ ...state, step: 'unknown' } as unknown as VoiceOnboarding)).toThrow('进度无效')
    expect(() => validateVoiceOnboarding({ ...state, updatedAt: 'not-a-date' })).toThrow('进度无效')
    await store.save(state)
    await writeFile(join(directory, 'voice-onboarding', `${state.childId}.json`), JSON.stringify({ ...state, childId: 'child-junior-3' }), 'utf8')
    await expect(store.load(state.childId)).rejects.toThrow('原记录已保留')
  })
})
