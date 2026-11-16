import { afterEach, describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { createSafeStorageDraftCodec, ParentDraftRecoveryCoordinator as Coordinator, ParentDraftRecoveryStore as Store, type ParentDraftRecoveryCodec, type ParentDraftRecoveryStoreOptions } from './parent-draft-recovery'
import { canonical, validateSnapshot } from './parent-draft-recovery-validation'
import type { ParentDraftRecoverySnapshot as Snapshot, ParentDraftRecoveryScope as Scope } from '../shared/parentDraftRecovery'
const dirs: string[] = []
afterEach(() => { for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true }) })
function directory() { const dir = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'hsc-draft-recovery-')); dirs.push(dir); return dir }
/** Synthetic authenticated encryption, not a substitute for actual Electron safeStorage. */
function aes(key = randomBytes(32)): ParentDraftRecoveryCodec {
  return { assertAvailable() {}, encrypt(plain) {
    const iv = randomBytes(12), cipher = createCipheriv('aes-256-gcm', key, iv)
    const bytes = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final()])
    return Buffer.concat([iv, cipher.getAuthTag(), bytes])
  }, decrypt(cipher) {
    const decipher = createDecipheriv('aes-256-gcm', key, cipher.subarray(0, 12), { authTagLength: 16 }); decipher.setAuthTag(cipher.subarray(12, 28))
    return Buffer.concat([decipher.update(cipher.subarray(28)), decipher.final()]).toString('utf8')
  } }
}
function editing(text = 'private synthetic marker 🧠', scope: Scope = 'capture') {
  return { page: { title: text }, capture_ref: '', capture_version: '', review_drafts: {}, action_ids: {}, ...(scope === 'capture' ? { objective_draft: {
    question_number: '6', full_question: '4+5', official_answers: ['9'], candidate_node_id: null,
    paper: { capture_ref: 'synthetic-paper', source_version: 'paper-v1' }, answer_key: { capture_ref: 'synthetic-answer', source_version: 'answer-v1' },
    raw_editing: { full_question: '4+5', official_answers_text: '9' }
  } } : {}) }
}
function snapshot(childId = 'child-a', scope: Scope = 'capture', pending = false): Snapshot {
  const original = editing('original acknowledged', scope)
  return { schema: 1, childId, scope, acknowledged: { child_id: childId, scope, storage_generation: 'generation-original', revision: 4,
    payload: original, last_action_id: 'old-action-1234', last_expected_revision: 3, updated_at: '2026-10-17T00:00:00Z', stale_refs: [], cloud_transmission_allowed: false },
    latest: editing('newest private synthetic marker 🧠', scope), pending: pending ? { expected_revision: 4, expected_generation: 'generation-original', client_action_id: 'exact-unresolved-action-1234', payload: editing('original pending bytes', scope) } : null }
}
function fixture(options: Partial<ParentDraftRecoveryStoreOptions> = {}) {
  const userDataPath = options.userDataPath ?? directory(), codec = options.codec ?? aes(), profileId = options.profileId ?? 'trusted-profile-a'
  const store = new Store({ userDataPath, codec, profileId, ...options })
  let authorized = 0, blocked = false
  const coordinator = new Coordinator({ store, authorize: async (_sender, _child, _scope, token) => { authorized++; if (token !== 'fresh-parent-auth') throw Error('private token must never escape'); return { profileId } }, isPrivacyBlocked: () => blocked })
  return { userDataPath, codec, profileId, store, coordinator, root: path.join(userDataPath, 'parent-draft-recovery'), authCount: () => authorized, setBlocked: (value: boolean) => { blocked = value } }
}
const prepare = (c: Coordinator, child = 'child-a', scope: Scope = 'capture', sender = 7) => c.prepare(sender, child, scope, 'fresh-parent-auth')
const files = (root: string) => fs.readdirSync(root).map(name => path.join(root, name))

 describe('native encrypted draft recovery — real files, synthetic codec', () => {
  it('reconstructs latest and exact unresolved action after new store/coordinator; disk contains no plaintext or token', async () => {
    const f = fixture(), p = await prepare(f.coordinator), value = snapshot('child-a', 'capture', true)
    expect(f.coordinator.checkpoint(7, p.leaseId, 1, value)).toEqual({ stored: true, localRevision: 1, sequence: 1 })
    const disk = fs.readFileSync(files(f.root)[0])
    for (const raw of ['private synthetic marker', 'exact-unresolved-action-1234', 'fresh-parent-auth', 'generation-original']) expect(disk.includes(Buffer.from(raw))).toBe(false)
    expect(fs.statSync(files(f.root)[0]).mode & 0o777).toBe(0o600)
    const next = fixture({ userDataPath: f.userDataPath, codec: f.codec }), prepared = await prepare(next.coordinator)
    expect(prepared.hasRecovery).toBe(true)
    expect(() => next.coordinator.checkpoint(7, prepared.leaseId, 1, value)).toThrow('INVALID_LEASE')
    const recovered = await next.coordinator.read(7, 'child-a', 'capture', 'fresh-parent-auth', prepared.localRevision)
    expect(recovered.snapshot).toEqual(value); expect(next.authCount()).toBe(2)
    expect(recovered.snapshot.pending).toEqual(value.pending)
    expect(recovered.snapshot.latest).not.toEqual(recovered.snapshot.pending?.payload)
  })
  it('clean acknowledged payload does not create false unsaved recovery', async () => {
    const f = fixture(), p = await prepare(f.coordinator), value = snapshot(); value.latest = value.acknowledged!.payload
    f.coordinator.checkpoint(7, p.leaseId, 1, value)
    const next = await prepare(f.coordinator); expect(next.hasRecovery).toBe(false)
    expect(f.coordinator.checkpoint(7, next.leaseId, 1, value).localRevision).toBe(2)
  })
  it('precommit interruption leaves original capsule exact and removes only its owned temp', () => {
    const f = fixture(); f.store.write('child-a', 'capture', 0, snapshot())
    const original = fs.readFileSync(files(f.root)[0])
    const failing = new Store({ userDataPath: f.userDataPath, codec: f.codec, profileId: f.profileId, fault(point) { if (point === 'before-rename') throw Error('synthetic interruption') } })
    expect(() => failing.write('child-a', 'capture', 1, snapshot('child-a', 'capture', true))).toThrow('DRAFT_RECOVERY_IO')
    expect(files(f.root)).toHaveLength(1); expect(fs.readFileSync(files(f.root)[0])).toEqual(original)
    expect(f.store.read('child-a', 'capture').localRevision).toBe(1)
  })
  it('post-rename lost ACK leaves complete new capsule; stale lease cannot overwrite; explicit read recovers', async () => {
    let fail = false
    const f = fixture({ fault(point) { if (fail && point === 'after-rename') throw Error('synthetic lost ACK') } }), p = await prepare(f.coordinator)
    f.coordinator.checkpoint(7, p.leaseId, 1, snapshot()); fail = true
    const pending = snapshot('child-a', 'capture', true)
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 2, pending)).toThrow('DRAFT_RECOVERY_IO')
    fail = false
    expect(f.store.read('child-a', 'capture')).toEqual({ localRevision: 2, snapshot: pending })
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 3, snapshot())).toThrow('STALE_REVISION')
    const recovered = await f.coordinator.read(7, 'child-a', 'capture', 'fresh-parent-auth', 2)
    expect(recovered.snapshot).toEqual(pending)
  })
  it('unavailable platform commit adapter fails closed, and no stored ACK is fabricated', async () => {
    const f = fixture({ atomicIO: { replaceAndFlush() { throw Error('unsupported platform flush') } } }), p = await prepare(f.coordinator)
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 1, snapshot())).toThrow('DRAFT_RECOVERY_IO')
    expect(files(f.root)).toHaveLength(0)
  })
  it('encrypted corruption and wrong key retain original bytes and prevent blank overwrite', () => {
    const f = fixture(); f.store.write('child-a', 'capture', 0, snapshot())
    const filename = files(f.root)[0], original = fs.readFileSync(filename)
    const wrong = fixture({ userDataPath: f.userDataPath, codec: aes() })
    expect(() => wrong.store.read('child-a', 'capture')).toThrow('CORRUPT_OR_KEY_UNAVAILABLE')
    expect(() => wrong.store.write('child-a', 'capture', 0, snapshot())).toThrow('CORRUPT_OR_KEY_UNAVAILABLE')
    expect(fs.readFileSync(filename)).toEqual(original)
    fs.writeFileSync(filename, Buffer.from('corrupt synthetic bytes'))
    const corrupt = fs.readFileSync(filename)
    expect(() => f.store.write('child-a', 'capture', 1, snapshot())).toThrow('CORRUPT_OR_KEY_UNAVAILABLE')
    expect(fs.readFileSync(filename)).toEqual(corrupt)
  })
  it('copying a valid cipher into a different trusted profile cannot expose or overwrite it', () => {
    const f = fixture(); f.store.write('child-a', 'capture', 0, snapshot())
    const other = fixture({ codec: f.codec, profileId: 'trusted-profile-b' }), filename = path.join(other.root, path.basename(files(f.root)[0]))
    fs.copyFileSync(files(f.root)[0], filename)
    expect(() => other.store.read('child-a', 'capture')).toThrow('BINDING_MISMATCH')
    expect(() => other.store.write('child-a', 'capture', 0, snapshot())).toThrow('BINDING_MISMATCH')
  })
  it('rejects hardlinks, symlinks, aliased directories and copied child/scope binding', () => {
    const f = fixture(); f.store.write('child-a', 'capture', 0, snapshot())
    const filename = files(f.root)[0], outside = path.join(f.userDataPath, 'outside-link')
    fs.linkSync(filename, outside); expect(() => f.store.read('child-a', 'capture')).toThrow('UNSAFE_FILE'); fs.unlinkSync(outside)
    fs.renameSync(filename, outside); fs.symlinkSync(outside, filename); expect(() => f.store.read('child-a', 'capture')).toThrow('UNSAFE_FILE'); fs.unlinkSync(filename); fs.renameSync(outside, filename)
    const childB = fixture({ codec: f.codec }); childB.store.write('child-b', 'capture', 0, snapshot('child-b'))
    fs.copyFileSync(filename, files(childB.root)[0]); expect(() => childB.store.read('child-b', 'capture')).toThrow('BINDING_MISMATCH')
    const user = directory(), link = path.join(user, 'alias'); fs.symlinkSync(f.userDataPath, link)
    expect(() => new Store({ userDataPath: link, codec: f.codec, profileId: f.profileId })).toThrow('UNSAFE_PATH')
  })
  it('retains crash-orphan ciphertext rather than silently evicting private work', () => {
    const f = fixture(); const orphan = path.join(f.root, '.pending-synthetic-orphan')
    fs.writeFileSync(orphan, f.codec.encrypt('synthetic private uncommitted bytes'))
    expect(() => f.store.read('child-a', 'capture')).toThrow('UNEXPECTED_FILE'); expect(fs.existsSync(orphan)).toBe(true)
  })
  it('enforces bounded scopes without evicting any of sixteen existing capsules', () => {
    const f = fixture()
    for (let i = 0; i < 16; i++) f.store.write(`synthetic-child-${i}`, 'capture', 0, snapshot(`synthetic-child-${i}`))
    expect(() => f.store.write('seventeenth', 'capture', 0, snapshot('seventeenth'))).toThrow('CAPACITY')
    expect(files(f.root)).toHaveLength(16); expect(f.store.read('synthetic-child-0', 'capture').snapshot?.latest).toEqual(editing('newest private synthetic marker 🧠'))
  })
  it('bounded finite payload validation preserves emoji/raw and rejects mismatched normalization, extra secret fields and oversized payload', () => {
    const value = snapshot(); value.latest!.objective_draft!.raw_editing = { full_question: '🧠'.repeat(1201), official_answers_text: '9' }; value.latest!.objective_draft!.full_question = ''
    expect(validateSnapshot(value, 'child-a', 'capture').snapshot).toEqual(value)
    const bad = structuredClone(value); bad.latest!.objective_draft!.full_question = 'invented normalized value'; expect(() => validateSnapshot(bad, 'child-a', 'capture')).toThrow('INVALID_SNAPSHOT')
    for (const altered of [{ ...snapshot(), parentToken: 'not allowed' }, { ...snapshot(), latest: { ...editing(), image_bytes: 'not allowed' } }]) expect(() => validateSnapshot(altered, 'child-a', 'capture')).toThrow('INVALID_SNAPSHOT')
    const huge = snapshot(); huge.latest!.review_drafts = Object.fromEntries(Array.from({ length: 10 }, (_, i) => [`ref-${i}`, { text: 'x'.repeat(150000), source_version: 'version' }]))
    expect(() => validateSnapshot(huge, 'child-a', 'capture')).toThrow('INVALID_SNAPSHOT')
    const wrongPending = snapshot('child-a', 'capture', true); wrongPending.pending!.expected_generation = 'generation-substituted'; expect(() => validateSnapshot(wrongPending, 'child-a', 'capture')).toThrow('INVALID_SNAPSHOT')
    expect(() => validateSnapshot({ ...snapshot(), latest: { ...editing(), page: { title: NaN } } }, 'child-a', 'capture')).toThrow('INVALID_SNAPSHOT')
    let getterCalls = 0; const getter = Object.defineProperty({}, 'schema', { enumerable: true, get() { getterCalls++; return 1 } })
    expect(() => canonical(getter)).toThrow('INVALID_SNAPSHOT'); expect(getterCalls).toBe(0)
  })
  it('rejects oversized encrypted files without truncation and stale read revisions without private replies', async () => {
    const f = fixture(), p = await prepare(f.coordinator)
    f.coordinator.checkpoint(7, p.leaseId, 1, snapshot())
    await expect(f.coordinator.read(7, 'child-a', 'capture', 'fresh-parent-auth', 0)).rejects.toThrow('STALE_REVISION')
    const filename = files(f.root)[0], originalSize = 4 * 1024 * 1024 + 65537
    fs.truncateSync(filename, originalSize)
    expect(() => f.store.read('child-a', 'capture')).toThrow('TOO_LARGE')
    expect(() => f.store.write('child-a', 'capture', 1, snapshot())).toThrow('TOO_LARGE')
    expect(fs.statSync(filename).size).toBe(originalSize)
  })
  it('readback ACK failure and corrupt-cipher adapter cannot be reported as protected', async () => {
    let fail = false
    const f = fixture({ fault(point) { if (fail && point === 'after-readback') throw Error('synthetic final ACK loss') } }), p = await prepare(f.coordinator)
    f.coordinator.checkpoint(7, p.leaseId, 1, snapshot()); fail = true
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 2, snapshot('child-a', 'capture', true))).toThrow('DRAFT_RECOVERY_IO')
    expect(f.store.read('child-a', 'capture').localRevision).toBe(2)
    const g = fixture({ atomicIO: { replaceAndFlush(temp, final) { fs.renameSync(temp, final); fs.writeFileSync(final, 'synthetic corrupt commit') } } }), q = await prepare(g.coordinator)
    expect(() => g.coordinator.checkpoint(7, q.leaseId, 1, snapshot())).toThrow('READBACK_FAILED')
    expect(() => g.store.read('child-a', 'capture')).toThrow('CORRUPT_OR_KEY_UNAVAILABLE')
  })
  it('one sender-wide revoke covers capture and language while other sender remains fenced by CAS', async () => {
    const f = fixture(), capture = await prepare(f.coordinator), language = await prepare(f.coordinator, 'child-a', 'language')
    f.coordinator.revokeSender(7)
    expect(() => f.coordinator.checkpoint(7, capture.leaseId, 1, snapshot())).toThrow('INVALID_LEASE')
    expect(() => f.coordinator.checkpoint(7, language.leaseId, 1, snapshot('child-a', 'language'))).toThrow('INVALID_LEASE')
    expect(files(f.root)).toHaveLength(0)
  })
  it('safeStorage fails closed for unavailable/basic_text and delegates only real codec methods', () => {
    let encryptCalls = 0
    for (const backend of ['unavailable', 'basic_text']) {
      const codec = createSafeStorageDraftCodec({ isEncryptionAvailable: () => backend !== 'unavailable', getSelectedStorageBackend: () => backend, encryptString() { encryptCalls++; return Buffer.from('wrong') }, decryptString() { return 'wrong' } })
      expect(() => codec.encrypt('private')).toThrow('ENCRYPTION_UNAVAILABLE'); expect(() => codec.decrypt(Buffer.from('private'))).toThrow('ENCRYPTION_UNAVAILABLE')
    }
    expect(encryptCalls).toBe(0)
    const calls: unknown[] = [], codec = createSafeStorageDraftCodec({ isEncryptionAvailable: () => true, encryptString(s) { calls.push(s); return Buffer.from('synthetic native result') }, decryptString(b) { calls.push(b); return 'synthetic native plaintext' } })
    const bytes = codec.encrypt('private supplied'); expect(codec.decrypt(bytes)).toBe('synthetic native plaintext'); expect(calls).toEqual(['private supplied', bytes])
  })
 })

 describe('native recovery authority and privacy fences', () => {
  it('revocation invalidates pending authorization even when old parent auth succeeds later', async () => {
    const f = fixture(); let finish!: (auth: { profileId: string }) => void
    const coordinator = new Coordinator({ store: f.store, authorize: () => new Promise(resolve => { finish = resolve }) })
    const pending = prepare(coordinator); coordinator.revokeSender(7); finish({ profileId: f.profileId })
    await expect(pending).rejects.toThrow('REVOKED'); expect(files(f.root)).toHaveLength(0)
  })
  it('new prepare supersedes old pending prepare and original scope grant; other child remains independent', async () => {
    const f = fixture(), finish: ((value: { profileId: string }) => void)[] = []
    const c = new Coordinator({ store: f.store, authorize: () => new Promise(resolve => { finish.push(resolve) }) })
    const old = prepare(c), current = prepare(c); finish[1]({ profileId: f.profileId }); const granted = await current
    finish[0]({ profileId: f.profileId }); await expect(old).rejects.toThrow('REVOKED')
    const other = prepare(c, 'child-b'); finish[2]({ profileId: f.profileId }); const b = await other
    expect(c.checkpoint(7, granted.leaseId, 1, snapshot()).stored).toBe(true)
    expect(c.checkpoint(7, b.leaseId, 1, snapshot('child-b')).stored).toBe(true)
  })
  it('fresh authorization required for read/discard; wrong profile and bad parent tokens reveal no raw', async () => {
    const f = fixture(), p = await prepare(f.coordinator); f.coordinator.checkpoint(7, p.leaseId, 1, snapshot())
    await expect(f.coordinator.read(7, 'child-a', 'capture', 'bad-token', 1)).rejects.toThrow('UNAUTHORIZED')
    await expect(f.coordinator.discard(7, 'child-a', 'capture', 'bad-token', 1, true)).rejects.toThrow('UNAUTHORIZED')
    const wrong = new Coordinator({ store: f.store, authorize: async () => ({ profileId: 'renderer-forged-profile' }) })
    await expect(prepare(wrong)).rejects.toThrow('UNAUTHORIZED')
    expect(f.store.read('child-a', 'capture').localRevision).toBe(1)
  })
  it('revoke during the outer await gap cannot grant a lease', async () => {
    const f = fixture(); let done!: (x: { profileId: string }) => void
    const c = new Coordinator({ store: f.store, authorize: () => new Promise(r => { done = r }) })
    const operation = prepare(c); done({ profileId: f.profileId }); queueMicrotask(() => c.revokeSender(7))
    await expect(operation).rejects.toThrow('REVOKED')
  })
  it('rejects stale and changed duplicate sequences, wrong sender/child/scope, and old writer after new read', async () => {
    const f = fixture(), p = await prepare(f.coordinator), value = snapshot()
    const ack = f.coordinator.checkpoint(7, p.leaseId, 2, value)
    expect(f.coordinator.checkpoint(7, p.leaseId, 2, structuredClone(value))).toEqual(ack)
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 2, snapshot('child-a', 'capture', true))).toThrow('STALE_SEQUENCE')
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 1, value)).toThrow('STALE_SEQUENCE')
    expect(() => f.coordinator.checkpoint(8, p.leaseId, 3, value)).toThrow('INVALID_LEASE')
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 3, snapshot('child-b'))).toThrow('INVALID_SNAPSHOT')
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 3, snapshot('child-a', 'language'))).toThrow('INVALID_SNAPSHOT')
    await f.coordinator.read(7, 'child-a', 'capture', 'fresh-parent-auth', 1)
    expect(() => f.coordinator.checkpoint(7, p.leaseId, 3, value)).toThrow('INVALID_LEASE')
  })
  it('independent senders cannot overwrite a capsule using stale local revision', async () => {
    const f = fixture(), a = await prepare(f.coordinator), b = await prepare(f.coordinator, 'child-a', 'capture', 8)
    f.coordinator.checkpoint(7, a.leaseId, 1, snapshot())
    expect(() => f.coordinator.checkpoint(8, b.leaseId, 1, snapshot('child-a', 'capture', true))).toThrow('STALE_REVISION')
    expect(f.store.read('child-a', 'capture').snapshot).toEqual(snapshot())
  })
  it('discard requires exact revision and explicit fresh auth, commits clean monotonic tombstone', async () => {
    const f = fixture(), p = await prepare(f.coordinator); f.coordinator.checkpoint(7, p.leaseId, 1, snapshot())
    await expect(f.coordinator.discard(7, 'child-a', 'capture', 'fresh-parent-auth', 0, true)).rejects.toThrow('STALE_REVISION')
    await expect(f.coordinator.discard(7, 'child-a', 'capture', 'fresh-parent-auth', 1, false as true)).rejects.toThrow('EXPLICIT_REQUIRED')
    const next = await f.coordinator.discard(7, 'child-a', 'capture', 'fresh-parent-auth', 1, true)
    expect(next.localRevision).toBe(2); expect(next.hasRecovery).toBe(false)
    expect(f.store.read('child-a', 'capture').snapshot).toEqual({ schema: 1, childId: 'child-a', scope: 'capture', acknowledged: null, latest: null, pending: null })
  })
  it('privacy barrier rejects current writes and late auth, erases only chosen child via durable empty capsules', async () => {
    const f = fixture(), a = await prepare(f.coordinator), b = await prepare(f.coordinator, 'child-b')
    f.coordinator.checkpoint(7, a.leaseId, 1, snapshot()); f.coordinator.checkpoint(7, b.leaseId, 1, snapshot('child-b'))
    f.setBlocked(true); expect(() => f.coordinator.checkpoint(7, a.leaseId, 2, snapshot())).toThrow('PRIVACY_BLOCKED'); await expect(prepare(f.coordinator)).rejects.toThrow('PRIVACY_BLOCKED')
    f.coordinator.eraseChild('child-a'); expect(f.store.read('child-a', 'capture').snapshot?.latest).toBeNull(); expect(f.store.read('child-b', 'capture').snapshot?.latest).not.toBeNull()
    f.setBlocked(false); expect(() => f.coordinator.checkpoint(7, a.leaseId, 2, snapshot())).toThrow('INVALID_LEASE')
    expect(f.coordinator.checkpoint(7, b.leaseId, 2, snapshot('child-b')).stored).toBe(true)
    f.coordinator.eraseAll(); expect(f.store.read('child-b', 'capture').snapshot?.latest).toBeNull()
    const reconstructed = fixture({ userDataPath: f.userDataPath, codec: f.codec }); expect((await prepare(reconstructed.coordinator)).hasRecovery).toBe(false)
  })
  it('erase failure leaves privacy barrier and prior capsule; pending child/all grants stay revoked', async () => {
    let fail = false
    const f = fixture({ fault(point) { if (fail && point === 'before-rename') throw Error('synthetic disk failure') } }), p = await prepare(f.coordinator)
    f.coordinator.checkpoint(7, p.leaseId, 1, snapshot()); const prior = fs.readFileSync(files(f.root)[0]); f.setBlocked(true); fail = true
    expect(() => f.coordinator.eraseChild('child-a')).toThrow('DRAFT_RECOVERY_IO'); expect(fs.readFileSync(files(f.root)[0])).toEqual(prior)
    f.setBlocked(false); expect(() => f.coordinator.checkpoint(7, p.leaseId, 2, snapshot())).toThrow('INVALID_LEASE')
    let finish!: (x: { profileId: string }) => void; const c = new Coordinator({ store: f.store, authorize: () => new Promise(r => { finish = r }) })
    const pending = prepare(c, 'child-b'); c.revokeChild('child-b'); finish({ profileId: f.profileId }); await expect(pending).rejects.toThrow('REVOKED')
    const all = prepare(c, 'child-b'); c.revokeAll(); finish({ profileId: f.profileId }); await expect(all).rejects.toThrow('REVOKED')
  })
 })
