import * as fs from 'node:fs'
import * as path from 'node:path'
import { createHash, randomBytes } from 'node:crypto'
import type { ParentDraftRecoveryScope as Scope, ParentDraftRecoverySnapshot as Snapshot, ParentDraftRecoveryPrepared as Prepared, ParentDraftRecoveryCheckpoint as Checkpoint, ParentDraftRecoveryRecovered as Recovered } from '../shared/parentDraftRecovery'
import { canonical, identity, isDirty, need, ParentDraftRecoveryError, validateSnapshot } from './parent-draft-recovery-validation'
export { ParentDraftRecoveryError } from './parent-draft-recovery-validation'

export interface ParentDraftRecoveryCodec { assertAvailable(): void; encrypt(plain: string): Buffer; decrypt(cipher: Buffer): string }
export interface ParentDraftSafeStorage {
  isEncryptionAvailable(): boolean
  getSelectedStorageBackend?(): string
  encryptString(plain: string): Buffer
  decryptString(cipher: Buffer): string
}
/** Root passes the real Electron safeStorage. No fallback codec or plaintext backend. */
export function createSafeStorageDraftCodec(safeStorage: ParentDraftSafeStorage): ParentDraftRecoveryCodec {
  const assertAvailable = () => {
    try { need(safeStorage.isEncryptionAvailable() === true && safeStorage.getSelectedStorageBackend?.() !== 'basic_text', 'DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE') }
    catch { throw new ParentDraftRecoveryError('DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE') }
  }
  return {
    assertAvailable,
    encrypt(plain) { assertAvailable(); return safeStorage.encryptString(plain) },
    decrypt(cipher) { assertAvailable(); return safeStorage.decryptString(cipher) }
  }
}
const MAX_CIPHER = 4 * 1024 * 1024 + 65536
const MAX_FILES = 16
const finalName = /^[a-f0-9]{64}\.pdr$/
const revision = (x: unknown): x is number => Number.isSafeInteger(x) && (x as number) >= 0
function safe<T>(action: () => T): T {
  try { return action() } catch (error) {
    if (error instanceof ParentDraftRecoveryError) throw error
    throw new ParentDraftRecoveryError('DRAFT_RECOVERY_IO')
  }
}
function exactKeys(v: any, expected: string[]): boolean {
  return v !== null && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === expected.length && expected.every(k => Object.hasOwn(v, k))
}
type Stored = { localRevision: number; snapshot: Snapshot | null; orphan?: { filename: string; cipher: Buffer } }
type Inventory = { files: Map<string, fs.Stats>; orphans: string[] }
export interface ParentDraftRecoveryAtomicIO {
  /** Trusted Root adapter only: both paths are Store-generated, same fixed private directory. */
  replaceAndFlush(tempPath: string, finalPath: string): void
}
function flushDirectory(directory: string): void {
  const fd = fs.openSync(directory, fs.constants.O_RDONLY)
  try { fs.fsyncSync(fd) } finally { fs.closeSync(fd) }
}
const defaultAtomicIO: ParentDraftRecoveryAtomicIO = {
  replaceAndFlush(temp, final) { fs.renameSync(temp, final); flushDirectory(path.dirname(final)) }
}
export type ParentDraftRecoveryStoreOptions = {
  userDataPath: string; profileId: string; codec: ParentDraftRecoveryCodec; atomicIO?: ParentDraftRecoveryAtomicIO
  /** Tests inject failure at actual I/O commit boundaries; production omits this. */
  fault?: (point: 'before-rename' | 'after-rename' | 'after-readback') => void
}
export class ParentDraftRecoveryStore {
  readonly profileId: string
  private readonly root: string
  constructor(private readonly options: ParentDraftRecoveryStoreOptions) {
    this.profileId = options.profileId
    this.root = path.join(options.userDataPath, 'parent-draft-recovery')
    safe(() => {
      need(typeof options.profileId === 'string' && options.profileId.length >= 1 && options.profileId.length <= 256, 'DRAFT_RECOVERY_INVALID_PROFILE')
      need(path.isAbsolute(options.userDataPath) && path.resolve(options.userDataPath) === options.userDataPath, 'DRAFT_RECOVERY_UNSAFE_PATH')
      this.verifyDirectory(options.userDataPath)
      if (!fs.existsSync(this.root)) fs.mkdirSync(this.root, { mode: 0o700 })
      this.verifyDirectory(this.root)
      this.inventory()
    })
  }
  private verifyDirectory(target: string): void {
    let current = path.parse(target).root
    for (const part of target.slice(current.length).split(path.sep).filter(Boolean)) {
      current = path.join(current, part)
      const stat = fs.lstatSync(current)
      need(stat.isDirectory() && !stat.isSymbolicLink(), 'DRAFT_RECOVERY_UNSAFE_PATH')
    }
    need(fs.realpathSync.native(target) === target, 'DRAFT_RECOVERY_UNSAFE_PATH')
  }
  private filename(childId: string, scope: Scope): string {
    identity(childId, scope)
    return path.join(this.root, createHash('sha256').update(canonical([childId, scope])).digest('hex') + '.pdr')
  }
  private inventory(): Inventory {
    this.verifyDirectory(this.root)
    const files = new Map<string, fs.Stats>()
    const orphans: string[] = []
    let total = 0
    for (const entry of fs.readdirSync(this.root)) {
      // Only the exact native temp-name grammar is recoverable; foreign files remain HOLD.
      const isOrphan = /^\.pending-[a-f0-9]{48}$/.test(entry)
      need(finalName.test(entry) || isOrphan, 'DRAFT_RECOVERY_UNEXPECTED_FILE')
      const stat = fs.lstatSync(path.join(this.root, entry))
      need(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1, 'DRAFT_RECOVERY_UNSAFE_FILE')
      need(stat.size > 0 && stat.size <= MAX_CIPHER, 'DRAFT_RECOVERY_TOO_LARGE')
      if (isOrphan) orphans.push(path.join(this.root, entry))
      else files.set(entry, stat)
      total += stat.size
    }
    need(files.size <= MAX_FILES && orphans.length <= MAX_FILES && total <= 2 * MAX_FILES * MAX_CIPHER, 'DRAFT_RECOVERY_CAPACITY')
    return { files, orphans }
  }
  private reservedScopes(inventory: Inventory): Set<string> {
    const scopes = new Set(inventory.files.keys())
    for (const filename of inventory.orphans) {
      const orphan = this.decode(this.readCipher(filename))
      scopes.add(path.basename(this.filename(orphan.childId, orphan.scope)))
    }
    need(scopes.size <= MAX_FILES, 'DRAFT_RECOVERY_CAPACITY')
    return scopes
  }
  private readCipher(filename: string): Buffer {
    const before = fs.lstatSync(filename)
    need(before.isFile() && !before.isSymbolicLink() && before.nlink === 1 && before.size > 0 && before.size <= MAX_CIPHER, 'DRAFT_RECOVERY_UNSAFE_FILE')
    const fd = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0))
    try {
      const held = fs.fstatSync(fd)
      need(held.isFile() && held.nlink === 1 && held.dev === before.dev && held.ino === before.ino && held.size === before.size, 'DRAFT_RECOVERY_UNSAFE_FILE')
      // Bound allocation and reads to the original size even if another process grows it.
      const buffer = Buffer.alloc(held.size + 1)
      let count = 0
      while (count < buffer.length) {
        const n = fs.readSync(fd, buffer, count, buffer.length - count, count)
        if (n === 0) break
        count += n
      }
      const bytes = buffer.subarray(0, count)
      const after = fs.fstatSync(fd), named = fs.lstatSync(filename)
      need(bytes.length === held.size && after.size === held.size && after.mtimeMs === held.mtimeMs && after.ctimeMs === held.ctimeMs && named.dev === held.dev && named.ino === held.ino && named.nlink === 1 && !named.isSymbolicLink(), 'DRAFT_RECOVERY_FILE_CHANGED')
      return bytes
    } finally { fs.closeSync(fd) }
  }
  private decode(bytes: Buffer, childId?: string, scope?: Scope): Stored & { childId: string; scope: Scope } {
    this.options.codec.assertAvailable()
    let plain: string, envelope: any
    try { plain = this.options.codec.decrypt(bytes); need(Buffer.byteLength(plain, 'utf8') <= MAX_CIPHER); envelope = JSON.parse(plain) }
    catch { throw new ParentDraftRecoveryError('DRAFT_RECOVERY_CORRUPT_OR_KEY_UNAVAILABLE') }
    identity(envelope?.childId, envelope?.scope)
    need(exactKeys(envelope, ['schema', 'profileId', 'childId', 'scope', 'localRevision', 'snapshot']) && envelope.schema === 1 && envelope.profileId === this.profileId && (childId === undefined || envelope.childId === childId) && (scope === undefined || envelope.scope === scope) && revision(envelope.localRevision) && envelope.localRevision > 0, 'DRAFT_RECOVERY_BINDING_MISMATCH')
    return { childId: envelope.childId, scope: envelope.scope, localRevision: envelope.localRevision, snapshot: validateSnapshot(envelope.snapshot, envelope.childId, envelope.scope).snapshot }
  }
  read(childId: string, scope: Scope): Stored {
    return safe(() => {
      this.options.codec.assertAvailable()
      const filename = this.filename(childId, scope), inventory = this.inventory()
      const decoded = inventory.files.has(path.basename(filename)) ? this.decode(this.readCipher(filename), childId, scope) : null
      const committed: Stored = { localRevision: decoded?.localRevision ?? 0, snapshot: decoded?.snapshot ?? null }
      const matches: Stored[] = []
      for (const orphanName of inventory.orphans) {
        const cipher = this.readCipher(orphanName), orphan = this.decode(cipher)
        if (orphan.childId === childId && orphan.scope === scope) matches.push({ localRevision: orphan.localRevision, snapshot: orphan.snapshot, orphan: { filename: orphanName, cipher } })
      }
      if (matches.length === 0) return committed
      need(matches.length === 1 && matches[0].localRevision === committed.localRevision + 1, 'DRAFT_RECOVERY_ORPHAN_CONFLICT')
      // No promotion here. Prepare reports the effective revision without releasing raw.
      return matches[0]
    })
  }
  recover(childId: string, scope: Scope, expectedLocalRevision: number): Stored {
    return safe(() => {
      const candidate = this.read(childId, scope)
      need(revision(expectedLocalRevision) && expectedLocalRevision === candidate.localRevision, 'DRAFT_RECOVERY_STALE_REVISION')
      if (!candidate.orphan) return candidate
      const filename = this.filename(childId, scope), orphan = candidate.orphan
      this.reservedScopes(this.inventory()) // Promotion cannot create a seventeenth canonical scope.
      need(this.readCipher(orphan.filename).equals(orphan.cipher), 'DRAFT_RECOVERY_FILE_CHANGED')
      this.options.fault?.('before-rename')
      ;(this.options.atomicIO ?? defaultAtomicIO).replaceAndFlush(orphan.filename, filename)
      this.options.fault?.('after-rename')
      need(this.readCipher(filename).equals(orphan.cipher), 'DRAFT_RECOVERY_READBACK_FAILED')
      const restored = this.decode(this.readCipher(filename), childId, scope)
      this.options.fault?.('after-readback')
      return { localRevision: restored.localRevision, snapshot: restored.snapshot }
    })
  }
  write(childId: string, scope: Scope, expectedLocalRevision: number, snapshot: Snapshot): Stored {
    return safe(() => {
      need(revision(expectedLocalRevision), 'DRAFT_RECOVERY_STALE_REVISION')
      const checked = validateSnapshot(snapshot, childId, scope)
      const prior = this.read(childId, scope) // Includes corrupt/key/profile and original-file checks before overwrite.
      need(!prior.orphan, 'DRAFT_RECOVERY_EXPLICIT_RECOVERY_REQUIRED')
      need(prior.localRevision === expectedLocalRevision && prior.localRevision < Number.MAX_SAFE_INTEGER, 'DRAFT_RECOVERY_STALE_REVISION')
      const filename = this.filename(childId, scope), inventory = this.inventory(), reserved = this.reservedScopes(inventory)
      need(inventory.orphans.length < MAX_FILES, 'DRAFT_RECOVERY_CAPACITY')
      need(reserved.has(path.basename(filename)) || reserved.size < MAX_FILES, 'DRAFT_RECOVERY_CAPACITY')
      const localRevision = prior.localRevision + 1
      const plain = canonical({ schema: 1, profileId: this.profileId, childId, scope, localRevision, snapshot: checked.snapshot })
      this.options.codec.assertAvailable()
      const cipher = this.options.codec.encrypt(plain)
      need(Buffer.isBuffer(cipher) && cipher.length > 0 && cipher.length <= MAX_CIPHER, 'DRAFT_RECOVERY_ENCRYPTION_UNAVAILABLE')
      const temp = path.join(this.root, `.pending-${randomBytes(24).toString('hex')}`)
      let fd: number | undefined, renamed = false
      try {
        fd = fs.openSync(temp, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600)
        fs.writeFileSync(fd, cipher); fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined
        need(this.readCipher(temp).equals(cipher), 'DRAFT_RECOVERY_READBACK_FAILED')
        this.decode(cipher, childId, scope)
        this.options.fault?.('before-rename')
        // Synchronous Main code serializes store calls. Recheck original path immediately before commit.
        if (prior.localRevision > 0) need(this.decode(this.readCipher(filename), childId, scope).localRevision === prior.localRevision, 'DRAFT_RECOVERY_STALE_REVISION')
        else need(!fs.existsSync(filename), 'DRAFT_RECOVERY_STALE_REVISION')
        // A trusted native adapter may implement WRITE_THROUGH on Windows. Default directory
        // fsync is strict; unsupported platforms fail closed rather than claim protection.
        ;(this.options.atomicIO ?? defaultAtomicIO).replaceAndFlush(temp, filename)
        renamed = true
        this.options.fault?.('after-rename')
        need(this.readCipher(filename).equals(cipher), 'DRAFT_RECOVERY_READBACK_FAILED')
        const committed = this.decode(this.readCipher(filename), childId, scope)
        this.options.fault?.('after-readback')
        return { localRevision: committed.localRevision, snapshot: committed.snapshot }
      } finally {
        if (fd !== undefined) fs.closeSync(fd)
        if (!renamed && fs.existsSync(temp)) fs.unlinkSync(temp)
      }
      // After rename, an ACK failure may leave the complete new capsule. Never report stored;
      // the old lease stays at the old revision and cannot overwrite that unacknowledged commit.
    })
  }
  eraseChild(childId: string): void {
    safe(() => { for (const scope of ['capture', 'language'] as const) this.clearScope(childId, scope) })
  }
  eraseAll(): void {
    safe(() => {
      const inventory = this.inventory(), owners = new Map<string, { childId: string; scope: Scope }>()
      for (const filename of [...Array.from(inventory.files.keys(), name => path.join(this.root, name)), ...inventory.orphans]) {
        const owner = this.decode(this.readCipher(filename))
        if (finalName.test(path.basename(filename))) need(this.filename(owner.childId, owner.scope) === filename, 'DRAFT_RECOVERY_BINDING_MISMATCH')
        owners.set(canonical([owner.childId, owner.scope]), owner)
      }
      for (const owner of owners.values()) this.clearScope(owner.childId, owner.scope)
    })
  }
  private clearScope(childId: string, scope: Scope): void {
    let prior = this.read(childId, scope)
    if (prior.orphan) prior = this.recover(childId, scope, prior.localRevision)
    if (prior.snapshot === null) return
    if (prior.snapshot.acknowledged === null && prior.snapshot.latest === null && prior.snapshot.pending === null) return
    // Explicit discard/privacy first promotes a sole valid orphan, then replaces every
    // original payload with empty cipher. Failed purge keeps Root's privacy barrier.
    this.write(childId, scope, prior.localRevision, { schema: 1, childId, scope, acknowledged: null, latest: null, pending: null })
  }
}

