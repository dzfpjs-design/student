import { createHash, randomBytes } from 'node:crypto'
import { lstat, mkdir, open, rename, unlink } from 'node:fs/promises'
import { join } from 'node:path'
import { ReportDeliverySecretStore } from './report-delivery-secrets'
import { HouseholdProvisioningError, importHouseholdProvisioning, validateHouseholdBootstrap,
  type ProvisioningPort, type ProvisioningResult, type SmtpBootstrapDefaults } from './household-provisioning'

export type HouseholdSetupDefaults = { smtp: { host: string; port: number; username: string; from_address: string;
  use_ssl: true; starttls: false; allow_external_delivery: true }; recipient: string }
type Receipt = { version: 1; bootstrapId: string | null; payloadSha256: string | null; complete: boolean; slots: Record<string, string> }
type Binding = { bootstrapId: string; payloadSha256: string; reportCipherSha256: string }
type Stored = { defaults: SmtpBootstrapDefaults; binding: Binding | null; contentSha256: string }
const DEFAULTS = 'household-setup-defaults.json', RECEIPT = 'household-provisioning-receipt.json'
const PAYLOAD = 'household-bootstrap.bin', KEY = 'household-bootstrap.key'
const SLOTS = ['model', 'asr', 'qwenRealtime', 'smtpPassword', 'wecomWebhook', 'smtpDefaults']
const STATES = ['pending', 'inflight', 'imported', 'preserved', 'suppressed']
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const HASH = /^[a-f0-9]{64}$/
const io = (): never => { throw new HouseholdProvisioningError('HOUSEHOLD_BOOTSTRAP_IO_UNAVAILABLE') }
const digest = (bytes: Buffer): string => createHash('sha256').update(bytes).digest('hex')
const keys = (value: unknown, names: string[]): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).sort().join(',') === [...names].sort().join(',')

async function bounded(path: string, limit: number): Promise<Buffer | null> {
  try {
    const before = await lstat(path)
    if (!before.isFile() || before.isSymbolicLink() || before.size > limit) return io()
    const file = await open(path, 'r')
    try {
      const stat = await file.stat()
      if (!stat.isFile() || stat.size > limit || stat.ino !== before.ino || stat.dev !== before.dev) return io()
      const buffer = Buffer.alloc(limit + 1), { bytesRead } = await file.read(buffer, 0, buffer.length, 0)
      if (bytesRead > limit) return io()
      return buffer.subarray(0, bytesRead)
    } finally { await file.close() }
  } catch (error) { if ((error as NodeJS.ErrnoException)?.code === 'ENOENT') return null; return io() }
}

async function receipt(root: string): Promise<Receipt | null> {
  try {
    const bytes = await bounded(join(root, RECEIPT), 8192)
    if (!bytes) return null
    const v: unknown = JSON.parse(bytes.toString('utf8'))
    if (!keys(v, ['version', 'bootstrapId', 'payloadSha256', 'complete', 'slots']) || v.version !== 1 || typeof v.complete !== 'boolean') throw Error()
    if ((v.bootstrapId === null) !== (v.payloadSha256 === null)
      || (v.bootstrapId !== null && (typeof v.bootstrapId !== 'string' || !UUID.test(v.bootstrapId)))
      || (v.payloadSha256 !== null && (typeof v.payloadSha256 !== 'string' || !HASH.test(v.payloadSha256)))) throw Error()
    if (!v.slots || typeof v.slots !== 'object' || Array.isArray(v.slots)) throw Error()
    const slots = v.slots as Record<string, unknown>
    if (Object.keys(slots).some(key => !SLOTS.includes(key)) || Object.values(slots).some(state => typeof state !== 'string' || !STATES.includes(state))) throw Error()
    if (v.complete && (!v.bootstrapId || !Object.keys(slots).length || Object.values(slots).some(state => state === 'pending' || state === 'inflight'))) throw Error()
    return v as Receipt
  } catch { throw new HouseholdProvisioningError('HOUSEHOLD_BOOTSTRAP_RECEIPT_UNREADABLE') }
}

