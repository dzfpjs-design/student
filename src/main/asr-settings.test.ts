import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('electron', () => ({
  app: { getPath: () => '' },
  safeStorage: {
    isEncryptionAvailable: () => true,
    encryptString: (value: string) => Buffer.from(`protected:${value}`),
    decryptString: (value: Buffer) => value.toString().slice('protected:'.length),
  },
}))

import { CloudAsrSettingsStore } from './asr-settings'

describe('CloudAsrSettingsStore', () => {
  let directory = ''
  beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), 'hsc-asr-settings-')) })
  afterEach(async () => { await rm(directory, { recursive: true, force: true }) })

  it('persists a cloud ASR credential without plaintext', async () => {
    const store = new CloudAsrSettingsStore(directory)
    const config = { provider: 'dashscope' as const, apiKey: 'synthetic-9a068702a89289963dc5', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3-asr-flash' }
    await store.save(config)
    expect(await store.load()).toEqual(config)
    expect(await readFile(join(directory, 'asr-settings.json'), 'utf8')).not.toContain(config.apiKey)
  })
})
