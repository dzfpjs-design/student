import { realpathSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {
  isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(Buffer.from(s).toString('base64')),
  decryptString: (b: Buffer) => Buffer.from(b.toString(), 'base64').toString()
} }))
import { PetAudioSettingsStore, createPetAudioSettingsHandler } from './pet-audio-settings'
import { buildLocalServiceChildEnvironment } from './local-service'
import { PET_AUDIO_PURPOSE } from '../shared/petAudioSettings'
import { currentPetAudioTariff, PET_AUDIO_TARIFF } from '../shared/petAudioTariffs'
afterEach(() => vi.restoreAllMocks())
beforeEach(() => vi.spyOn(Date, 'now').mockReturnValue(Date.parse(PET_AUDIO_TARIFF.verifiedAt) + 3600000))
it('turns explicitly accepted fixed-model catalog settings into dedicated backend environment, without granting child permission', () => {
  const dir = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-enable-'))
  try {
    const store = new PetAudioSettingsStore(dir)
    const saved = store.execute({ kind: 'save', expectedRevision: 0, actionId: 'enable-first', config: {
      purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: PET_AUDIO_TARIFF.model, apiKey: 'synthetic-not-a-key',
      enabled: true, tariffId: PET_AUDIO_TARIFF.id
    } })
    expect(saved.enabled).toBe(true)
    const env = buildLocalServiceChildEnvironment({ HSC_PET_AUDIO_API_KEY: 'untrusted' }, {
      sessionToken: 's', dataEncryptionKey: 'k', dataDir: '/tmp/synthetic', databasePath: '/tmp/synthetic/db',
      localModelDir: '/tmp/models', modelConfig: null, voiceConfig: null, petAudioConfig: store.runtimeConfig()
    })
    expect(env.HSC_PET_AUDIO_ENABLED).toBe('true')
    expect(env.HSC_PET_AUDIO_API_KEY).toBe('synthetic-not-a-key')
    expect(env.HSC_PET_AUDIO_INPUT_SECOND_MICROS).toBe('371')
    expect(env.HSC_PET_AUDIO_OUTPUT_TOKEN_MICROS).toBe('40')
    expect(Object.keys(env).filter(k => /GRANT|TOKEN_PLAN/.test(k))).toEqual([])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

it('rejects alias, wrong region and expired catalog; an expired saved record reopens disabled without erasing its key', () => {
  const dir = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-catalog-'))
  try {
    const store = new PetAudioSettingsStore(dir)
    const config = { purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: PET_AUDIO_TARIFF.model, apiKey: 'synthetic-key-only', enabled: true, tariffId: PET_AUDIO_TARIFF.id }
    for (const bad of [{ model: 'qwen3-omni-flash' }, { baseUrl: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1' }, { tariffId: 'unknown' }]) {
      expect(() => store.execute({ kind: 'save', actionId: 'bad-action', expectedRevision: 0, config: { ...config, ...bad } })).toThrow('尚未核验')
    }
    store.execute({ kind: 'save', actionId: 'saved-before-expiry', expectedRevision: 0, config })
    expect(currentPetAudioTariff(config.tariffId, config.baseUrl, config.model, Date.parse('2026-09-10T23:59:59Z'))).toBeNull()
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse(PET_AUDIO_TARIFF.reviewBefore))
    const summary = new PetAudioSettingsStore(dir).execute({ kind: 'read' })
    expect(summary).toMatchObject({ enabled: false, state: 'tariff_unavailable', configured: true, revision: 1 })
    expect(store.runtimeConfig()?.apiKey).toBe('synthetic-key-only')
    expect(() => store.execute({ kind: 'save', actionId: 'expired-save', expectedRevision: 1, config })).toThrow()
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

it('recovers a saved configuration after restart failure by explicit apply of the same ID, never another save', async () => {
  const dir = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-restart-failure-'))
  try {
    const store = new PetAudioSettingsStore(dir)
    let fail = true, applies = 0, applied: string | null = null
    const handler = createPetAudioSettingsHandler(store, async () => {}, { apply: async config => {
      applies++; if (fail) throw Error('Synthetic backend launch failed'); applied = config?.actionId ?? null
    }, applied: id => applied === id })
    const command = { kind: 'save' as const, expectedRevision: 0, actionId: 'saved-launch-failed', config: {
      purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: PET_AUDIO_TARIFF.model,
      apiKey: 'synthetic-key-only', enabled: true, tariffId: PET_AUDIO_TARIFF.id } }
    await expect(handler({ sender: { id: 1 } }, 'primary', 'parent', command)).rejects.toThrow('launch failed')
    expect(await handler({ sender: { id: 1 } }, 'primary', 'parent', { kind: 'read' })).toMatchObject({ revision: 1, actionId: command.actionId, runtimeApplied: false })
    fail = false
    const result = await handler({ sender: { id: 1 } }, 'primary', 'parent', { kind: 'apply', expectedRevision: 1, actionId: command.actionId })
    expect(result).toMatchObject({ revision: 1, actionId: command.actionId, runtimeApplied: true })
    expect(applies).toBe(2)
    await expect(handler({ sender: { id: 1 } }, 'primary', 'parent', command)).rejects.toThrow('版本已变化')
  } finally { rmSync(dir, { recursive: true, force: true }) }
})

it('leaves an externally managed backend and its prior settings unchanged rather than claiming an unapplied save', async () => {
  const dir = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-external-'))
  try {
    const store = new PetAudioSettingsStore(dir), apply = vi.fn()
    const handler = createPetAudioSettingsHandler(store, async () => {}, { apply, applied: () => false, canApply: () => false })
    expect(await handler({ sender: { id: 1 } }, 'primary', 'parent', { kind: 'read' })).toMatchObject({ managed: false, revision: 0 })
    await expect(handler({ sender: { id: 1 } }, 'primary', 'parent', { kind: 'clear', expectedRevision: 0, actionId: 'external-clear' })).rejects.toThrow('外部后台')
    expect(store.execute({ kind: 'read' }).revision).toBe(0)
    expect(apply).not.toHaveBeenCalled()
  } finally { rmSync(dir, { recursive: true, force: true }) }
})


it('keeps a retired protected record disabled and unchanged until an explicit new-model save, without inventing a child grant', () => {
  const dir = mkdtempSync(join(realpathSync(tmpdir()), 'hsc-pet-retired-migration-'))
  try {
    const path = join(dir, 'pet-audio-settings.bin')
    const old = { version: 1, revision: 7, actionId: 'old-config-0007', config: {
      purpose: PET_AUDIO_PURPOSE, baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: 'qwen3-omni-flash-2025-12-01', apiKey: 'synthetic-retained-key', enabled: true,
      tariffId: 'aliyun-cn-qwen3-omni-20251201-20260911-v1'
    } }
    writeFileSync(path, Buffer.from(Buffer.from(JSON.stringify(old)).toString('base64')), { flag: 'wx', mode: 0o600 })
    const bytes = readFileSync(path), store = new PetAudioSettingsStore(dir)
    expect(store.execute({ kind: 'read' })).toMatchObject({ revision: 7, actionId: old.actionId,
      configured: true, enabled: false, state: 'tariff_unavailable', model: old.config.model })
    expect(readFileSync(path)).toEqual(bytes)
    const base = { sessionToken: 's', dataEncryptionKey: 'k', dataDir: dir, databasePath: join(dir, 'test.db'),
      localModelDir: dir, modelConfig: null, voiceConfig: null }
    const before = buildLocalServiceChildEnvironment({}, { ...base, petAudioConfig: store.runtimeConfig() })
    expect(Object.keys(before).filter(key => key.startsWith('HSC_PET_AUDIO_'))).toEqual([])
    store.execute({ kind: 'save', expectedRevision: 7, actionId: 'new-config-0008', config: {
      ...old.config, apiKey: '', model: PET_AUDIO_TARIFF.model, tariffId: PET_AUDIO_TARIFF.id
    } })
    const reopened = new PetAudioSettingsStore(dir)
    expect(reopened.execute({ kind: 'read' })).toMatchObject({ revision: 8, actionId: 'new-config-0008',
      enabled: true, model: PET_AUDIO_TARIFF.model, tariffId: PET_AUDIO_TARIFF.id })
    expect(reopened.runtimeConfig()?.apiKey).toBe(old.config.apiKey)
    const next = buildLocalServiceChildEnvironment({}, { ...base, petAudioConfig: reopened.runtimeConfig() })
    expect(next.HSC_PET_AUDIO_MODEL).toBe('qwen3.5-omni-plus-2026-03-15')
    expect(next.HSC_PET_AUDIO_CATALOG_ID).toBe(PET_AUDIO_TARIFF.id)
    expect(Object.keys(next).filter(key => /GRANT|TOKEN_PLAN/.test(key))).toEqual([])
  } finally { rmSync(dir, { recursive: true, force: true }) }
})
