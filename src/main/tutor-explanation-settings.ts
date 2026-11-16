import { safeStorage } from 'electron'
import { createDecipheriv, createHash, randomBytes } from 'node:crypto'
import { link, lstat, mkdir, open, rename, rmdir, unlink } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'

import { validateTutorEndpoint, validateTutorModel, type TutorExplanationConfig, type TutorExplanationInput, type TutorExplanationSummary } from '../shared/tutorExplanation'
export { validateTutorEndpoint, validateTutorModel } from '../shared/tutorExplanation'
export type { TutorExplanationConfig, TutorExplanationInput, TutorExplanationSummary } from '../shared/tutorExplanation'
const magic = Buffer.from('HSCTUT01')
const storedName = 'tutor-explanation-settings.json'
const journalName = 'tutor-explanation-settings.pending'
const payloadName = 'tutor-explanation.bin'
const keyName = 'tutor-explanation.key'

const absent = (error: unknown): boolean => (error as NodeJS.ErrnoException)?.code === 'ENOENT'
const locks = new Map<string, Promise<void>>()
function serialized<T>(directory: string, work: () => Promise<T>): Promise<T> {
  const lockKey = resolve(directory)
  const previous = locks.get(lockKey) ?? Promise.resolve()
  const result = previous.catch(() => undefined).then(async () => {
    const release = await acquireProcessLock(lockKey)
    try { return await work() } finally { await release() }
  })
  const finished = result.then(() => undefined, () => undefined)
  locks.set(lockKey, finished)
  void finished.then(() => { if (locks.get(lockKey) === finished) locks.delete(lockKey) })
  return result
}
const digest = (value: Buffer): string => createHash('sha256').update(value).digest('hex')

function ownerAlive(bytes: Buffer): boolean {
  let owner: { version?: unknown; pid?: unknown; nonce?: unknown }
  try { owner = JSON.parse(bytes.toString('utf8')) } catch { throw Error('TUTOR_EXPLANATION_LOCK_RECOVERY_REQUIRED') }
  if (!owner || owner.version !== 1 || typeof owner.pid !== 'number' || !Number.isSafeInteger(owner.pid) || owner.pid < 1
    || typeof owner.nonce !== 'string' || !/^[a-f0-9]{32}$/.test(owner.nonce)) throw Error('TUTOR_EXPLANATION_LOCK_RECOVERY_REQUIRED')
  try { process.kill(owner.pid, 0); return true }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ESRCH') return false
    if ((error as NodeJS.ErrnoException).code === 'EPERM') return true
    throw Error('TUTOR_EXPLANATION_LOCK_RECOVERY_REQUIRED')
  }
}

