import { safeStorage } from 'electron'
import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto'
import { lstat, mkdir, open, rename, unlink } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { ModelSettingsStore, validateModelConfig, type ModelConfig } from './model-settings'
import { CloudAsrSettingsStore, validateCloudAsrConfig, type CloudAsrConfig } from './asr-settings'
import { QwenRealtimeSettingsStore, validateQwenRealtimeConfig, type QwenRealtimeConfig } from './qwen-realtime'
import { ReportDeliverySecretStore, type ReportDeliverySecrets } from './report-delivery-secrets'

const MAGIC = Buffer.from('HSCBOOT1')
const MAX_BYTES = 65_536
const RECEIPT_FILE = 'household-provisioning-receipt.json'
const PAYLOAD_FILE = 'household-bootstrap.bin'
const SLOT_NAMES = ['model', 'asr', 'qwenRealtime', 'smtpPassword', 'wecomWebhook', 'smtpDefaults'] as const
export type ProvisioningSlot = typeof SLOT_NAMES[number]
export type SmtpBootstrapDefaults = {
  host: 'smtp.qq.com'; port: 465; security: 'ssl'; username: string; fromAddress: string; recipient: string
}
export type HouseholdBootstrap = {
  version: 1; bootstrapId: string; settings: {
    model?: ModelConfig; asr?: CloudAsrConfig; qwenRealtime?: QwenRealtimeConfig
    reportDeliverySecrets?: ReportDeliverySecrets; smtpDefaults?: SmtpBootstrapDefaults
  }
}
export type ProvisioningLoad<T> = { state: 'configured'; value: T } | { state: 'not_configured' | 'unreadable' }
export type ProvisioningPort<T> = { inspect: () => Promise<ProvisioningLoad<T>>; save: (value: T) => Promise<unknown> }
export type HouseholdProvisioningStores = {
  model: ProvisioningPort<ModelConfig>; asr: ProvisioningPort<CloudAsrConfig>; qwenRealtime: ProvisioningPort<QwenRealtimeConfig>
  smtpPassword: ProvisioningPort<string>; wecomWebhook: ProvisioningPort<string>; smtpDefaults: ProvisioningPort<SmtpBootstrapDefaults>
}
type SlotState = 'pending' | 'inflight' | 'imported' | 'preserved' | 'suppressed'
type Receipt = { version: 1; bootstrapId: string | null; payloadSha256: string | null; complete: boolean; slots: Partial<Record<ProvisioningSlot, SlotState>> }
export type ProvisioningResult = { status: 'not_present' | 'already_processed' | 'complete' | 'partial'; slots: Partial<Record<ProvisioningSlot, SlotState>> }
export type ProvisioningOptions = {
  resourcesPath: string; userDataPath: string; key: Buffer
  smtpPort: ProvisioningPort<SmtpBootstrapDefaults>
  /** Test/integration ports must inspect the durable current userData stores. */
  stores?: HouseholdProvisioningStores
}
export class HouseholdProvisioningError extends Error {
  constructor(readonly code: 'HOUSEHOLD_BOOTSTRAP_INVALID' | 'HOUSEHOLD_BOOTSTRAP_STORAGE_UNAVAILABLE'
    | 'HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE' | 'HOUSEHOLD_BOOTSTRAP_RECEIPT_UNREADABLE'
    | 'HOUSEHOLD_BOOTSTRAP_RECONCILIATION_REQUIRED' | 'HOUSEHOLD_BOOTSTRAP_PACKAGE_CHANGED'
    | 'HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE') { super(code); this.name = 'HouseholdProvisioningError' }
}
const fail = (code: ConstructorParameters<typeof HouseholdProvisioningError>[0]): never => { throw new HouseholdProvisioningError(code) }
const locks = new Map<string, Promise<unknown>>()
async function serial<T>(root: string, work: () => Promise<T>): Promise<T> {
  const id = resolve(root), prior = locks.get(id) ?? Promise.resolve()
  const request = prior.catch(() => undefined).then(work)
  locks.set(id, request)
  try { return await request } finally { if (locks.get(id) === request) locks.delete(id) }
}
function object(value: unknown, fields: readonly string[], required: readonly string[] = fields): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  const result = value as Record<string, unknown>
  if (Object.keys(result).some(key => !fields.includes(key)) || required.some(key => !Object.hasOwn(result, key))) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  return result
}
function text(value: unknown, min = 1, max = 512): string {
  if (typeof value !== 'string' || value.length < min || value.length > max || value.trim() !== value || /[\x00-\x20\x7f]/.test(value)) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  return value as string
}
function uuid(value: unknown): string {
  const result = text(value, 36, 36)
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(result)) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  return result
}
function endpoint(value: unknown): ModelConfig {
  const input = object(value, ['provider', 'apiKey', 'baseUrl', 'model', 'voiceIntentModel', 'voiceIntentApiKey', 'fallback'], ['provider', 'apiKey', 'baseUrl', 'model', 'voiceIntentModel'])
  const check = (candidate: unknown, primary = false): void => {
    const v = object(candidate, primary ? ['provider', 'apiKey', 'baseUrl', 'model', 'voiceIntentModel', 'voiceIntentApiKey', 'fallback'] : ['provider', 'apiKey', 'baseUrl', 'model'], ['provider', 'apiKey', 'baseUrl', 'model'])
    const provider = v.provider, base = text(v.baseUrl), model = text(v.model, 2, 120)
    text(v.apiKey, 8, 512)
    const workspace = /^https:\/\/[a-zA-Z0-9_-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com\/compatible-mode\/v1$/.test(base)
    if (provider === 'qwen') { if (!(base === 'https://dashscope.aliyuncs.com/compatible-mode/v1' || workspace) || !/^(?:qwen|deepseek)[a-zA-Z0-9._:-]+$/.test(model)) fail('HOUSEHOLD_BOOTSTRAP_INVALID') }
    else if (provider === 'deepseek') { if (!(['https://api.deepseek.com', 'https://api.deepseek.com/v1'].includes(base) || workspace) || !/^deepseek[a-zA-Z0-9._:-]+$/.test(model)) fail('HOUSEHOLD_BOOTSTRAP_INVALID') }
    else fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  }
  check(input, true); text(input.voiceIntentModel, 2, 120)
  if (input.voiceIntentApiKey !== undefined) {
    text(input.voiceIntentApiKey, 8, 512)
    // The independent credential is sent to the primary endpoint. Never allow
    // a private installer to direct it to an unrelated provider's origin.
    if (!(input.baseUrl === 'https://dashscope.aliyuncs.com/compatible-mode/v1'
      || /^https:\/\/[a-zA-Z0-9_-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com\/compatible-mode\/v1$/.test(String(input.baseUrl)))
      || !/^(?:qwen|deepseek)[a-zA-Z0-9._:-]+$/.test(String(input.voiceIntentModel))) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  }
  if (input.fallback !== undefined) check(input.fallback)
  return validateModelConfig(input as ModelConfig)
}
function smtp(value: unknown): SmtpBootstrapDefaults {
  const v = object(value, ['host', 'port', 'security', 'username', 'fromAddress', 'recipient'])
  const email = (input: unknown): string => { const s = text(input, 3, 254); if (!/^[a-zA-Z0-9.!#$%&'*+\/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,63}$/.test(s)) fail('HOUSEHOLD_BOOTSTRAP_INVALID'); return s }
  const username = email(v.username), fromAddress = email(v.fromAddress), recipient = email(v.recipient)
  if (v.host !== 'smtp.qq.com' || v.port !== 465 || v.security !== 'ssl' || !/@qq\.com$/i.test(username) || username !== fromAddress) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  return { host: 'smtp.qq.com', port: 465, security: 'ssl', username, fromAddress, recipient }
}
/** Strict input validator; errors never retain a raw value or provider message. */
export function validateHouseholdBootstrap(value: unknown): HouseholdBootstrap {
  try {
    const v = object(value, ['version', 'bootstrapId', 'settings'])
    if (v.version !== 1) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
    const settings = object(v.settings, ['model', 'asr', 'qwenRealtime', 'reportDeliverySecrets', 'smtpDefaults'], [])
    if (!Object.keys(settings).length) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
    const result: HouseholdBootstrap = { version: 1, bootstrapId: uuid(v.bootstrapId), settings: {} }
    if (settings.model !== undefined) result.settings.model = endpoint(settings.model)
    if (settings.asr !== undefined) {
      const a = object(settings.asr, ['provider', 'apiKey', 'baseUrl', 'model'])
      const base = text(a.baseUrl)
      const workspace = /^https:\/\/[a-zA-Z0-9_-]+\.(?:cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com\/compatible-mode\/v1$/.test(base)
      if (a.provider !== 'dashscope' || !(base === 'https://dashscope.aliyuncs.com/compatible-mode/v1' || workspace) || !/^qwen[a-zA-Z0-9._:-]+$/.test(text(a.model, 2, 120))) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
      text(a.apiKey, 8, 512); result.settings.asr = validateCloudAsrConfig(a as CloudAsrConfig)
    }
    if (settings.qwenRealtime !== undefined) {
      const q = object(settings.qwenRealtime, ['apiKey', 'endpoint', 'model', 'voice', 'enabled'])
      text(q.apiKey, 16, 512); text(q.voice, 2, 180)
      const normalized = validateQwenRealtimeConfig(q as QwenRealtimeConfig)
      if (q.endpoint !== normalized.endpoint) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
      result.settings.qwenRealtime = normalized
    }
    if (settings.reportDeliverySecrets !== undefined) {
      const secrets = object(settings.reportDeliverySecrets, ['smtpPassword', 'wecomWebhook'], [])
      if (!Object.keys(secrets).length) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
      result.settings.reportDeliverySecrets = {}
      if (secrets.smtpPassword !== undefined) result.settings.reportDeliverySecrets.smtpPassword = text(secrets.smtpPassword, 8, 256)
      if (secrets.wecomWebhook !== undefined) {
        const webhook = text(secrets.wecomWebhook, 50, 256)
        if (!/^https:\/\/qyapi\.weixin\.qq\.com\/cgi-bin\/webhook\/send\?key=[a-zA-Z0-9-]{16,80}$/.test(webhook)) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
        result.settings.reportDeliverySecrets.wecomWebhook = webhook
      }
    }
    if (result.settings.reportDeliverySecrets && !Object.keys(result.settings.reportDeliverySecrets).length) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
    if (settings.smtpDefaults !== undefined) result.settings.smtpDefaults = smtp(settings.smtpDefaults)
    if (!Object.keys(result.settings).length) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
    return result
  } catch { return fail('HOUSEHOLD_BOOTSTRAP_INVALID') }
}
/** Package-side helper. This AES envelope prevents accidental plaintext exposure,
 * not extraction by someone who owns the private installer and its key. */
export function sealHouseholdBootstrap(value: unknown, key: Buffer): Buffer {
  if (!Buffer.isBuffer(key) || key.length !== 32) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  const plain = Buffer.from(JSON.stringify(validateHouseholdBootstrap(value)), 'utf8')
  if (plain.length > MAX_BYTES - 36) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  try {
    const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv)
    cipher.setAAD(MAGIC)
    const encrypted = Buffer.concat([cipher.update(plain), cipher.final()])
    return Buffer.concat([MAGIC, iv, cipher.getAuthTag(), encrypted])
  } finally { plain.fill(0) }
}
function decrypt(payload: Buffer, key: Buffer): HouseholdBootstrap {
  if (!Buffer.isBuffer(key) || key.length !== 32 || payload.length <= 36 || payload.length > MAX_BYTES || !payload.subarray(0, 8).equals(MAGIC)) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
  let plain: Buffer | undefined
  try {
    const decipher = createDecipheriv('aes-256-gcm', key, payload.subarray(8, 20)); decipher.setAAD(MAGIC); decipher.setAuthTag(payload.subarray(20, 36))
    plain = Buffer.concat([decipher.update(payload.subarray(36)), decipher.final()])
    const json = new TextDecoder('utf-8', { fatal: true }).decode(plain), parsed: unknown = JSON.parse(json)
    // Package helper emits canonical JSON; reject duplicate keys and ambiguity.
    if (JSON.stringify(parsed) !== json) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
    return validateHouseholdBootstrap(parsed)
  } catch { return fail('HOUSEHOLD_BOOTSTRAP_INVALID') } finally { plain?.fill(0) }
}
async function boundedFile(path: string, limit: number): Promise<Buffer | null> {
  try {
    const info = await lstat(path)
    if (!info.isFile() || info.isSymbolicLink() || info.size > limit) fail('HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE')
    const file = await open(path, 'r')
    try {
      const stat = await file.stat(); if (!stat.isFile() || stat.size > limit || stat.ino !== info.ino || stat.dev !== info.dev) fail('HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE')
      const buffer = Buffer.alloc(limit + 1), { bytesRead } = await file.read(buffer, 0, buffer.length, 0)
      if (bytesRead > limit) fail('HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE')
      return buffer.subarray(0, bytesRead)
    } finally { await file.close() }
  } catch (error) { if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') return null; throw error }
}
async function readReceipt(root: string): Promise<Receipt> {
  let bytes: Buffer | null
  try { bytes = await boundedFile(join(root, RECEIPT_FILE), 8192) } catch { return fail('HOUSEHOLD_BOOTSTRAP_RECEIPT_UNREADABLE') }
  if (!bytes) return { version: 1, bootstrapId: null, payloadSha256: null, complete: false, slots: {} }
  try {
    const v = object(JSON.parse(bytes.toString('utf8')), ['version', 'bootstrapId', 'payloadSha256', 'complete', 'slots'])
    if (v.version !== 1 || typeof v.complete !== 'boolean') throw Error()
    if (v.bootstrapId !== null) uuid(v.bootstrapId)
    if (v.payloadSha256 !== null && (typeof v.payloadSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(v.payloadSha256))) throw Error()
    if ((v.bootstrapId === null) !== (v.payloadSha256 === null)) throw Error()
    const slots = object(v.slots, SLOT_NAMES, [])
    for (const state of Object.values(slots)) if (typeof state !== 'string' || !['pending', 'inflight', 'imported', 'preserved', 'suppressed'].includes(state)) throw Error()
    if (v.complete && (!v.bootstrapId || !Object.keys(slots).length || Object.values(slots).some(state => state === 'pending' || state === 'inflight'))) throw Error()
    return v as Receipt
  } catch { return fail('HOUSEHOLD_BOOTSTRAP_RECEIPT_UNREADABLE') }
}
async function persistReceipt(root: string, receipt: Receipt): Promise<void> {
  await mkdir(root, { recursive: true })
  const temporary = join(root, `${RECEIPT_FILE}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`)
  const file = await open(temporary, 'wx', 0o600)
  try { await file.writeFile(JSON.stringify(receipt)); await file.sync() } finally { await file.close() }
  try {
    await rename(temporary, join(root, RECEIPT_FILE))
    // Windows does not support opening/fsyncing directories through this API.
    if (process.platform !== 'win32') { const directory = await open(root, 'r'); try { await directory.sync() } finally { await directory.close() } }
  } finally { await unlink(temporary).catch(() => undefined) }
}
const terminal = (state: SlotState | undefined): boolean => state === 'imported' || state === 'preserved' || state === 'suppressed'
async function fileStore<T>(path: string, load: () => Promise<T | null>): Promise<ProvisioningLoad<T>> {
  try {
    if (!await boundedFile(path, MAX_BYTES)) return { state: 'not_configured' }
    const value = await load()
    return value === null ? { state: 'unreadable' } : { state: 'configured', value }
  } catch { return { state: 'unreadable' } }
}
export function createHouseholdProvisioningStores(userDataPath: string, smtpPort: ProvisioningPort<SmtpBootstrapDefaults>): HouseholdProvisioningStores {
  const model = new ModelSettingsStore(userDataPath), asr = new CloudAsrSettingsStore(userDataPath), realtime = new QwenRealtimeSettingsStore(userDataPath), report = new ReportDeliverySecretStore(userDataPath)
  const secret = (name: keyof ReportDeliverySecrets): ProvisioningPort<string> => ({
    inspect: async () => {
      try {
        const bytes = await boundedFile(join(userDataPath, 'report-delivery-secrets.json'), MAX_BYTES)
        if (!bytes) return { state: 'not_configured' }
        const stored = object(JSON.parse(bytes.toString('utf8')), ['encryptedSmtpPassword', 'encryptedWecomWebhook'], [])
        for (const ciphertext of Object.values(stored)) {
          if (typeof ciphertext !== 'string' || !ciphertext.length || ciphertext.length > 32_768
            || Buffer.from(ciphertext, 'base64').toString('base64') !== ciphertext) return { state: 'unreadable' }
        }
      } catch { return { state: 'unreadable' } }
      const { secrets, summary } = await report.inspect()
      if (summary.state === 'unavailable' || summary.state === 'corrupt') return { state: 'unreadable' }
      return secrets[name] ? { state: 'configured', value: secrets[name]! } : { state: 'not_configured' }
    }, save: value => report.save({ [name]: value }),
  })
  return {
    model: { inspect: async () => fileStore(join(userDataPath, 'model-settings.json'), async () => {
      const result = await model.loadWithStatus(); if (result.diagnostic !== 'loaded' || !result.config) return null
      try { return validateModelConfig(result.config) } catch { return null }
    }), save: value => model.save(value) },
    asr: { inspect: () => fileStore(join(userDataPath, 'asr-settings.json'), () => asr.load()), save: value => asr.save(value) },
    qwenRealtime: { inspect: () => fileStore(join(userDataPath, 'qwen-realtime-settings.json'), () => realtime.load()), save: value => realtime.save(value) },
    smtpPassword: secret('smtpPassword'), wecomWebhook: secret('wecomWebhook'), smtpDefaults: smtpPort,
  }
}
function targets(payload: HouseholdBootstrap): Partial<Record<ProvisioningSlot, unknown>> {
  const s = payload.settings
  return { ...(s.model ? { model: s.model } : {}), ...(s.asr ? { asr: s.asr } : {}), ...(s.qwenRealtime ? { qwenRealtime: s.qwenRealtime } : {}),
    ...(s.reportDeliverySecrets?.smtpPassword ? { smtpPassword: s.reportDeliverySecrets.smtpPassword } : {}), ...(s.reportDeliverySecrets?.wecomWebhook ? { wecomWebhook: s.reportDeliverySecrets.wecomWebhook } : {}), ...(s.smtpDefaults ? { smtpDefaults: s.smtpDefaults } : {}) }
}
async function inspectAll(stores: HouseholdProvisioningStores): Promise<Record<ProvisioningSlot, ProvisioningLoad<unknown>>> {
  const rows = await Promise.all(SLOT_NAMES.map(async slot => { try { return [slot, await stores[slot].inspect()] as const } catch { return [slot, { state: 'unreadable' }] as const } }))
  const result = Object.fromEntries(rows) as Record<ProvisioningSlot, ProvisioningLoad<unknown>>
  if (SLOT_NAMES.some(slot => !['configured', 'not_configured'].includes(result[slot]?.state))) fail('HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE')
  return result
}
/** Run before renderer/settings mutation is enabled. Clear IPC must first await
 * suppressHouseholdProvisioningSlots, using this same serialized boundary. */
export async function importHouseholdProvisioning(options: ProvisioningOptions): Promise<ProvisioningResult> {
  return serial(options.userDataPath, async () => {
    try {
      const receipt = await readReceipt(options.userDataPath), path = join(options.resourcesPath, PAYLOAD_FILE)
      if (receipt.complete) { await unlink(path).catch(error => { if (error?.code !== 'ENOENT') throw error }); return { status: 'already_processed', slots: { ...receipt.slots } } }
      const bytes = await boundedFile(path, MAX_BYTES)
      if (!bytes) return { status: 'not_present', slots: { ...receipt.slots } }
      const payload = decrypt(bytes, options.key), payloadSha256 = createHash('sha256').update(bytes).digest('hex')
      if (receipt.bootstrapId && (receipt.bootstrapId !== payload.bootstrapId || receipt.payloadSha256 !== payloadSha256)) fail('HOUSEHOLD_BOOTSTRAP_PACKAGE_CHANGED')
      if (!safeStorage.isEncryptionAvailable()) fail('HOUSEHOLD_BOOTSTRAP_STORAGE_UNAVAILABLE')
      const stores = options.stores ?? createHouseholdProvisioningStores(options.userDataPath, options.smtpPort)
      const before = await inspectAll(stores), desired = targets(payload), slots = Object.keys(desired) as ProvisioningSlot[]
      // Resolve prior successful writes conservatively. An absent inflight target
      // cannot distinguish a crash-before-save from a later explicit user clear.
      for (const slot of slots) if (receipt.slots[slot] === 'inflight' && before[slot].state === 'not_configured') fail('HOUSEHOLD_BOOTSTRAP_RECONCILIATION_REQUIRED')
      receipt.bootstrapId = payload.bootstrapId; receipt.payloadSha256 = payloadSha256
      for (const slot of slots) if (!terminal(receipt.slots[slot])) receipt.slots[slot] = before[slot].state === 'configured' ? 'preserved' : 'pending'
      await persistReceipt(options.userDataPath, receipt)
      for (const slot of slots) {
        if (terminal(receipt.slots[slot])) continue
        const current = await stores[slot].inspect()
        if (current.state === 'unreadable') fail('HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE')
        if (current.state === 'configured') { receipt.slots[slot] = 'preserved'; await persistReceipt(options.userDataPath, receipt); continue }
        receipt.slots[slot] = 'inflight'; await persistReceipt(options.userDataPath, receipt)
        try { await (stores[slot].save as (value: unknown) => Promise<unknown>)(desired[slot]) }
        catch {
          const actual = await stores[slot].inspect()
          if (actual.state === 'configured') receipt.slots[slot] = 'preserved'
          else if (actual.state === 'not_configured') receipt.slots[slot] = 'pending'
          else fail('HOUSEHOLD_BOOTSTRAP_CONFIG_UNREADABLE')
          await persistReceipt(options.userDataPath, receipt)
          return { status: 'partial', slots: { ...receipt.slots } }
        }
        const actual = await stores[slot].inspect()
        if (actual.state !== 'configured') fail('HOUSEHOLD_BOOTSTRAP_RECONCILIATION_REQUIRED')
        receipt.slots[slot] = 'imported'; await persistReceipt(options.userDataPath, receipt)
      }
      receipt.complete = true; await persistReceipt(options.userDataPath, receipt)
      await unlink(path)
      return { status: 'complete', slots: { ...receipt.slots } }
    } catch (error) { if (error instanceof HouseholdProvisioningError) throw error; return fail('HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE') }
  })
}
/** Persist this tombstone BEFORE the existing settings clear. Do not invoke it
 * merely because import failed; it represents an explicit user decision. */
export async function suppressHouseholdProvisioningSlots(userDataPath: string, slots: readonly ProvisioningSlot[]): Promise<void> {
  return serial(userDataPath, async () => {
    try {
      if (!slots.length || slots.some(slot => !SLOT_NAMES.includes(slot))) fail('HOUSEHOLD_BOOTSTRAP_INVALID')
      const receipt = await readReceipt(userDataPath)
      for (const slot of slots) receipt.slots[slot] = 'suppressed'
      await persistReceipt(userDataPath, receipt)
    } catch (error) { if (error instanceof HouseholdProvisioningError) throw error; return fail('HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE') }
  })
}
