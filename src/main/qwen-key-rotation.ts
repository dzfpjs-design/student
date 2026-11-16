import { safeStorage } from 'electron'
import { createCipheriv, createDecipheriv, createHash, randomBytes, randomUUID } from 'node:crypto'
import { link, lstat, mkdir, open, readFile, rename, unlink } from 'node:fs/promises'
import { join } from 'node:path'
import { validateModelConfig, type ModelConfig } from './model-settings'
import { validateCloudAsrConfig, type CloudAsrConfig } from './asr-settings'
import { QwenRealtimeSettingsStore, validateQwenRealtimeConfig, type QwenRealtimeConfig } from './qwen-realtime'
import { VoiceSettingsStore, validateVoiceConfig, type VoiceConfig } from './voice-settings'
import { PetAudioSettingsStore } from './pet-audio-settings'
import { applyQwenOverlay, qwenOverlayImplicitVoice, readQwenOverlay, recoverQwenOverlayEdit } from './qwen-key-overlay'
import { cloudProviderSettingsFingerprint } from './runtime-provider-refresh'

// A release-specific generation is separate from household bootstrap and never
// replays over later settings. Keys are passed only as authenticated ciphertext.
const MAGIC = Buffer.from('HSCROT01')
const MAX_FILE = 131_072
const JOURNAL = 'qwen-key-rotation-journal.bin'
const FILES = ['model-settings.json', 'asr-settings.json', 'qwen-realtime-settings.json', 'voice-settings.json', 'pet-audio-settings.bin'] as const
type Name = typeof FILES[number]
type Entry = { name: Name; before: string | null; after: string | null }
type Journal = { version: 1; generation: string; order: number; payloadHash: string; keyHash: string; overlay: string; entries: Entry[] }
type Payload = { version: 1; generation: string; order: number; apiKey: string }
export type RotationResult = { status: 'not_present' | 'completed' | 'already_completed'; changedSlots: number }

export class QwenRotationError extends Error {
  constructor(readonly code: 'ROTATION_INVALID' | 'ROTATION_STORAGE_UNAVAILABLE' | 'ROTATION_CONFIG_UNREADABLE' | 'ROTATION_MODEL_UNCLASSIFIED' | 'ROTATION_CONFLICT' | 'ROTATION_IO_UNAVAILABLE') { super(code) }
}
const fail = (code: QwenRotationError['code']): never => { throw new QwenRotationError(code) }
const generationPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const sha = (bytes: Buffer): string => createHash('sha256').update(bytes).digest('hex')
/** Opaque in-memory binding; never expose or log the returned digest. */
export async function qwenRotationRecoveryBinding(options: { userDataPath: string; resourcesPath: string }): Promise<string> {
  const payload = await bounded(join(options.resourcesPath, 'qwen-rotation.bin'))
  const key = await bounded(join(options.resourcesPath, 'qwen-rotation.key'))
  if (!payload || !key || key.length !== 32) return fail('ROTATION_INVALID')
  const settings = await cloudProviderSettingsFingerprint(options.userDataPath)
  return sha(Buffer.from(`${sha(payload)}:${sha(key)}:${settings}`, 'utf8'))
}
const exact = (value: unknown, fields: string[]): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join('|') === [...fields].sort().join('|')
const validSecret = (value: unknown): value is string => typeof value === 'string' && value.length >= 16 && value.length <= 512 && value.trim() === value && !/[\x00-\x20\x7f]/.test(value)

export function sealQwenRotation(generation: string, apiKey: string, wrappingKey: Buffer, order = Date.now()): Buffer {
  if (!generationPattern.test(generation) || !Number.isSafeInteger(order) || order < 1 || !validSecret(apiKey) || !Buffer.isBuffer(wrappingKey) || wrappingKey.length !== 32) return fail('ROTATION_INVALID')
  const iv = randomBytes(12), plain = Buffer.from(JSON.stringify({ version: 1, generation, order, apiKey }), 'utf8')
  try {
    const cipher = createCipheriv('aes-256-gcm', wrappingKey, iv)
    return Buffer.concat([MAGIC, iv, cipher.update(plain), cipher.final(), cipher.getAuthTag()])
  } finally { plain.fill(0) }
}

