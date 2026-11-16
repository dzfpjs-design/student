import { realpathSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const security = vi.hoisted(() => ({ available: true }))
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {
  isEncryptionAvailable: () => security.available,
  encryptString: (value: string) => Buffer.from('synthetic-seal:' + Buffer.from(value).toString('base64')),
  decryptString: (value: Buffer) => { const raw = value.toString(); if (!raw.startsWith('synthetic-seal:')) throw Error('Damaged'); return Buffer.from(raw.slice(15), 'base64').toString() }
} }))
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from './pet-audio-settings'
import { PET_AUDIO_PURPOSE } from '../shared/petAudioSettings'
let directory: string, store: PetAudioSettingsStore
const config = { purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'synthetic-original', apiKey: 'synthetic-no-real-key' }
const command = () => ({ kind: 'save' as const, expectedRevision: 0, actionId: 'action-first', config: { ...config } })
beforeEach(() => { security.available = true; directory = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-settings-test-')); store = new PetAudioSettingsStore(directory) })
afterEach(() => rmSync(directory, { recursive: true, force: true }))
it('persists encrypted exact action through reopen, never activates or exposes key; clearing keeps a tombstone receipt', () => {
  const summary = store.execute(command())
  expect(summary).toMatchObject({ actionId: 'action-first', revision: 1, configured: true, enabled: false, state: 'awaiting_verified_tariff' })
  expect(JSON.stringify(summary)).not.toContain(config.apiKey)
  expect(readFileSync(join(directory, 'pet-audio-settings.bin')).toString()).not.toContain(config.apiKey)
  store = new PetAudioSettingsStore(directory)
  expect(store.execute({ kind: 'read' })).toEqual(summary)
  expect(() => store.execute(command())).toThrow('版本已变化')
  const cleared = store.execute({ kind: 'clear', expectedRevision: 1, actionId: 'clear-action' })
  expect(cleared).toMatchObject({ revision: 2, configured: false, actionId: 'clear-action' })
  expect(new PetAudioSettingsStore(directory).execute({ kind: 'read' })).toEqual(cleared)
  expect(readdirSync(directory)).toEqual(['pet-audio-settings.bin'])
})
it('refuses unknown tariffs, token-plan and wrong purpose rather than enabling with caller data', () => {
  for (const extra of [{ tariff_version: 'invented' }, { tokenPlan: 'free' }, { purpose: 'chat' }]) {
    expect(() => store.execute({ ...command(), config: { ...config, ...extra } as typeof config })).toThrow()
  }
  expect(store.execute({ kind: 'read' }).revision).toBe(0)
})
it('keeps key only on the exact origin and does not migrate region credentials', () => {
  store.execute(command())
  expect(() => store.execute({ ...command(), actionId: 'new-action', expectedRevision: 1, config: { ...config,
    apiKey: '', baseUrl: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1' } })).toThrow('更换区域')
  expect(store.execute({ ...command(), actionId: 'new-action', expectedRevision: 1, config: { ...config, apiKey: '', model: 'synthetic-new' } }).revision).toBe(2)
})
it.each(['https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1', 'http://127.0.0.1:9000/v1',
  'https://dashscope.aliyuncs.com/compatible-mode/v1?q=1', 'https://user@dashscope.aliyuncs.com/compatible-mode/v1'])('refuses credential destination %s', baseUrl => {
  expect(() => store.execute({ ...command(), config: { ...config, baseUrl } })).toThrow()
  expect(readdirSync(directory)).toEqual([])
})
it('fails closed on damaged history and unavailable encryption without replacing prior bytes', () => {
  store.execute(command()); const path = join(directory, 'pet-audio-settings.bin'), before = readFileSync(path)
  security.available = false
  expect(() => store.execute({ ...command(), expectedRevision: 1 })).toThrow('安全存储')
  expect(readFileSync(path)).toEqual(before)
  security.available = true; writeFileSync(path, 'damaged')
  expect(() => store.execute(command())).toThrow('损坏')
  expect(readFileSync(path, 'utf8')).toBe('damaged')
})
it('gates IPC owner/child and retains CAS while another valid parent saves during authority await', async () => {
  let release!: () => void
  const wait = new Promise<void>(resolve => { release = resolve })
  const handler = createPetAudioSettingsHandler(store, async (sender, child, token) => {
    if (sender !== 7 || child !== 'primary' || token !== 'parent') throw Error('denied')
    await wait
  })
  await expect(handler({ sender: { id: 7 } }, 'other', 'parent', command())).rejects.toThrow('denied')
  const first = handler({ sender: { id: 7 } }, 'primary', 'parent', command())
  store.execute({ ...command(), actionId: 'concurrent-action' }); release()
  await expect(first).rejects.toThrow('版本已变化')
  expect(store.execute({ kind: 'read' }).actionId).toBe('concurrent-action')
})
