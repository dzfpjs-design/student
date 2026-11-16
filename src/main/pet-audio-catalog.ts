import { safeStorage } from 'electron'
import { createHash, createPublicKey, randomBytes, verify } from 'node:crypto'
import { lstatSync, readFileSync, writeFileSync, renameSync, unlinkSync, mkdirSync, openSync, readSync, fstatSync, closeSync, fsyncSync, writeSync, constants } from 'node:fs'
import { basename, dirname, join, resolve } from 'node:path'
import type { PetCatalogControl } from '../shared/petAudioSettings'
import { PET_CATALOG_SCHEMA, PET_CATALOG_MAX_BYTES, PET_CATALOG_TRUST_KEYS, canonicalPetCatalog, petCatalogTariff,
  type PetCatalogEnvelope, type PetCatalogPayload, type PetCatalogSelection, type PetCatalogPreview } from '../shared/petAudioCatalog'
const need = (v: unknown, code: string): void => { if (!v) throw Error(code) }
const sha = (x: Buffer): string => createHash('sha256').update(x).digest('hex')
/** Opaque revocation marker only: these bytes never confer catalogue or price trust. */
export function readPetCatalogControl(file: string): PetCatalogControl {
  need(typeof file === 'string' && resolve(file) === file && basename(file) === 'pet-audio-catalog-control.bin', 'PET_CATALOG_CONTROL_PATH')
  for (let parent = dirname(file); ; parent = dirname(parent)) {
    const entry = lstatSync(parent)
    need(entry.isDirectory() && !entry.isSymbolicLink(), 'PET_CATALOG_CONTROL_ALIAS')
    if (dirname(parent) === parent) break
  }
  try { return { file, state: sha(readPetCatalogImport(file)) } }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    // Only the fixed leaf may be absent. A disappearing/unreadable parent is an error.
    const parent = lstatSync(dirname(file)); need(parent.isDirectory() && !parent.isSymbolicLink(), 'PET_CATALOG_CONTROL_ALIAS')
    try { lstatSync(file) } catch (again) { if ((again as NodeJS.ErrnoException).code === 'ENOENT') return { file, state: 'ABSENT' }; throw again }
    throw Error('PET_CATALOG_CONTROL_CHANGED')
  }
}
export function verifyPetCatalog(bytes: Buffer, now = Date.now(), trusted = PET_CATALOG_TRUST_KEYS) {
  need(bytes.length > 0 && bytes.length <= PET_CATALOG_MAX_BYTES, 'PET_CATALOG_SIZE')
  const envelope = JSON.parse(bytes.toString('utf8')) as PetCatalogEnvelope
  need(envelope && Object.keys(envelope).sort().join('|') === ['schema','key_id','payload_base64','signature_base64'].sort().join('|')
    && envelope.schema === PET_CATALOG_SCHEMA && typeof envelope.key_id === 'string' && /^[A-Za-z0-9-]{8,100}$/.test(envelope.key_id)
    && typeof envelope.payload_base64 === 'string' && typeof envelope.signature_base64 === 'string', 'PET_CATALOG_ENVELOPE')
  need(canonicalPetCatalog(envelope) === bytes.toString('utf8'), 'PET_CATALOG_ENVELOPE_CANONICAL')
  const key = trusted[envelope.key_id]; need(key, 'PET_CATALOG_SIGNER_NOT_CONFIGURED')
  const body = Buffer.from(envelope.payload_base64, 'base64'), signature = Buffer.from(envelope.signature_base64, 'base64')
  need(body.toString('base64') === envelope.payload_base64 && signature.length === 64 && signature.toString('base64') === envelope.signature_base64
    && body.length > 0 && body.every(x => x >= 32 && x <= 126), 'PET_CATALOG_ENCODING')
  const publicKey = createPublicKey(key.publicKeyPem); need(publicKey.asymmetricKeyType === 'ed25519', 'PET_CATALOG_KEY_TYPE')
  need(verify(null, body, publicKey, signature), 'PET_CATALOG_SIGNATURE')
  const payload = JSON.parse(body.toString('ascii')) as PetCatalogPayload
  need(canonicalPetCatalog(payload) === body.toString('ascii'), 'PET_CATALOG_CANONICAL')
  return { envelope, payload, tariff: petCatalogTariff(payload, now), issuer: key.issuer, sha256: sha(bytes) }
}
export function readPetCatalog(file: string, digest: string, now = Date.now(), trusted = PET_CATALOG_TRUST_KEYS) {
  need(typeof file === 'string' && resolve(file) === file && /^[a-f0-9]{64}$/.test(digest), 'PET_CATALOG_FILE')
  const bytes = readPetCatalogImport(file)
  need(sha(bytes) === digest, 'PET_CATALOG_FILE_CHANGED')
  return verifyPetCatalog(bytes, now, trusted)
}
export function readPetCatalogImport(file: string): Buffer {
  const before = lstatSync(file)
  need(before.isFile() && !before.isSymbolicLink() && before.nlink === 1 && before.size > 0 && before.size <= PET_CATALOG_MAX_BYTES, 'PET_CATALOG_FILE')
  const fd = openSync(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0))
  try {
    const held = fstatSync(fd), bytes = Buffer.alloc(PET_CATALOG_MAX_BYTES + 1)
    need(held.dev === before.dev && held.ino === before.ino && held.size === before.size && held.nlink === 1, 'PET_CATALOG_FILE_CHANGED')
    let total = 0, n = 0
    do { n = readSync(fd, bytes, total, bytes.length - total, null); total += n } while (n > 0 && total < bytes.length)
    const after = lstatSync(file), end = fstatSync(fd)
    need(total === before.size && total <= PET_CATALOG_MAX_BYTES && before.dev === after.dev && before.ino === after.ino
      && before.size === after.size && before.mtimeMs === after.mtimeMs && held.size === end.size && held.mtimeMs === end.mtimeMs && end.nlink === 1, 'PET_CATALOG_FILE_CHANGED')
    return bytes.subarray(0, total)
  } finally { closeSync(fd) }
}
type Floor = { version: 1; revision: number; sha256: string; lastUtc: number }
/** Dedicated local DPAPI control state, deliberately outside business DB/media backup. */
export class PetAudioCatalogStore {
  private readonly control: string
  private readonly folder: string
  private readonly staged = new Map<string, { bytes: Buffer; scope: string }>()
  constructor(directory: string, private readonly trusted = PET_CATALOG_TRUST_KEYS) {
    this.control = join(directory, 'pet-audio-catalog-control.bin'); this.folder = join(directory, 'pet-audio-catalogs')
  }
  controlSnapshot(): PetCatalogControl { return readPetCatalogControl(this.control) }
  private floor(): Floor | null {
    let bytes: Buffer
    try { bytes = readPetCatalogImport(this.control) } catch (e) { if ((e as NodeJS.ErrnoException).code === 'ENOENT') return null; throw e }
    need(safeStorage.isEncryptionAvailable(), 'PET_CATALOG_CONTROL_UNAVAILABLE')
    const x = JSON.parse(safeStorage.decryptString(bytes)) as Floor
    need(x && typeof x === 'object' && Object.keys(x).sort().join('|') === 'lastUtc|revision|sha256|version'
      && x.version === 1 && Number.isSafeInteger(x.revision) && x.revision > 0 && /^[a-f0-9]{64}$/.test(x.sha256)
      && Number.isSafeInteger(x.lastUtc) && x.lastUtc > 0, 'PET_CATALOG_CONTROL_INVALID'); return x
  }
  preview(bytes: Buffer, scope: string): PetCatalogPreview {
    const checked = verifyPetCatalog(bytes, Date.now(), this.trusted), floor = this.floor()
    if (floor) need(checked.payload.revision > floor.revision || checked.payload.revision === floor.revision && checked.sha256 === floor.sha256, 'PET_CATALOG_ROLLBACK')
    need(this.staged.size < 8 && scope.length > 0, 'PET_CATALOG_PREVIEW_BOUND')
    const token = randomBytes(24).toString('hex'); this.staged.set(token, { bytes: Buffer.from(bytes), scope })
    return { token, sha256: checked.sha256, revision: checked.payload.revision, issuer: checked.issuer, tariff: checked.tariff, sourceUrls: checked.payload.source_urls }
  }
  inspect(token: string, scope: string) {
    const stage = this.staged.get(token); need(stage && stage.scope === scope, 'PET_CATALOG_PREVIEW_STALE')
    return verifyPetCatalog(stage!.bytes, Date.now(), this.trusted)
  }
  /** Called only inside original settings CAS section, after live parent authorization. */
  activate(token: string, scope: string): PetCatalogSelection {
    const stage = this.staged.get(token); need(stage && stage.scope === scope, 'PET_CATALOG_PREVIEW_STALE')
    const checked = verifyPetCatalog(stage!.bytes, Date.now(), this.trusted), floor = this.floor(), now = Date.now()
    if (floor) need(now >= floor.lastUtc && (checked.payload.revision > floor.revision || checked.payload.revision === floor.revision && checked.sha256 === floor.sha256), 'PET_CATALOG_ROLLBACK')
    need(safeStorage.isEncryptionAvailable(), 'PET_CATALOG_CONTROL_UNAVAILABLE')
    mkdirSync(this.folder, { recursive: true, mode: 0o700 }); const file = join(this.folder, checked.sha256 + '.json')
    try { writeFileSync(file, stage!.bytes, { flag: 'wx', mode: 0o600 }) } catch (e) { if ((e as NodeJS.ErrnoException).code !== 'EEXIST') throw e }
    readPetCatalog(file, checked.sha256, now, this.trusted)
    const next: Floor = { version: 1, revision: checked.payload.revision, sha256: checked.sha256, lastUtc: now }
    const encoded = safeStorage.encryptString(JSON.stringify(next)); need(safeStorage.decryptString(encoded) === JSON.stringify(next), 'PET_CATALOG_CONTROL_UNCONFIRMED')
    const temporary = this.control + '.' + randomBytes(12).toString('hex') + '.tmp'
    try {
      const fd = openSync(temporary, 'wx', 0o600)
      try { let offset = 0; while (offset < encoded.length) offset += writeSync(fd, encoded, offset, encoded.length - offset); fsyncSync(fd) } finally { closeSync(fd) }
      renameSync(temporary, this.control)
    }
    finally { try { unlinkSync(temporary) } catch (e) { if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e } }
    need(JSON.stringify(this.floor()) === JSON.stringify(next), 'PET_CATALOG_CONTROL_UNCONFIRMED')
    return { file, sha256: checked.sha256, revision: checked.payload.revision }
  }
  current(selection: PetCatalogSelection) {
    const floor = this.floor(); need(floor && floor.revision === selection.revision && floor.sha256 === selection.sha256
      && Date.now() >= floor.lastUtc && resolve(selection.file) === join(this.folder, selection.sha256 + '.json'), 'PET_CATALOG_CONTROL_REQUIRED')
    return readPetCatalog(selection.file, selection.sha256, Date.now(), this.trusted)
  }
  assertPrior(selection?: PetCatalogSelection): void {
    const floor = this.floor()
    if (selection) need(floor && (floor.revision > selection.revision || floor.revision === selection.revision && floor.sha256 === selection.sha256), 'PET_CATALOG_CONTROL_REQUIRED')
  }
  builtinAllowed(): boolean { return this.floor() === null }
}
