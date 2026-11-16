import { app, safeStorage } from 'electron'
import { randomBytes } from 'node:crypto'
import { readFileSync, writeFileSync, renameSync, unlinkSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { applyQwenOverlay, QwenOverlayError, writeQwenOverlayEdit } from './qwen-key-overlay'
import { PET_AUDIO_PURPOSE, type PetAudioSettingsCommand, type PetAudioSettingsInput, type PetAudioSettingsSummary, type PetAudioRuntimeConfig } from '../shared/petAudioSettings'
import { currentPetAudioTariff } from '../shared/petAudioTariffs'
import type { PetCatalogSelection, PetCatalogTariff } from '../shared/petAudioCatalog'
import { PetAudioCatalogStore } from './pet-audio-catalog'

type RecordValue = { version: 1; revision: number; actionId: string; config: (PetAudioSettingsInput & { catalog?: PetCatalogSelection }) | null }
const official = /^https:\/\/(?:dashscope\.aliyuncs\.com|dashscope-intl\.aliyuncs\.com|[a-z0-9][a-z0-9-]{0,62}\.(?:cn-beijing|ap-southeast-1|ap-northeast-1|us-east-1)\.maas\.aliyuncs\.com)\/compatible-mode\/v1$/
function validate(input: PetAudioSettingsInput, prior: PetAudioSettingsInput | null, saved = false, tariff?: PetCatalogTariff): PetAudioSettingsInput {
  if (!input || input.purpose !== PET_AUDIO_PURPOSE || typeof input.baseUrl !== 'string'
    || typeof input.model !== 'string' || typeof input.apiKey !== 'string'
    || (input.enabled !== undefined && typeof input.enabled !== 'boolean')
    || (input.tariffId !== undefined && (typeof input.tariffId !== 'string' || input.tariffId.length > 100))
    || Object.keys(input).some(key => !['purpose', 'baseUrl', 'model', 'apiKey', 'enabled', 'tariffId'].includes(key))) throw Error('原音专属配置格式无效。')
  const baseUrl = input.baseUrl.trim().replace(/\/+$/, ''), model = input.model.trim()
  if (!official.test(baseUrl) || !/^[A-Za-z0-9._-]{1,150}$/.test(model)) throw Error('原音模型或官方同源地址无效。')
  const apiKey = input.apiKey.trim() || (prior?.baseUrl === baseUrl ? prior.apiKey : '')
  if (apiKey.length < 8 || apiKey.length > 4096 || /[\r\n]/.test(apiKey)) throw Error('请输入该原音服务地址的密钥；更换区域不能沿用旧密钥。')
  if (input.enabled === true && !saved && !currentPetAudioTariff(input.tariffId, baseUrl, model, Date.now(), tariff)) throw Error('该模型、地域或费率版本尚未核验或已过期，未启用。')
  return { purpose: PET_AUDIO_PURPOSE, baseUrl, model, apiKey, enabled: input.enabled === true, tariffId: input.tariffId ?? '' }
}

export class PetAudioSettingsStore {
  private readonly path: string
  readonly catalogs: PetAudioCatalogStore
  constructor(directory = app.getPath('userData'), catalogs?: PetAudioCatalogStore) { this.path = join(directory, 'pet-audio-settings.bin'); this.catalogs = catalogs ?? new PetAudioCatalogStore(directory) }

  private load(): RecordValue | null {
    let encrypted: Buffer
    try {
      const original = (() => { try { return readFileSync(this.path) } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null; throw error } })()
      const bytes = applyQwenOverlay(dirname(this.path), 'pet-audio-settings.bin', original)
      if (!bytes) return null
      encrypted = bytes
    } catch (error) { if (error instanceof QwenOverlayError) throw error; throw Error('无法读取原音配置，未覆盖原记录。') }
    if (!safeStorage.isEncryptionAvailable()) throw Error('系统安全存储不可用，未读取或覆盖原音配置。')
    try {
      const value = JSON.parse(safeStorage.decryptString(encrypted)) as RecordValue
      if (value.version !== 1 || !Number.isSafeInteger(value.revision) || value.revision < 1
        || typeof value.actionId !== 'string' || !/^[A-Za-z0-9-]{8,100}$/.test(value.actionId)) throw Error('Invalid record')
      if (value.config !== null) {
        const { catalog, ...input } = value.config
        if (catalog !== undefined && (!catalog || Object.keys(catalog).sort().join('|') !== 'file|revision|sha256' || typeof catalog.file !== 'string' || !/^[a-f0-9]{64}$/.test(catalog.sha256) || !Number.isSafeInteger(catalog.revision))) throw Error('Invalid catalogue record')
        value.config = { ...validate(input, null, true), ...(catalog ? { catalog } : {}) }
      }
      return value
    } catch { throw Error('原音配置无法解密或已损坏，未覆盖原记录。') }
  }

  private summary(value: RecordValue | null): PetAudioSettingsSummary {
    let selected: ReturnType<PetAudioCatalogStore['current']> | undefined, unavailable = false, expired = false
    try {
      this.catalogs.assertPrior(value?.config?.catalog)
      if (value?.config?.catalog) selected = this.catalogs.current(value.config.catalog); else unavailable = !this.catalogs.builtinAllowed()
    } catch (error) { if (error instanceof Error && error.message === 'PET_CATALOG_EXPIRED') expired = true; else unavailable = true }
    const enabled = Boolean(!unavailable && !expired && value?.config?.enabled && currentPetAudioTariff(value.config.tariffId, value.config.baseUrl, value.config.model, Date.now(), selected?.tariff))
    return { purpose: PET_AUDIO_PURPOSE, revision: value?.revision ?? 0, actionId: value?.actionId ?? null,
      configured: Boolean(value?.config), enabled, tariffId: value?.config?.tariffId ?? '',
      state: expired ? 'tariff_unavailable' : unavailable ? 'catalog_recovery_required' : enabled ? 'ready' : value?.config?.enabled ? 'tariff_unavailable' : value?.config ? 'awaiting_verified_tariff' : 'missing',
      baseUrl: value?.config?.baseUrl ?? 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: value?.config?.model ?? '',
      ...(selected ? { catalog: { sha256: selected.sha256, revision: selected.payload.revision, issuer: selected.issuer, tariff: selected.tariff, sourceUrls: selected.payload.source_urls } } : {}) }
  }

  runtimeConfig(): PetAudioRuntimeConfig | null {
    const value = this.load()
    if (!value) return null
    return { ...(value.config ?? { purpose: PET_AUDIO_PURPOSE, baseUrl: '', model: '', apiKey: '', enabled: false }),
      revision: value.revision, actionId: value.actionId, enabled: this.summary(value).enabled, control: this.catalogs.controlSnapshot() }
  }

  // No awaits between CAS and atomic replacement. The caller completes its live parent gate first.
  execute(command: PetAudioSettingsCommand, catalogScope = ''): PetAudioSettingsSummary {
    const prior = this.load()
    if (command?.kind === 'read') return this.summary(prior)
    if (command?.kind === 'previewCatalog') throw Error('目录导入必须经过当前家长与原生文件选择器。')
    if (command?.kind === 'apply') {
      if (!prior || prior.revision !== command.expectedRevision || prior.actionId !== command.actionId) throw Error('原配置版本已变化，未重新应用。')
      return this.summary(prior)
    }
    if (!command || !['save', 'clear'].includes(command.kind) || !Number.isSafeInteger(command.expectedRevision)
      || command.expectedRevision >= Number.MAX_SAFE_INTEGER || command.expectedRevision !== (prior?.revision ?? 0)
      || typeof command.actionId !== 'string' || !/^[A-Za-z0-9-]{8,100}$/.test(command.actionId)
      || command.actionId === prior?.actionId) throw Error('原音配置版本已变化，请先重新读取；没有重复保存。')
    if (Object.keys(command).some(key => !['kind','expectedRevision','actionId','config','catalogToken'].includes(key))
      || (command.catalogToken !== undefined && (command.kind !== 'save' || typeof command.catalogToken !== 'string' || !/^[a-f0-9]{48}$/.test(command.catalogToken)))) throw Error('目录选用回执无效，未保存。')
    if (!safeStorage.isEncryptionAvailable()) throw Error('系统安全存储不可用，未保存原音密钥。')
    let nextConfig: RecordValue['config'] = null
    if (command.kind === 'save') {
      this.catalogs.assertPrior(prior?.config?.catalog)
      const staged = command.catalogToken ? this.catalogs.inspect(command.catalogToken, catalogScope) : null
      const sameCatalog = !staged && prior?.config?.catalog && command.config?.tariffId === prior.config.tariffId ? prior.config.catalog : null
      const selected = staged ?? (sameCatalog ? this.catalogs.current(sameCatalog) : null)
      if (!selected && !this.catalogs.builtinAllowed()) throw Error('目录更新尚待配置确认；请重新导入已核目录并显式保存。')
      const clean = validate(command.config!, prior?.config ?? null, false, selected?.tariff)
      if (staged && clean.tariffId !== staged.tariff.id) throw Error('选用目录已变化，未保存。')
      const catalog = command.catalogToken ? this.catalogs.activate(command.catalogToken, catalogScope) : sameCatalog
      nextConfig = { ...clean, ...(catalog ? { catalog } : {}) }
    }
    const next: RecordValue = { version: 1, revision: command.expectedRevision + 1, actionId: command.actionId, config: nextConfig }
    const encoded = safeStorage.encryptString(JSON.stringify(next))
    if (safeStorage.decryptString(encoded) !== JSON.stringify(next)) throw Error('安全存储读回不一致，未保存。')
    if (!writeQwenOverlayEdit(dirname(this.path), 'pet-audio-settings.bin', encoded)) {
      const temporary = `${this.path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
      try { writeFileSync(temporary, encoded, { flag: 'wx', mode: 0o600 }); renameSync(temporary, this.path) }
      finally { try { unlinkSync(temporary) } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error } }
    }
    const reread = this.load()
    if (JSON.stringify(reread) !== JSON.stringify(next)) throw Error('保存回执未确认，请重新读取原操作。')
    return this.summary(reread)
  }
}

export function createPetAudioSettingsHandler(store: PetAudioSettingsStore,
  authorize: (senderId: number, childId: string, parentToken: string) => Promise<void>,
  runtime?: { apply: (config: PetAudioRuntimeConfig | null) => Promise<void>; applied: (actionId: string | null) => boolean; revoke?: () => void; canApply?: () => boolean; importCatalog?: () => Promise<Buffer | null> }) {
  let applying = false
  return async (event: { sender: { id: number } }, childId: string, parentToken: string, command: PetAudioSettingsCommand): Promise<PetAudioSettingsSummary> => {
    if (typeof childId !== 'string' || !childId || childId.length > 100 || typeof parentToken !== 'string' || !parentToken) throw Error('需要当前家长身份。')
    // Detach from the caller's object before the asynchronous authority read.
    const savedCommand = structuredClone(command)
    await authorize(event.sender.id, childId, parentToken)
    const catalogScope = `${event.sender.id}:${childId}:${parentToken}`
    if (savedCommand.kind === 'previewCatalog') {
      if (Object.keys(savedCommand).join('|') !== 'kind' || !runtime?.importCatalog || applying || runtime.canApply?.() === false) throw Error('当前无法导入目录。')
      const bytes = await runtime.importCatalog()
      await authorize(event.sender.id, childId, parentToken)
      if (!bytes) return store.execute({ kind: 'read' })
      return { ...store.execute({ kind: 'read' }), catalogPreview: store.catalogs.preview(bytes, catalogScope) }
    }
    if (savedCommand.kind === 'read') {
      const result = store.execute(savedCommand, catalogScope)
      return { ...result, ...(runtime ? { runtimeApplied: runtime.applied(result.actionId), managed: runtime.canApply?.() ?? true } : {}) }
    }
    if (applying) throw Error('原音配置正在应用，请读取原操作，未重复保存。')
    if (runtime?.canApply?.() === false) throw Error('外部后台不由桌面托管，未修改其原音配置。')
    applying = true
    let beforeControl: ReturnType<PetAudioCatalogStore['controlSnapshot']> | undefined
    try {
      beforeControl = store.catalogs.controlSnapshot()
      const result = store.execute(savedCommand, catalogScope)
      if (runtime) await runtime.apply(store.runtimeConfig())
      return { ...result, ...(runtime ? { runtimeApplied: runtime.applied(result.actionId) } : {}) }
    } catch (error) {
      if (beforeControl) {
        let unchanged = false
        try { unchanged = JSON.stringify(beforeControl) === JSON.stringify(store.catalogs.controlSnapshot()) } catch { /* unreadable control is never current */ }
        if (!unchanged) { try { runtime?.revoke?.() } catch { /* preserve original failure; the opaque cross-process guard is already revoked */ } }
      }
      throw error
    } finally { applying = false }
  }
}