async function acquireProcessLock(directory: string): Promise<() => Promise<void>> {
  const target = join(directory, 'tutor-explanation-settings.lock')
  const recovery = `${target}.recovery`
  const owner = Buffer.from(JSON.stringify({ version: 1, pid: process.pid, nonce: randomBytes(16).toString('hex') }))
  const staged = `${target}.${randomBytes(8).toString('hex')}.tmp`
  const file = await open(staged, 'wx', 0o600)
  try { try { await file.writeFile(owner); await file.sync() } finally { await file.close() }
    for (let attempts = 0; attempts < 5; attempts++) {
      let linked = false
      try {
        await link(staged, target)
        linked = true
        await syncDirectory(directory)
        return async () => {
          const current = await readBounded(target, 1024)
          if (!current?.equals(owner)) throw Error('TUTOR_EXPLANATION_LOCK_RECOVERY_REQUIRED')
          await unlink(target)
          await syncDirectory(directory)
        }
      } catch (error) {
        if (linked) {
          // The lock exists even though its directory sync failed. Remove only
          // our own token so an ordinary transient failure cannot wedge us.
          const current = await readBounded(target, 1024)
          if (!current?.equals(owner)) throw Error('TUTOR_EXPLANATION_LOCK_RECOVERY_REQUIRED')
          await unlink(target)
          await syncDirectory(directory).catch(() => undefined)
          throw error
        }
        if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
      }
      const seen = await readBounded(target, 1024)
      if (!seen) continue
      if (ownerAlive(seen)) throw Error('TUTOR_EXPLANATION_BUSY')
      try { await mkdir(recovery, { mode: 0o700 }) }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw Error('TUTOR_EXPLANATION_RECOVERY_MARKER_BLOCKED')
        throw error
      }
      try {
        const rechecked = await readBounded(target, 1024)
        if (!rechecked) continue
        if (!rechecked.equals(seen) || ownerAlive(rechecked)) throw Error('TUTOR_EXPLANATION_BUSY')
        // The owner is dead and recovery is exclusive; move only this stale lock.
        const quarantine = `${target}.${randomBytes(8).toString('hex')}.stale`
        await rename(target, quarantine)
        await syncDirectory(directory)
        await unlink(quarantine)
      } finally { await rmdir(recovery) }
    }
    throw Error('TUTOR_EXPLANATION_BUSY')
  } finally { await unlink(staged).catch(() => undefined) }
}
async function syncDirectory(directory: string): Promise<void> {
  // Node does not provide a portable directory fsync handle on Windows.
  if (process.platform === 'win32') return
  const handle = await open(directory, 'r')
  try { await handle.sync() } finally { await handle.close() }
}

async function atomicWrite(target: string, bytes: Buffer): Promise<void> {
  const temporary = `${target}.${randomBytes(8).toString('hex')}.tmp`
  const file = await open(temporary, 'wx', 0o600)
  try {
    try { await file.writeFile(bytes); await file.sync() } finally { await file.close() }
    await rename(temporary, target)
    await syncDirectory(dirname(target))
  } finally { await unlink(temporary).catch(() => undefined) }
}