function unseal(bytes: Buffer, key: Buffer): Payload {
  if (!Buffer.isBuffer(key) || key.length !== 32 || bytes.length <= 36 || bytes.length > MAX_FILE || !bytes.subarray(0, 8).equals(MAGIC)) return fail('ROTATION_INVALID')
  let plain: Buffer | undefined
  try {
    const cipher = createDecipheriv('aes-256-gcm', key, bytes.subarray(8, 20))
    cipher.setAuthTag(bytes.subarray(-16))
    plain = Buffer.concat([cipher.update(bytes.subarray(20, -16)), cipher.final()])
    const value: unknown = JSON.parse(plain.toString('utf8'))
    if (!exact(value, ['version', 'generation', 'order', 'apiKey']) || value.version !== 1 || typeof value.generation !== 'string'
      || !generationPattern.test(value.generation) || !Number.isSafeInteger(value.order) || Number(value.order) < 1 || !validSecret(value.apiKey)) return fail('ROTATION_INVALID')
    return value as Payload
  } catch { return fail('ROTATION_INVALID') } finally { plain?.fill(0) }
}

async function bounded(path: string, maxBytes = MAX_FILE): Promise<Buffer | null> {
  let observed = false
  try {
    const before = await lstat(path)
    observed = true
    if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > maxBytes) return fail('ROTATION_CONFIG_UNREADABLE')
    const file = await open(path, 'r')
    try {
      const held = await file.stat()
      if (!held.isFile() || held.nlink !== 1 || held.dev !== before.dev || held.ino !== before.ino || held.size !== before.size) return fail('ROTATION_CONFIG_UNREADABLE')
      const bytes = Buffer.alloc(held.size + 1), { bytesRead } = await file.read(bytes, 0, bytes.length, 0)
      if (bytesRead !== held.size) return fail('ROTATION_CONFIG_UNREADABLE')
      const after = await file.stat(), pathAfter = await lstat(path)
      if (!pathAfter.isFile() || pathAfter.isSymbolicLink() || pathAfter.nlink !== 1 || after.nlink !== 1
        || after.dev !== held.dev || after.ino !== held.ino || pathAfter.dev !== held.dev || pathAfter.ino !== held.ino
        || after.size !== held.size || pathAfter.size !== held.size || after.mtimeMs !== held.mtimeMs || after.ctimeMs !== held.ctimeMs) return fail('ROTATION_CONFIG_UNREADABLE')
      return bytes.subarray(0, bytesRead)
    } finally { await file.close() }
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === 'ENOENT' && !observed) return null
    if (error instanceof QwenRotationError) throw error
    return fail('ROTATION_IO_UNAVAILABLE')
  }
}
async function atomic(path: string, bytes: Buffer): Promise<void> {
  const temporary = `${path}.${randomBytes(12).toString('hex')}.tmp`
  try {
    const file = await open(temporary, 'wx', 0o600)
    try { await file.writeFile(bytes); await file.sync() } finally { await file.close() }
    await rename(temporary, path)
  } finally { await unlink(temporary).catch(() => undefined) }
}
function qwenEndpoint(value: unknown): boolean {
  if (typeof value !== 'string') return false
  try {
    const u = new URL(value)
    return u.protocol === 'https:' && !u.username && !u.password && !u.port &&
      /^(?:dashscope(?:-intl)?\.aliyuncs\.com|[a-z0-9_-]+\.(?:cn-beijing|ap-southeast-1|ap-northeast-1|us-east-1)\.maas\.aliyuncs\.com)$/i.test(u.hostname)
  } catch { return false }
}
function decryptField(value: Record<string, unknown>): string {
  const encoded = value.encryptedApiKey
  if (typeof encoded !== 'string' || !encoded || Buffer.from(encoded, 'base64').toString('base64') !== encoded) return fail('ROTATION_CONFIG_UNREADABLE')
  const old = safeStorage.decryptString(Buffer.from(encoded, 'base64'))
  if (typeof old !== 'string' || old.length < 8) return fail('ROTATION_CONFIG_UNREADABLE')
  return old
}
function rotateCipher(value: Record<string, unknown>, apiKey: string): void {
  decryptField(value)
  const encrypted = safeStorage.encryptString(apiKey)
  if (safeStorage.decryptString(encrypted) !== apiKey) return fail('ROTATION_STORAGE_UNAVAILABLE')
  value.encryptedApiKey = encrypted.toString('base64')
}
function sealSecret(apiKey: string): string {
  const encrypted = safeStorage.encryptString(apiKey)
  if (safeStorage.decryptString(encrypted) !== apiKey) return fail('ROTATION_STORAGE_UNAVAILABLE')
  return encrypted.toString('base64')
}
// This intentionally covers only reviewed IDs used by this product, not the
// provider's complete catalog. An alias cannot establish the model vendor.
const KNOWN_QWEN_MODELS = new Set(['qwen3.5-plus','qwen3.6-plus','qwen3.7-plus','qwen3.8-flash',
  'qwen3-asr-flash','qwen3-asr-flash-realtime','qwen3-vl-flash','qwen3-vl-plus',
  'qwen3.5-omni-plus-2026-03-15','qwen3.5-omni-flash-realtime','qwen3.8-omni-flash',
  'qwen3.8-omni-flash-realtime','qwen3-omni-flash','qwen3-omni-flash-2025-12-01','qwen3-tts-vc-2026-01-22'])
