import { randomBytes } from 'node:crypto'
import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

export type VoiceOnboarding = {
  childId: string
  version: 1
  step: 'wake_practice' | 'study_practice' | 'document_practice' | 'finish_practice' | 'completed' | 'deferred'
  updatedAt: string
}

const steps = new Set(['wake_practice', 'study_practice', 'document_practice', 'finish_practice', 'completed', 'deferred'])

function childKey(value: string): string {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9._:-]{2,120}$/.test(value) || value === '..') throw new Error('孩子配置标识无效。')
  return encodeURIComponent(value)
}

export function validateVoiceOnboarding(value: VoiceOnboarding): VoiceOnboarding {
  childKey(value?.childId)
  if (value.version !== 1 || !steps.has(value.step) || !Number.isFinite(Date.parse(value.updatedAt))) throw new Error('语音教程进度无效。')
  return { childId: value.childId, version: 1, step: value.step, updatedAt: new Date(value.updatedAt).toISOString() }
}

// One atomic file per child avoids cross-child read/modify/write data loss.
export class VoiceOnboardingStore {
  private readonly directory: string
  private writes = new Map<string, Promise<unknown>>()

  constructor(userDataPath: string) { this.directory = join(userDataPath, 'voice-onboarding') }

  async load(childId: string): Promise<VoiceOnboarding | null> {
    const path = join(this.directory, `${childKey(childId)}.json`)
    await this.writes.get(childId)?.catch(() => undefined)
    try {
      const value = validateVoiceOnboarding(JSON.parse(await readFile(path, 'utf8')) as VoiceOnboarding)
      if (value.childId !== childId) throw new Error('教程进度与孩子档案不匹配。')
      return value
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
      throw new Error('语音教程进度暂时无法读取，原记录已保留。')
    }
  }

  async save(value: VoiceOnboarding): Promise<VoiceOnboarding> {
    const normalized = validateVoiceOnboarding(value)
    const pending = (this.writes.get(normalized.childId) ?? Promise.resolve()).catch(() => undefined).then(async () => {
      await mkdir(this.directory, { recursive: true })
      const path = join(this.directory, `${childKey(normalized.childId)}.json`)
      const temporary = `${path}.${randomBytes(8).toString('hex')}.tmp`
      try {
        await writeFile(temporary, JSON.stringify(normalized), { encoding: 'utf8', flag: 'wx', mode: 0o600 })
        await rename(temporary, path)
      } finally { await unlink(temporary).catch(() => undefined) }
      return normalized
    })
    this.writes.set(normalized.childId, pending)
    try { return await pending } finally {
      if (this.writes.get(normalized.childId) === pending) this.writes.delete(normalized.childId)
    }
  }
}