type Lease = { senderId: number; childId: string; scope: Scope; epoch: number; issue: number; localRevision: number; active: boolean; sequence: number; bytes: string | null }
export type ParentDraftRecoveryCoordinatorOptions = {
  store: Pick<ParentDraftRecoveryStore,'profileId'|'read'|'recover'|'write'|'eraseChild'|'eraseAll'>
  prepare?:()=>Promise<void>
  authorize: (senderId: number, childId: string, scope: Scope, parentToken: string) => Promise<{ profileId: string }>
  /** Root's durable privacy transaction guard; never a renderer flag. */
  isPrivacyBlocked?: (childId: string) => boolean
}
export class ParentDraftRecoveryCoordinator {
  private readonly epochs = new Map<number, number>()
  private readonly issues = new Map<string, number>()
  private readonly leases = new Map<string, Lease>()
  private accepting=true
  private readonly preparing=new Set<{key:string;epoch:number;issue:number}>()
  constructor(private readonly options: ParentDraftRecoveryCoordinatorOptions) {}
  private key(senderId: number, childId: string, scope: Scope): string { return canonical([senderId, childId, scope]) }
  private allowed(childId: string): void { need(!this.options.isPrivacyBlocked?.(childId), 'DRAFT_RECOVERY_PRIVACY_BLOCKED') }
  private begin(senderId: number, childId: string, scope: Scope, token: string) {
    need(this.accepting,'DRAFT_RECOVERY_REVOKED')
    identity(childId, scope); need(Number.isSafeInteger(senderId) && senderId >= 0 && typeof token === 'string' && token.length > 0, 'DRAFT_RECOVERY_UNAUTHORIZED')
    this.allowed(childId)
    const key = this.key(senderId, childId, scope), epoch = this.epochs.get(senderId) ?? 0, issue = (this.issues.get(key) ?? 0) + 1
    need(Number.isSafeInteger(issue), 'DRAFT_RECOVERY_UNAUTHORIZED')
    this.issues.set(key, issue)
    for (const [id, lease] of this.leases) if (lease.senderId === senderId && lease.childId === childId && lease.scope === scope) this.leases.delete(id)
    return { key, epoch, issue }
  }
  private current(senderId: number, childId: string, turn: { key: string; epoch: number; issue: number }): void {
    this.allowed(childId)
    need((this.epochs.get(senderId) ?? 0) === turn.epoch && this.issues.get(turn.key) === turn.issue, 'DRAFT_RECOVERY_REVOKED')
  }
  private async authorized(senderId:number,childId:string,scope:Scope,token:string,assertCurrent:()=>void=()=>{}) {
    const turn=this.begin(senderId,childId,scope,token);this.preparing.add(turn);let returning=false
    try {
      assertCurrent()
      let auth:{profileId:string}
      try{auth=await this.options.authorize(senderId,childId,scope,token)}catch{throw new ParentDraftRecoveryError('DRAFT_RECOVERY_UNAUTHORIZED')}
      assertCurrent();this.current(senderId,childId,turn)
      need(auth?.profileId===this.options.store.profileId,'DRAFT_RECOVERY_UNAUTHORIZED')
      if(this.options.prepare){
        await this.options.prepare();assertCurrent();this.current(senderId,childId,turn)
        try{auth=await this.options.authorize(senderId,childId,scope,token)}catch{throw new ParentDraftRecoveryError('DRAFT_RECOVERY_UNAUTHORIZED')}
        assertCurrent();this.current(senderId,childId,turn)
      }
      need(auth?.profileId===this.options.store.profileId,'DRAFT_RECOVERY_UNAUTHORIZED')
      returning=true;return turn
    }finally{if(!returning)this.preparing.delete(turn)}
  }
  quiesce():void{this.accepting=false;for(const turn of this.preparing)if(this.issues.get(turn.key)===turn.issue)this.issues.set(turn.key,turn.issue+1)}
  resume():void{this.accepting=true}
  private grant(senderId: number, childId: string, scope: Scope, turn: { epoch: number; issue: number }, stored: Stored, active: boolean): Prepared {
    const leaseId = randomBytes(32).toString('hex')
    this.leases.set(leaseId, { senderId, childId, scope, epoch: turn.epoch, issue: turn.issue, localRevision: stored.localRevision, active, sequence: 0, bytes: null })
    return { leaseId, localRevision: stored.localRevision, hasRecovery: !!stored.orphan || (stored.snapshot !== null && isDirty(stored.snapshot)), protection: 'native_encrypted' }
  }
  async prepare(senderId: number, childId: string, scope: Scope, token: string, assertCurrent?:()=>void): Promise<Prepared> {
    const turn = await this.authorized(senderId, childId, scope, token,assertCurrent)
    try {
    assertCurrent?.()
    // Recheck after the outer await as well: another microtask may have revoked this turn.
    this.current(senderId, childId, { ...turn, key: this.key(senderId, childId, scope) })
    const stored = this.options.store.read(childId, scope)
    return this.grant(senderId, childId, scope, turn, stored, !stored.orphan && (stored.snapshot === null || !isDirty(stored.snapshot)))
    }finally{this.preparing.delete(turn)}
  }
  async read(senderId: number, childId: string, scope: Scope, token: string, expectedLocalRevision: number, assertCurrent?:()=>void): Promise<Recovered> {
    const turn = await this.authorized(senderId, childId, scope, token,assertCurrent)
    try {
    assertCurrent?.()
    this.current(senderId, childId, turn)
    const stored = this.options.store.recover(childId, scope, expectedLocalRevision)
    need(revision(expectedLocalRevision) && stored.localRevision === expectedLocalRevision && stored.snapshot !== null, 'DRAFT_RECOVERY_STALE_REVISION')
    return { ...this.grant(senderId, childId, scope, turn, stored, true), snapshot: stored.snapshot }
    }finally{this.preparing.delete(turn)}
  }
  checkpoint(senderId: number, leaseId: string, sequence: number, snapshot: Snapshot): Checkpoint {
    return safe(() => {
      const lease = this.leases.get(leaseId)
      need(lease && lease.senderId === senderId && lease.active, 'DRAFT_RECOVERY_INVALID_LEASE')
      this.current(senderId, lease.childId, { key: this.key(senderId, lease.childId, lease.scope), epoch: lease.epoch, issue: lease.issue })
      need(revision(sequence) && sequence > 0, 'DRAFT_RECOVERY_STALE_SEQUENCE')
      const checked = validateSnapshot(snapshot, lease.childId, lease.scope)
      if (sequence === lease.sequence && checked.bytes === lease.bytes) {
        need(this.options.store.read(lease.childId, lease.scope).localRevision === lease.localRevision, 'DRAFT_RECOVERY_STALE_REVISION')
        return { stored: true, localRevision: lease.localRevision, sequence }
      }
      need(sequence > lease.sequence, 'DRAFT_RECOVERY_STALE_SEQUENCE')
      const stored = this.options.store.write(lease.childId, lease.scope, lease.localRevision, checked.snapshot)
      lease.sequence = sequence; lease.bytes = checked.bytes; lease.localRevision = stored.localRevision
      return { stored: true, localRevision: stored.localRevision, sequence }
    })
  }
  async discard(senderId: number, childId: string, scope: Scope, token: string, expectedLocalRevision: number, explicit: true, assertCurrent?:()=>void): Promise<Prepared> {
    need(explicit === true, 'DRAFT_RECOVERY_EXPLICIT_REQUIRED')
    const turn = await this.authorized(senderId, childId, scope, token,assertCurrent)
    try {
    assertCurrent?.()
    this.current(senderId, childId, turn)
    const prior = this.options.store.recover(childId, scope, expectedLocalRevision)
    const stored = this.options.store.write(childId, scope, prior.localRevision, { schema: 1, childId, scope, acknowledged: null, latest: null, pending: null })
    return this.grant(senderId, childId, scope, turn, stored, true)
    }finally{this.preparing.delete(turn)}
  }
  revokeSender(senderId: number): void {
    this.epochs.set(senderId, (this.epochs.get(senderId) ?? 0) + 1)
    for (const [id, lease] of this.leases) if (lease.senderId === senderId) this.leases.delete(id)
  }
  revokeChild(childId: string): void {
    identity(childId, 'capture')
    for (const [key, issue] of this.issues) if (JSON.parse(key)[1] === childId) this.issues.set(key, issue + 1)
    for (const [id, lease] of this.leases) if (lease.childId === childId) this.leases.delete(id)
  }
  revokeAll(): void {
    for (const [key, issue] of this.issues) this.issues.set(key, issue + 1)
    this.leases.clear()
  }
  /** Main privacy hooks only; deliberately absent from Desktop renderer port. */
  eraseChild(childId: string): void { this.revokeChild(childId); this.options.store.eraseChild(childId) }
  eraseAll(): void { this.revokeAll(); this.options.store.eraseAll() }
}