const KNOWN_OTHER_ALI_MODELS = new Set(['deepseek-v4-pro','deepseek-v4-flash','deepseek-chat','deepseek-reasoner','paraformer-realtime-v2'])
function knownAliModel(model: unknown): 'qwen' | 'other' {
  if (typeof model !== 'string') return fail('ROTATION_CONFIG_UNREADABLE')
  if (KNOWN_QWEN_MODELS.has(model)) return 'qwen'
  if (KNOWN_OTHER_ALI_MODELS.has(model)) return 'other'
  return fail('ROTATION_MODEL_UNCLASSIFIED')
}
function endpointSlot(value: unknown, apiKey: string): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fail('ROTATION_CONFIG_UNREADABLE')
  const item = value as Record<string, unknown>
  if (item.provider !== 'qwen' && item.provider !== 'deepseek') return fail('ROTATION_CONFIG_UNREADABLE')
  if (typeof item.model !== 'string') return fail('ROTATION_CONFIG_UNREADABLE')
  const official = qwenEndpoint(item.baseUrl)
  const qwenModel = official ? knownAliModel(item.model) === 'qwen' : KNOWN_QWEN_MODELS.has(item.model)
  if (!official && !qwenModel && (item.provider === 'qwen' || /^qwen/i.test(item.model))) return fail('ROTATION_MODEL_UNCLASSIFIED')
  if (qwenModel && item.provider !== 'qwen') return fail('ROTATION_MODEL_UNCLASSIFIED')
  if (qwenModel && !official) return fail('ROTATION_CONFIG_UNREADABLE')
  if (!qwenModel) return false
  rotateCipher(item, apiKey); return true
}