function defaults(value: unknown): SmtpBootstrapDefaults {
  return validateHouseholdBootstrap({ version: 1, bootstrapId: '00000000-0000-4000-8000-000000000000', settings: { smtpDefaults: value } }).settings.smtpDefaults!
}
async function stored(root: string): Promise<Stored | null> {
  const bytes = await bounded(join(root, DEFAULTS), 8192)
  if (!bytes) return null
  const v: unknown = JSON.parse(bytes.toString('utf8'))
  if (!keys(v, ['version', 'defaults', 'binding']) || v.version !== 1) return io()
  const value = defaults(v.defaults)
  let binding: Binding | null = null
  // Stale/invalid binding never makes readable existing preferences "missing".
  if (keys(v.binding, ['bootstrapId', 'payloadSha256', 'reportCipherSha256'])
    && typeof v.binding.bootstrapId === 'string' && UUID.test(v.binding.bootstrapId)
    && typeof v.binding.payloadSha256 === 'string' && HASH.test(v.binding.payloadSha256)
    && typeof v.binding.reportCipherSha256 === 'string' && HASH.test(v.binding.reportCipherSha256)) binding = v.binding as Binding
  return { defaults: value, binding, contentSha256: digest(bytes) }
}
async function reportCipher(root: string): Promise<string | null> {
  const bytes = await bounded(join(root, 'report-delivery-secrets.json'), 65536)
  return bytes ? digest(bytes) : null
}
async function atomic(root: string, value: unknown): Promise<void> {
  await mkdir(root, { recursive: true })
  const path = join(root, DEFAULTS), temporary = `${path}.${process.pid}.${randomBytes(8).toString('hex')}.tmp`
  const file = await open(temporary, 'wx', 0o600)
  try { await file.writeFile(JSON.stringify(value)); await file.sync() } finally { await file.close() }
  try {
    await rename(temporary, path)
    if (process.platform !== 'win32') { const dir = await open(root, 'r'); try { await dir.sync() } finally { await dir.close() } }
  } finally { await unlink(temporary).catch(() => undefined) }
}

/** Called by the serialized importer before renderer/settings mutation exists. */
export function createHouseholdSetupDefaultsPort(root: string): ProvisioningPort<SmtpBootstrapDefaults> {
  return {
    inspect: async () => { try { const value = await stored(root); return value ? { state: 'configured', value: value.defaults } : { state: 'not_configured' } } catch { return { state: 'unreadable' } } },
    save: async input => {
      try {
        const value = defaults(input), provenance = await receipt(root), hash = await reportCipher(root)
        const binding: Binding | null = provenance?.bootstrapId && provenance.payloadSha256 && hash
          ? { bootstrapId: provenance.bootstrapId, payloadSha256: provenance.payloadSha256, reportCipherSha256: hash } : null
        await atomic(root, { version: 1, defaults: value, binding })
      } catch { return io() }
    },
  }
}

function importedSameOrigin(r: Receipt | null, binding: Binding): boolean {
  return r?.bootstrapId === binding.bootstrapId && r.payloadSha256 === binding.payloadSha256
    && r.slots.smtpPassword === 'imported' && r.slots.smtpDefaults === 'imported'
}
/** A current snapshot can authorize omission of a password, never reveal one. */
export async function getHouseholdSetupDefaults(root: string,
  report: Pick<ReportDeliverySecretStore, 'inspect'> = new ReportDeliverySecretStore(root)): Promise<HouseholdSetupDefaults | null> {
  try {
    const value = await stored(root)
    if (!value?.binding || !importedSameOrigin(await receipt(root), value.binding)) return null
    const before = await reportCipher(root)
    if (before !== value.binding.reportCipherSha256) return null
    const { summary } = await report.inspect()
    if (summary.state !== 'ready' || summary.smtpPasswordConfigured !== true) return null
    const after = await reportCipher(root), latest = await stored(root)
    if (after !== before || latest?.contentSha256 !== value.contentSha256 || !importedSameOrigin(await receipt(root), value.binding)) return null
    const smtp = value.defaults
    return { smtp: { host: smtp.host, port: smtp.port, username: smtp.username, from_address: smtp.fromAddress,
      use_ssl: true, starttls: false, allow_external_delivery: true }, recipient: smtp.recipient }
  } catch { return null }
}

/** Fixed installed-resource names only. No PIN, privacy or backend start here. */
export async function importInstalledHouseholdSetup(options: { resourcesPath: string; userDataPath: string;
  smtpPort: ProvisioningPort<SmtpBootstrapDefaults> }): Promise<ProvisioningResult> {
  let key: Buffer | null = null, payload: Buffer | null = null
  try {
    const prior = await receipt(options.userDataPath)
    // A completed receipt must not need a retained extraction key to stay closed.
    if (!prior?.complete) {
      payload = await bounded(join(options.resourcesPath, PAYLOAD), 65536)
      key = await bounded(join(options.resourcesPath, KEY), 32)
      if ((payload === null) !== (key === null) || (key && key.length !== 32)) throw new HouseholdProvisioningError('HOUSEHOLD_BOOTSTRAP_INVALID')
    }
    const result = await importHouseholdProvisioning({ ...options, key: key ?? Buffer.alloc(0) })
    if (result.status === 'complete' || result.status === 'already_processed') {
      try { await unlink(join(options.resourcesPath, KEY)) } catch (error) { if ((error as NodeJS.ErrnoException)?.code !== 'ENOENT') return io() }
    }
    return result
  } catch (error) {
    if (error instanceof HouseholdProvisioningError) throw error
    return io()
  } finally { key?.fill(0); payload?.fill(0) }
}
