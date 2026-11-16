import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { qwenOverlayFingerprint } from './qwen-key-overlay'
import type { ModelConfig } from './model-settings'
import type { CloudAsrConfig } from './asr-settings'
import type { VoiceConfig } from './voice-settings'
import type { TutorExplanationConfig } from './tutor-explanation-settings'

export type CloudProviderSnapshot = {
  modelConfig: ModelConfig | null
  asrConfig: CloudAsrConfig | null
  voiceConfig: VoiceConfig | null
  tutorExplanationConfig?: TutorExplanationConfig | null
}

// Opaque protected bytes only. Never return this fingerprint through IPC or logs.
export async function cloudProviderSettingsFingerprint(directory: string): Promise<string> {
  const hash = createHash('sha256')
  for (const name of ['model-settings.json', 'asr-settings.json', 'qwen-realtime-settings.json', 'voice-settings.json', 'voice-disabled.json', 'pet-audio-settings.bin', 'tutor-explanation-settings.json']) {
    hash.update(name + '\0')
    try {
      const bytes = await readFile(join(directory, name), { signal: AbortSignal.timeout(5_000) })
      hash.update('present:' + bytes.length + ':').update(bytes)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw new Error('CLOUD_PROVIDER_SETTINGS_UNREADABLE')
      hash.update('absent')
    }
    hash.update('\0')
  }
  hash.update(qwenOverlayFingerprint(directory))
  return hash.digest('hex')
}

export class RuntimeProviderRefresh {
  private tail: Promise<unknown> = Promise.resolve()
  private revision = 0
  private pendingMutations = 0
  private requestEpoch = 0
  private pendingReloads = 0
  private readonly providerRequests = new Set<AbortController>()

  private retireProviderRequests(): void {
    const previous = [...this.providerRequests]
    this.requestEpoch += 1
    for (const controller of previous) {
      controller.abort(new DOMException('语音连接已取消，请重新连接。', 'AbortError'))
    }
  }

  async withCurrentProviderRequest<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
    if (this.pendingMutations || this.pendingReloads) {
      throw new DOMException('语音连接已取消，请重新连接。', 'AbortError')
    }
    const epoch = this.requestEpoch
    const controller = new AbortController()
    this.providerRequests.add(controller)
    const current = (): void => {
      if (this.pendingMutations || this.pendingReloads || this.requestEpoch !== epoch) {
        controller.abort(new DOMException('语音连接已取消，请重新连接。', 'AbortError'))
      }
      controller.signal.throwIfAborted()
    }
    try {
      current()
      const result = await operation(controller.signal)
      current()
      return result
    } catch (error) {
      current()
      throw error
    } finally {
      this.providerRequests.delete(controller)
    }
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.tail.then(operation)
    this.tail = result.catch(() => undefined)
    return result
  }

  mutate<T>(operation: () => Promise<T>): Promise<T> {
    // A request invalidates a read immediately, even while queued behind it.
    this.revision += 1
    this.pendingMutations += 1
    this.retireProviderRequests()
    return this.enqueue(async () => {
      try { return await operation() }
      finally { this.pendingMutations -= 1; this.revision += 1; this.retireProviderRequests() }
    })
  }

  reload<Admission, Applied, Result>(dependencies: {
    assertCurrent: () => void
    fingerprint: () => Promise<string>
    loadSnapshot: () => Promise<CloudProviderSnapshot>
    admit: () => Admission
    apply: (snapshot: CloudProviderSnapshot, admission: Admission) => Promise<Applied>
    applied: (token: Applied) => boolean
    result: () => Result
  }): Promise<Result> {
    this.pendingReloads += 1
    this.retireProviderRequests()
    return this.enqueue(async () => {
      try {
        const revision = this.revision
        const current = (): void => {
          dependencies.assertCurrent()
          if (this.pendingMutations || this.revision !== revision) throw new Error('CLOUD_PROVIDER_REFRESH_CHANGED')
        }
        current()
        const admission = dependencies.admit()
        const before = await dependencies.fingerprint()
        current()
        const snapshot = await dependencies.loadSnapshot()
        const after = await dependencies.fingerprint()
        current()
        if (before !== after) throw new Error('CLOUD_PROVIDER_SETTINGS_CHANGED')
        const applied = await dependencies.apply(snapshot, admission)
        const final = await dependencies.fingerprint()
        current()
        if (before !== final || !dependencies.applied(applied)) throw new Error('CLOUD_PROVIDER_REFRESH_NOT_APPLIED')
        return dependencies.result()
      } finally {
        this.pendingReloads -= 1
        this.retireProviderRequests()
      }
    })
  }
}