async function prepare(root: string, payload: Payload): Promise<{ entries: Entry[]; rotated: number }> {
  const entries: Entry[] = [], previous = readQwenOverlay(root)
  let rotated = 0
  for (const name of FILES) {
    const before = await bounded(join(root, name))
    const source = applyQwenOverlay(root, name, before)
    const hadOverlay = !!previous?.overlay.entries.some(item => item.name === name)
    if (!before && !source) { if (hadOverlay) entries.push({name,before:name==='voice-settings.json' && !qwenOverlayImplicitVoice(root)?'':null,after:null}); continue }
    if (!source) { if (hadOverlay && before) entries.push({name,before:before.toString('base64'),after:null}); continue }
    try {
      let changed = false, after: Buffer
      if (name === 'pet-audio-settings.bin') {
        // Reuse the production reader's complete schema/catalog checks; never
        // create or enable this optional, independently authorized feature.
        new PetAudioSettingsStore(root).runtimeConfig()
        const value = JSON.parse(safeStorage.decryptString(source))
        if (!value || value.version !== 1 || !Number.isSafeInteger(value.revision) || value.revision < 1 || value.revision >= Number.MAX_SAFE_INTEGER
          || typeof value.actionId !== 'string' || !Object.hasOwn(value, 'config')) return fail('ROTATION_CONFIG_UNREADABLE')
        if (value.config !== null) {
          if (!value.config || typeof value.config !== 'object' || !qwenEndpoint(value.config.baseUrl) || typeof value.config.apiKey !== 'string' || value.config.apiKey.length < 8) return fail('ROTATION_CONFIG_UNREADABLE')
          if (typeof value.config.model !== 'string') return fail('ROTATION_CONFIG_UNREADABLE')
          if (knownAliModel(value.config.model) === 'qwen') { value.config.apiKey = payload.apiKey; value.revision += 1; value.actionId = `rotation-${payload.generation}`; changed = true }
        }
        if (changed) {
          const serialized = JSON.stringify(value)
          after = safeStorage.encryptString(serialized)
          if (safeStorage.decryptString(after) !== serialized) return fail('ROTATION_STORAGE_UNAVAILABLE')
        } else after = source
      } else {
        const value = JSON.parse(source.toString('utf8')) as Record<string, unknown>
        if (!value || typeof value !== 'object' || Array.isArray(value)) return fail('ROTATION_CONFIG_UNREADABLE')
        if (name === 'model-settings.json') {
          const fallback = value.fallback as Record<string, unknown> | undefined
          const primaryOldKey = decryptField(value)
          const independentIntent = value.encryptedVoiceIntentApiKey === undefined ? undefined : decryptField({ encryptedApiKey: value.encryptedVoiceIntentApiKey })
          validateModelConfig({ ...value, apiKey: decryptField(value), voiceIntentModel: value.voiceIntentModel || value.model,
            ...(independentIntent ? { voiceIntentApiKey: independentIntent } : {}),
            ...(fallback ? { fallback: { ...fallback, apiKey: decryptField(fallback) } } : {}) } as ModelConfig)
          const primaryQwen = qwenEndpoint(value.baseUrl) ? knownAliModel(value.model) === 'qwen' : typeof value.model === 'string' && KNOWN_QWEN_MODELS.has(value.model)
          const intentModel = value.voiceIntentModel || value.model
          const intentQwen = qwenEndpoint(value.baseUrl) ? knownAliModel(intentModel) === 'qwen' : typeof intentModel === 'string' && KNOWN_QWEN_MODELS.has(intentModel)
          if (!qwenEndpoint(value.baseUrl) && typeof intentModel === 'string' && /^qwen/i.test(intentModel) && !intentQwen) return fail('ROTATION_MODEL_UNCLASSIFIED')
          if (intentQwen && !qwenEndpoint(value.baseUrl)) return fail('ROTATION_CONFIG_UNREADABLE')
          // Preserve a non-Qwen intent's original key before rotating a Qwen
          // primary. Conversely, a Qwen intent gets its own encrypted key.
          if (primaryQwen && !intentQwen && !independentIntent) value.encryptedVoiceIntentApiKey = sealSecret(primaryOldKey)
          if (intentQwen && (!primaryQwen || independentIntent)) value.encryptedVoiceIntentApiKey = sealSecret(payload.apiKey)
          changed = endpointSlot(value, payload.apiKey)
          if (intentQwen && !primaryQwen) changed = true
          if (value.fallback !== undefined) changed = endpointSlot(value.fallback, payload.apiKey) || changed
        } else if (name === 'asr-settings.json') {
          const visionKey = value.encryptedDocumentVisionApiKey === undefined ? undefined : decryptField({encryptedApiKey:value.encryptedDocumentVisionApiKey})
          validateCloudAsrConfig({ ...value, apiKey: decryptField(value), ...(visionKey ? {documentVisionApiKey:visionKey} : {}) } as CloudAsrConfig)
          if (value.provider !== 'dashscope' || !qwenEndpoint(value.baseUrl)) return fail('ROTATION_CONFIG_UNREADABLE')
          if (typeof value.model !== 'string') return fail('ROTATION_CONFIG_UNREADABLE')
          if (knownAliModel(value.model) === 'qwen') {
            rotateCipher(value, payload.apiKey)
            if (visionKey) value.encryptedDocumentVisionApiKey = sealSecret(payload.apiKey)
          } else {
            // The same ASR origin can serve Qwen-VL while ASR uses a non-Qwen
            // model. Keep ASR's key and split the vision credential.
            value.encryptedDocumentVisionApiKey = sealSecret(payload.apiKey)
          }
          changed = true
        } else if (name === 'qwen-realtime-settings.json') {
          validateQwenRealtimeConfig({ ...value, apiKey: decryptField(value) } as QwenRealtimeConfig)
          if (!qwenEndpoint(value.endpoint) || value.model !== 'qwen3.8-omni-flash-realtime' || typeof value.enabled !== 'boolean') return fail('ROTATION_CONFIG_UNREADABLE')
          rotateCipher(value, payload.apiKey); changed = true
        } else {
          validateVoiceConfig({ ...value, apiKey: decryptField(value) } as VoiceConfig)
          if (value.provider === 'relay_pcm' || value.provider === 'aliyun_cosyvoice') changed = false
          else if (['qwen_audio_tts', 'qwen_voice_clone'].includes(String(value.provider))) { rotateCipher(value, payload.apiKey); changed = true }
          else return fail('ROTATION_CONFIG_UNREADABLE')
        }
        after = changed ? Buffer.from(JSON.stringify(value), 'utf8') : source
      }
      if (changed) rotated += 1
      if (changed || hadOverlay) entries.push({ name, before: before ? before.toString('base64') : name==='voice-settings.json' && !qwenOverlayImplicitVoice(root)?'':null, after: after.toString('base64') })
    } catch (error) { if (error instanceof QwenRotationError) throw error; return fail('ROTATION_CONFIG_UNREADABLE') }
  }
  // The default CosyVoice is derived from the realtime key when no explicit
  // voice file exists. Preserve that non-Qwen key in a virtual absent-file
  // overlay, without enabling an explicitly disabled voice.
  if (!entries.some(item=>item.name==='voice-settings.json') && !await bounded(join(root,'voice-settings.json'))) {
    const realtime = await new QwenRealtimeSettingsStore(root).load()
    if (realtime) {
      const voice = await new VoiceSettingsStore(root).loadEffective({ ...realtime, enabled:true })
      if (voice?.provider === 'aliyun_cosyvoice') {
        const stored = { provider:voice.provider, relayUrl:voice.relayUrl, voiceId:voice.voiceId, model:voice.model,
          workspaceId:voice.workspaceId, region:voice.region, encryptedApiKey:sealSecret(voice.apiKey) }
        entries.push({name:'voice-settings.json',before:null,after:Buffer.from(JSON.stringify(stored)).toString('base64')})
      }
    }
  }
  return { entries, rotated }
}