/** Pending is uncommitted; the journal contains only encrypted old bytes and digests. */
async function recoverPending(userData: string): Promise<void> {
  const journal = join(userData, journalName), target = join(userData, storedName)
  const bytes = await readBounded(journal, 32768)
  if (!bytes) return
  let record: { version?: unknown; state?: unknown; previous?: unknown; previousHash?: unknown; nextHash?: unknown }
  try { record = JSON.parse(bytes.toString('utf8')) } catch { throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED') }
  if (!record || record.version !== 1 || (record.state !== 'pending' && record.state !== 'committed') || (record.previous !== null && typeof record.previous !== 'string')
    || typeof record.nextHash !== 'string' || !/^[a-f0-9]{64}$/.test(record.nextHash)) throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED')
  const previous = record.previous === null ? null : Buffer.from(record.previous as string, 'base64')
  if (previous && (!previous.length || previous.length > 8192 || previous.toString('base64') !== record.previous || digest(previous) !== record.previousHash)
    || !previous && record.previousHash !== null) throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED')
  const current = await readBounded(target, 8192)
  const matchesOld = previous ? Boolean(current?.equals(previous)) : current === null
  const matchesNew = Boolean(current && digest(current) === record.nextHash)
  if (record.state === 'committed') {
    if (!matchesNew) throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED')
    try { await unlink(journal); await syncDirectory(userData) } catch { throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED') }
    return
  }
  if (!matchesOld && !matchesNew) throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED')
  try {
    if (!matchesOld) {
      if (previous) await atomicWrite(target, previous)
      else await unlink(target)
    }
    const restored = await readBounded(target, 8192)
    if (previous ? !restored?.equals(previous) : restored !== null) throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED')
    await unlink(journal)
    await syncDirectory(userData)
  } catch { throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED') }
}
function keyValue(value: unknown): string {
  if (typeof value !== 'string' || !/^[!-~]{16,512}$/.test(value)) throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID')
  return value
}

export function summarizeTutorExplanation(config: TutorExplanationConfig | null): TutorExplanationSummary {
  return { configured: Boolean(config?.apiKey && config.baseUrl && config.model), keyConfigured: Boolean(config?.apiKey),
    requiresEndpoint: Boolean(config?.apiKey && !config.baseUrl), baseUrl: config?.baseUrl || '', model: config?.model || 'deepseek-v4.1-flash' }
}

async function readBounded(path: string, max: number): Promise<Buffer | null> {
  try {
    const before = await lstat(path)
    if (!before.isFile() || before.isSymbolicLink() || before.size > max) throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID')
    const file = await open(path, 'r')
    try {
      const held = await file.stat()
      if (!held.isFile() || held.dev !== before.dev || held.ino !== before.ino || held.size !== before.size) {
        throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID')
      }
      const bytes = Buffer.alloc(max + 1)
      const { bytesRead } = await file.read(bytes, 0, bytes.length, 0)
      if (bytesRead !== held.size) throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID')
      return bytes.subarray(0, bytesRead)
    } finally { await file.close() }
  } catch (error) { if (absent(error)) return null; throw error }
}

async function loadStored(userData: string): Promise<TutorExplanationConfig | null> {
  const bytes = await readBounded(join(userData, storedName), 8192)
  if (!bytes) return null
  let stored: { version?: number; encryptedApiKey?: unknown; encryptedConfig?: unknown }
  try { stored = JSON.parse(bytes.toString('utf8')) } catch { throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID') }
  if (!stored || !safeStorage.isEncryptionAvailable()) throw new Error('TUTOR_EXPLANATION_STORAGE_UNAVAILABLE')
  const encoded = stored.version === 1 ? stored.encryptedApiKey : stored.version === 2 ? stored.encryptedConfig : null
  if (typeof encoded !== 'string') throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID')
  const encrypted = Buffer.from(encoded, 'base64')
  if (!encrypted.length || encrypted.toString('base64') !== encoded) throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID')
  let decrypted: string
  try { decrypted = safeStorage.decryptString(encrypted) } catch { throw new Error('TUTOR_EXPLANATION_STORAGE_UNAVAILABLE') }
  if (stored.version === 1) return { apiKey: keyValue(decrypted) }
  let value: TutorExplanationInput
  try { value = JSON.parse(decrypted) } catch { throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID') }
  return { apiKey: keyValue(value.apiKey), baseUrl: validateTutorEndpoint(value.baseUrl), model: validateTutorModel(value.model) }
}

async function writeStored(userData: string, config: TutorExplanationConfig, authorize: () => Promise<void> = async () => {}): Promise<void> {
  if (!safeStorage.isEncryptionAvailable()) throw new Error('TUTOR_EXPLANATION_STORAGE_UNAVAILABLE')
  await recoverPending(userData)
  const stored = config.baseUrl && config.model
    ? { version: 2, encryptedConfig: safeStorage.encryptString(JSON.stringify(config)).toString('base64') }
    : { version: 1, encryptedApiKey: safeStorage.encryptString(config.apiKey).toString('base64') }
  const target = join(userData, storedName), journal = join(userData, journalName)
  const previous = await readBounded(target, 8192)
  const encoded = Buffer.from(JSON.stringify(stored), 'utf8')
  const record = { version: 1, state: 'pending', previous: previous?.toString('base64') ?? null,
    previousHash: previous ? digest(previous) : null, nextHash: digest(encoded) }
  try {
    await authorize()
    if (!safeStorage.isEncryptionAvailable()) throw Error('TUTOR_EXPLANATION_STORAGE_UNAVAILABLE')
    const current = await readBounded(target, 8192)
    if (Boolean(previous) !== Boolean(current) || (previous && current && !previous.equals(current))) throw Error('TUTOR_EXPLANATION_CONFIG_CHANGED')
    await atomicWrite(journal, Buffer.from(JSON.stringify(record)))
    await authorize()
    const beforeReplacement = await readBounded(target, 8192)
    if (previous ? !beforeReplacement?.equals(previous) : beforeReplacement !== null) throw Error('TUTOR_EXPLANATION_CONFIG_CHANGED')
    await atomicWrite(target, encoded)
    await authorize()
    const verified = await loadStored(userData)
    if (verified?.apiKey !== config.apiKey || verified?.baseUrl !== config.baseUrl || verified?.model !== config.model) throw Error('TUTOR_EXPLANATION_STORAGE_UNAVAILABLE')
    // This authorized, durable journal transition is the explicit commit point.
    await atomicWrite(journal, Buffer.from(JSON.stringify({ ...record, state: 'committed' })))
    await unlink(journal)
    await syncDirectory(userData)
  } catch (error) {
    try { await recoverPending(userData) } catch { throw Error('TUTOR_EXPLANATION_RECOVERY_REQUIRED') }
    throw error
  }
}

export async function saveTutorExplanationConfig(userData: string, input: TutorExplanationInput, authorize?: () => Promise<void>): Promise<TutorExplanationSummary> {
  return serialized(userData, async () => {
  await recoverPending(userData)
  if (!input || typeof input !== 'object') throw Error('TUTOR_EXPLANATION_CONFIG_INVALID')
  const baseUrl = validateTutorEndpoint(input.baseUrl), model = validateTutorModel(input.model)
  const previous = await loadStored(userData)
  const apiKey = input.apiKey ? keyValue(input.apiKey) : previous?.apiKey
  if (!apiKey) throw Error('TUTOR_EXPLANATION_KEY_REQUIRED')
  const config = { apiKey, baseUrl, model }
  await writeStored(userData, config, authorize)
  return summarizeTutorExplanation(config)
  })
}

/** The private installer supplies this one teaching-only credential. */
export async function loadTutorExplanationConfig(userData: string, installedResources?: string): Promise<TutorExplanationConfig | null> {
  return serialized(userData, async () => {
    await recoverPending(userData)
    return loadTutorExplanationConfigUnlocked(userData, installedResources)
  })
}
async function loadTutorExplanationConfigUnlocked(userData: string, installedResources?: string): Promise<TutorExplanationConfig | null> {
  if (!installedResources) return loadStored(userData)
  const payloadPath = join(installedResources, payloadName), keyPath = join(installedResources, keyName)
  const payload = await readBounded(payloadPath, 1024)
  if (!payload) {
    const stored = await loadStored(userData)
    if (stored) await unlink(keyPath).catch(error => { if (!absent(error)) throw error })
    return stored
  }
  let secret: Buffer | null = null, plaintext: Buffer | undefined
  try {
    secret = await readBounded(keyPath, 32)
    if (!secret || secret.length !== 32 || payload.length <= 36 || !payload.subarray(0, 8).equals(magic)) {
      throw new Error('TUTOR_EXPLANATION_CONFIG_INVALID')
    }
    if (!safeStorage.isEncryptionAvailable()) throw new Error('TUTOR_EXPLANATION_STORAGE_UNAVAILABLE')
    const decipher = createDecipheriv('aes-256-gcm', secret, payload.subarray(8, 20))
    decipher.setAAD(magic)
    decipher.setAuthTag(payload.subarray(20, 36))
    plaintext = Buffer.concat([decipher.update(payload.subarray(36)), decipher.final()])
    const apiKey = keyValue(plaintext.toString('utf8'))
    const previous = await loadStored(userData)
    // A private installer may rotate a key; retain a previously authorized URL.
    await writeStored(userData, previous?.baseUrl && previous.model
      ? { apiKey, baseUrl: previous.baseUrl, model: previous.model } : { apiKey })
    await unlink(payloadPath)
    await unlink(keyPath)
    return (await loadStored(userData))
  } catch {
    throw new Error('TUTOR_EXPLANATION_PROVISIONING_FAILED')
  } finally { secret?.fill(0); payload.fill(0); plaintext?.fill(0) }
}