const JOURNAL_LIMIT = 4_000_000
const validBase64 = (value: unknown): value is string => typeof value === 'string' && value.length > 0
  && value.length <= Math.ceil(MAX_FILE * 4 / 3) + 8 && Buffer.from(value, 'base64').toString('base64') === value
function protectedRecord(value: unknown): Buffer {
  if (!safeStorage.isEncryptionAvailable()) return fail('ROTATION_STORAGE_UNAVAILABLE')
  try {
    const json = JSON.stringify(value), sealed = safeStorage.encryptString(json)
    if (safeStorage.decryptString(sealed) !== json || sealed.length > JOURNAL_LIMIT) return fail('ROTATION_STORAGE_UNAVAILABLE')
    return sealed
  } catch (error) { if (error instanceof QwenRotationError) throw error; return fail('ROTATION_STORAGE_UNAVAILABLE') }
}
function readProtected(bytes: Buffer): unknown {
  if (!safeStorage.isEncryptionAvailable()) return fail('ROTATION_STORAGE_UNAVAILABLE')
  try { return JSON.parse(safeStorage.decryptString(bytes)) } catch { return fail('ROTATION_CONFIG_UNREADABLE') }
}
function decodeJournal(bytes: Buffer): Journal {
  const value = readProtected(bytes)
  if (!exact(value, ['version', 'generation', 'order', 'payloadHash', 'keyHash', 'overlay', 'entries']) || value.version !== 1
    || typeof value.generation !== 'string' || !generationPattern.test(value.generation)
    || !Number.isSafeInteger(value.order) || Number(value.order) < 1
    || typeof value.payloadHash !== 'string' || !/^[a-f0-9]{64}$/.test(value.payloadHash)
    || typeof value.keyHash !== 'string' || !/^[a-f0-9]{64}$/.test(value.keyHash)
    || typeof value.overlay !== 'string' || value.overlay.length > JOURNAL_LIMIT * 2 || Buffer.from(value.overlay,'base64').toString('base64') !== value.overlay
    || !Array.isArray(value.entries) || value.entries.length > FILES.length || (value.entries.length > 0 && !value.overlay)) return fail('ROTATION_CONFIG_UNREADABLE')
  const names = new Set<string>()
  for (const item of value.entries) {
    if (!exact(item, ['name', 'before', 'after']) || typeof item.name !== 'string' || !FILES.includes(item.name as Name)
      || names.has(item.name) || !((item.before === null || item.before === '') && item.name === 'voice-settings.json' || validBase64(item.before)) || !(item.after === null || validBase64(item.after))) return fail('ROTATION_CONFIG_UNREADABLE')
    names.add(item.name)
  }
  return value as Journal
}
async function safeDirectory(root: string): Promise<string> {
  const directory = join(root, 'qwen-key-rotation')
  try {
    await mkdir(directory, { recursive: false }).catch(error => { if (error?.code !== 'EEXIST') throw error })
    const stat = await lstat(directory)
    if (!stat.isDirectory() || stat.isSymbolicLink()) return fail('ROTATION_CONFIG_UNREADABLE')
    return directory
  } catch (error) { if (error instanceof QwenRotationError) throw error; return fail('ROTATION_IO_UNAVAILABLE') }
}
async function receipt(directory: string, journal: Journal): Promise<'absent' | 'matches'> {
  const bytes = await bounded(join(directory, `${journal.generation}.bin`), JOURNAL_LIMIT)
  if (!bytes) return 'absent'
  if (journal.overlay && !bytes.equals(Buffer.from(journal.overlay,'base64'))) return fail('ROTATION_CONFLICT')
  const value = readProtected(bytes)
  if (!exact(value, ['version', 'generation', 'order', 'payloadHash', 'entries']) || value.version !== 1
    || value.generation !== journal.generation || value.order !== journal.order || value.payloadHash !== journal.payloadHash) return fail('ROTATION_CONFLICT')
  return 'matches'
}
async function checkWatermark(directory: string, journal: Journal): Promise<boolean> {
  const bytes = await bounded(join(directory, 'high-water.bin'))
  if (!bytes) return false
  const value = readProtected(bytes)
  if (!exact(value, ['version', 'generation', 'order', 'payloadHash']) || value.version !== 1
    || typeof value.generation !== 'string' || !generationPattern.test(value.generation)
    || !Number.isSafeInteger(value.order) || Number(value.order) < 1
    || typeof value.payloadHash !== 'string' || !/^[a-f0-9]{64}$/.test(value.payloadHash)) return fail('ROTATION_CONFIG_UNREADABLE')
  if (Number(value.order) > journal.order || (value.order === journal.order
    && (value.generation !== journal.generation || value.payloadHash !== journal.payloadHash))) return fail('ROTATION_CONFLICT')
  return value.order === journal.order
}
async function recoverPublishedLink(directory: string, journal: Journal): Promise<void> {
  const target = join(directory, `${journal.generation}.bin`), staged = join(directory, `${journal.generation}.stage`)
  let left, right
  try { left = await lstat(target) } catch (error) { if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') throw error }
  try { right = await lstat(staged) } catch (error) { if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') throw error }
  if (!left || !right) return
  if (!left.isFile() || !right.isFile() || left.isSymbolicLink() || right.isSymbolicLink()
    || left.dev !== right.dev || left.ino !== right.ino || left.nlink !== 2 || right.nlink !== 2 || right.size > JOURNAL_LIMIT
    || !journal.overlay || !(await readFile(staged)).equals(Buffer.from(journal.overlay,'base64'))) return fail('ROTATION_CONFLICT')
  await unlink(staged)
}
async function finishJournal(root: string, directory: string, journal: Journal): Promise<void> {
  await checkWatermark(directory, journal)
  await recoverPublishedLink(directory, journal)
  // Original user settings are never written by the rotation transaction.
  for (const item of journal.entries) {
    const target = join(root, item.name), before = item.before === null || item.before === '' ? null : Buffer.from(item.before, 'base64')
    const current = await bounded(target)
    if (!(before === null ? current === null : !!current && current.equals(before))) return fail('ROTATION_CONFLICT')
  }
  if (await receipt(directory, journal) === 'absent') {
    const bytes = Buffer.from(journal.overlay,'base64'), target = join(directory, `${journal.generation}.bin`)
    const staged = join(directory, `${journal.generation}.stage`)
    let prior = await bounded(staged, JOURNAL_LIMIT)
    if (prior && !prior.equals(bytes)) {
      // A crash during our own exclusive staging write leaves only a prefix.
      if (!bytes.subarray(0, prior.length).equals(prior)) return fail('ROTATION_CONFLICT')
      await unlink(staged); prior = null
    }
    if (!prior) {
      const file = await open(staged, 'wx', 0o600)
      try { await file.writeFile(bytes); await file.sync() } finally { await file.close() }
    }
    await link(staged, target)
    await unlink(staged)
  }
  const guardPath = join(root,'qwen-key-rotation-required.bin')
  const guard = await bounded(guardPath)
  if (guard) {
    const value = readProtected(guard)
    if (!exact(value,['version','required']) || value.version !== 1 || value.required !== true) return fail('ROTATION_CONFIG_UNREADABLE')
  } else {
    await atomic(guardPath, protectedRecord({version:1,required:true}))
  }
  if (!await checkWatermark(directory, journal)) {
    await atomic(join(directory, 'high-water.bin'), protectedRecord({ version: 1, generation: journal.generation, order: journal.order, payloadHash: journal.payloadHash }))
  }
}

async function finishCleanup(root: string, resources: string, journal: Journal): Promise<void> {
  // Cleanup itself is crash-recoverable. A newer installer payload is never
  // deleted by the preceding generation's resumed transaction.
  for (const [name, expected] of [['qwen-rotation.bin', journal.payloadHash], ['qwen-rotation.key', journal.keyHash]] as const) {
    const path = join(resources, name), bytes = await bounded(path)
    if (!bytes) continue
    if (sha(bytes) !== expected) return fail('ROTATION_CONFLICT')
    await unlink(path)
  }
  await unlink(join(root, JOURNAL))
}

/** Recovery precedes bootstrap and all provider loading; never contacts cloud. */
export async function recoverQwenKeyRotation(options: { userDataPath: string; resourcesPath: string }): Promise<boolean> {
  try {
    const editRecovered = recoverQwenOverlayEdit(options.userDataPath)
    const bytes = await bounded(join(options.userDataPath, JOURNAL), JOURNAL_LIMIT)
    if (!bytes) return editRecovered
    const journal = decodeJournal(bytes), directory = await safeDirectory(options.userDataPath)
    await finishJournal(options.userDataPath, directory, journal)
    await finishCleanup(options.userDataPath, options.resourcesPath, journal)
    return true
  } catch (error) { if (error instanceof QwenRotationError) throw error; return fail('ROTATION_IO_UNAVAILABLE') }
}

/** Invoke only under the desktop's provider mutation/startup fence. */
export async function rotateInstalledQwenKey(options: { userDataPath: string; resourcesPath: string }): Promise<RotationResult> {
  const root = options.userDataPath, journalPath = join(root, JOURNAL)
  try {
    const recovered = await recoverQwenKeyRotation(options)
    const payloadPath = join(options.resourcesPath, 'qwen-rotation.bin')
    const keyPath = join(options.resourcesPath, 'qwen-rotation.key')
    const payloadBytes = await bounded(payloadPath), wrappingKey = await bounded(keyPath)
    if (!payloadBytes && !wrappingKey) return { status: recovered ? 'completed' : 'not_present', changedSlots: 0 }
    if (!payloadBytes || !wrappingKey || wrappingKey.length !== 32) return fail('ROTATION_INVALID')
    try {
      const decoded = unseal(payloadBytes, wrappingKey), payloadHash = sha(payloadBytes)
      let directory: string | undefined
      try { const st = await lstat(join(root,'qwen-key-rotation')); if (!st.isDirectory() || st.isSymbolicLink()) return fail('ROTATION_CONFIG_UNREADABLE'); directory = join(root,'qwen-key-rotation') }
      catch (error) { if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') throw error }
      const journal: Journal = { version: 1, generation: decoded.generation, order: decoded.order, payloadHash, keyHash: sha(wrappingKey), overlay:'', entries: [] }
      if (directory && await receipt(directory, journal) === 'matches') {
        await atomic(journalPath, protectedRecord(journal))
        await finishCleanup(root, options.resourcesPath, journal)
        return { status: 'already_completed', changedSlots: 0 }
      }
      if (!safeStorage.isEncryptionAvailable()) return fail('ROTATION_STORAGE_UNAVAILABLE')
      if (directory) await checkWatermark(directory, journal)
      const planned = await prepare(root, decoded)
      journal.entries = planned.entries
      if (!planned.rotated) return fail('ROTATION_CONFIG_UNREADABLE')
      const overlay = { version: 1, generation: journal.generation, order: journal.order, payloadHash: journal.payloadHash,
        entries: journal.entries.map(item => ({name:item.name,sourceHash:item.before===null?'absent-implicit':item.before===''?'absent':sha(Buffer.from(item.before,'base64')),after:item.after})) }
      journal.overlay = protectedRecord(overlay).toString('base64')
      const encrypted = protectedRecord(journal)
      decodeJournal(encrypted)
      await atomic(journalPath, encrypted)
      directory = directory ?? await safeDirectory(root)
      await finishJournal(root, directory, journal)
      await finishCleanup(root, options.resourcesPath, journal)
      return { status: 'completed', changedSlots: journal.entries.length }
    } finally { wrappingKey.fill(0); payloadBytes.fill(0) }
  } catch (error) { if (error instanceof QwenRotationError) throw error; return fail('ROTATION_IO_UNAVAILABLE') }
}
