import { afterEach, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'
import * as os from 'node:os'
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import type { ParentDraftRecoverySnapshot as Snapshot } from '../shared/parentDraftRecovery'
const native = await import(process.env.HSC_NATIVE_CONTROL_SOURCE ?? path.join(import.meta.dirname, 'parent-draft-recovery.ts'))
const dirs: string[] = []
afterEach(() => { for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true }) })
const payload = (text: string) => ({ page: { title: text }, capture_ref: '', capture_version: '', review_drafts: {}, action_ids: {} })
const value = (text: string, childId = 'child-a'): Snapshot => ({ schema: 1, childId, scope: 'capture', acknowledged: {
  child_id: childId, scope: 'capture', storage_generation: 'original-generation', revision: 4, payload: payload('server acknowledged'),
  last_action_id: 'old-action-1234', last_expected_revision: 3, updated_at: null, stale_refs: [], cloud_transmission_allowed: false
}, latest: payload(text), pending: { expected_revision: 4, expected_generation: 'original-generation', client_action_id: 'same-unresolved-action-1234', payload: payload('exact original request') } })
function fixture() {
  const dir = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'hsc-draft-crash-')); dirs.push(dir)
  const root = path.join(dir, 'parent-draft-recovery'), key = randomBytes(32)
  const codec = { assertAvailable() {}, encrypt(plain: string) { const iv = randomBytes(12), c = createCipheriv('aes-256-gcm', key, iv); const b = Buffer.concat([c.update(plain), c.final()]); return Buffer.concat([iv, c.getAuthTag(), b]) }, decrypt(b: Buffer) {
    const d = createDecipheriv('aes-256-gcm', key, b.subarray(0, 12), { authTagLength: 16 }); d.setAuthTag(b.subarray(12, 28)); return Buffer.concat([d.update(b.subarray(28)), d.final()]).toString('utf8')
  } }
  const options = { userDataPath: dir, profileId: 'trusted-profile', codec }
  const make = (extra = {}) => new native.ParentDraftRecoveryStore({ ...options, ...extra })
  const store = make(); store.write('child-a', 'capture', 0, value('earlier ACK'))
  const canonical = path.join(root, fs.readdirSync(root)[0]), oldBytes = fs.readFileSync(canonical)
  // Preserve the exact encrypted/fsynced temp produced by the actual Store before commit.
  // This materializes crash-equivalent disk state, not an actual forced OS process death.
  const orphan = path.join(root, `.pending-${'a'.repeat(48)}`)
  const interrupted = make({ fault(point: string) { if (point === 'before-rename') {
    const actual = fs.readdirSync(root).find(name => name.startsWith('.pending-'))!
    fs.copyFileSync(path.join(root, actual), orphan, fs.constants.COPYFILE_EXCL)
    const fd = fs.openSync(orphan, 'r+'); fs.fsyncSync(fd); fs.closeSync(fd)
    throw Error('synthetic crash-equivalent interruption')
  } } })
  expect(() => interrupted.write('child-a', 'capture', 1, value('latest unacknowledged 🧠'))).toThrow('DRAFT_RECOVERY_IO')
  expect(fs.readFileSync(canonical)).toEqual(oldBytes)
  const coordinator = (fresh: any, blocked = () => false) => new native.ParentDraftRecoveryCoordinator({ store: fresh, authorize: async (_s: number, _c: string, _scope: string, token: string) => {
    if (token !== 'fresh-parent') throw Error('unauthorized'); return { profileId: 'trusted-profile' }
  }, isPrivacyBlocked: blocked })
  return { root, orphan, canonical, oldBytes, make, coordinator, codec }
}
it('original ACK plus actual Store orphan survives reconstruction, explicit fresh auth promotes and editing continues', async () => {
  const f = fixture(), rebuilt = f.make(), c = f.coordinator(rebuilt)
  const p = await c.prepare(7, 'child-a', 'capture', 'fresh-parent')
  expect(p.hasRecovery).toBe(true); expect(p.localRevision).toBe(2); expect(p).not.toHaveProperty('snapshot')
  expect(fs.readFileSync(f.canonical)).toEqual(f.oldBytes); expect(fs.existsSync(f.orphan)).toBe(true)
  expect(() => c.checkpoint(7, p.leaseId, 1, value('forbidden fresh overwrite'))).toThrow('INVALID_LEASE')
  await expect(c.read(7, 'child-a', 'capture', 'wrong-parent', 2)).rejects.toThrow('UNAUTHORIZED')
  await expect(c.read(7, 'child-a', 'capture', 'fresh-parent', 1)).rejects.toThrow('STALE_REVISION')
  expect(fs.readFileSync(f.canonical)).toEqual(f.oldBytes)
  const recovered = await c.read(7, 'child-a', 'capture', 'fresh-parent', 2)
  expect(recovered.snapshot).toEqual(value('latest unacknowledged 🧠')); expect(fs.existsSync(f.orphan)).toBe(false)
  expect(c.checkpoint(7, recovered.leaseId, 1, value('continue editing')).localRevision).toBe(3)
  expect(f.make().read('child-a', 'capture').snapshot).toEqual(value('continue editing'))
})
it('explicit discard and Main privacy erase remove the matching orphan by durable empty replacement', async () => {
  const f = fixture(), c = f.coordinator(f.make())
  await expect(c.discard(7, 'child-a', 'capture', 'fresh-parent', 1, true)).rejects.toThrow('STALE_REVISION')
  const discarded = await c.discard(7, 'child-a', 'capture', 'fresh-parent', 2, true)
  expect(discarded.localRevision).toBe(3); expect(discarded.hasRecovery).toBe(false); expect(fs.existsSync(f.orphan)).toBe(false)
  expect(f.make().read('child-a', 'capture').snapshot).toEqual({ schema: 1, childId: 'child-a', scope: 'capture', acknowledged: null, latest: null, pending: null })
  const g = fixture(), blocked = true, privacy = g.coordinator(g.make(), () => blocked)
  privacy.eraseChild('child-a'); expect(fs.existsSync(g.orphan)).toBe(false); expect(g.make().read('child-a', 'capture').snapshot?.latest).toBeNull()
  const h = fixture(); h.coordinator(h.make(), () => true).eraseAll(); expect(fs.existsSync(h.orphan)).toBe(false); expect(h.make().read('child-a', 'capture').snapshot?.pending).toBeNull()
})
it('ambiguous or wrong-revision orphans remain untouched and cannot be silently erased or overwrite canonical', async () => {
  const f = fixture(), second = path.join(f.root, `.pending-${'b'.repeat(48)}`)
  fs.copyFileSync(f.orphan, second)
  const c = f.coordinator(f.make())
  await expect(c.prepare(7, 'child-a', 'capture', 'fresh-parent')).rejects.toThrow('ORPHAN_CONFLICT')
  expect(() => c.eraseChild('child-a')).toThrow('ORPHAN_CONFLICT'); expect(() => c.eraseAll()).toThrow('ORPHAN_CONFLICT')
  expect(fs.readFileSync(f.canonical)).toEqual(f.oldBytes); expect(fs.existsSync(f.orphan) && fs.existsSync(second)).toBe(true)
  fs.unlinkSync(second)
  const envelope = JSON.parse(f.codec.decrypt(fs.readFileSync(f.orphan))); envelope.localRevision = 4
  fs.writeFileSync(f.orphan, f.codec.encrypt(JSON.stringify(envelope)))
  await expect(c.prepare(7, 'child-a', 'capture', 'fresh-parent')).rejects.toThrow('ORPHAN_CONFLICT')
})
it('orphan alias/profile/bounds failures retain bytes and grant no read or purge success', async () => {
  const f = fixture(), link = path.join(path.dirname(f.root), 'linked-orphan')
  fs.linkSync(f.orphan, link); expect(() => f.make()).toThrow('UNSAFE_FILE'); fs.unlinkSync(link)
  const envelope = JSON.parse(f.codec.decrypt(fs.readFileSync(f.orphan))); envelope.profileId = 'different-profile'
  fs.writeFileSync(f.orphan, f.codec.encrypt(JSON.stringify(envelope)))
  const original = fs.readFileSync(f.orphan), c = f.coordinator(f.make())
  await expect(c.prepare(7, 'child-a', 'capture', 'fresh-parent')).rejects.toThrow('BINDING_MISMATCH')
  expect(() => c.eraseAll()).toThrow('BINDING_MISMATCH'); expect(fs.readFileSync(f.orphan)).toEqual(original)
})
it('lost promotion ACK leaves complete canonical revision and fresh explicit retry resumes original action', async () => {
  const f = fixture(), c = f.coordinator(f.make({ fault(point: string) { if (point === 'after-rename') throw Error('lost promote ACK') } }))
  await expect(c.read(7, 'child-a', 'capture', 'fresh-parent', 2)).rejects.toThrow('DRAFT_RECOVERY_IO')
  expect(fs.existsSync(f.orphan)).toBe(false)
  const fresh = f.coordinator(f.make()), recovered = await fresh.read(7, 'child-a', 'capture', 'fresh-parent', 2)
  expect(recovered.snapshot.pending).toEqual(value('unused').pending); expect(recovered.snapshot.latest).toEqual(payload('latest unacknowledged 🧠'))
  expect(fresh.checkpoint(7, recovered.leaseId, 1, value('newest after failed ACK')).localRevision).toBe(3)
})
it('failed privacy clear after promotion stays blocked, retains original complete data and can retry idempotently', async () => {
  const f = fixture(); let operations = 0
  const store = f.make({ fault(point: string) { if (point === 'before-rename' && ++operations === 2) throw Error('privacy empty commit failed') } }), c = f.coordinator(store, () => true)
  expect(() => c.eraseChild('child-a')).toThrow('DRAFT_RECOVERY_IO')
  await expect(c.prepare(7, 'child-a', 'capture', 'fresh-parent')).rejects.toThrow('PRIVACY_BLOCKED')
  expect(f.make().read('child-a', 'capture').snapshot).toEqual(value('latest unacknowledged 🧠'))
  c.eraseChild('child-a'); expect(f.make().read('child-a', 'capture').snapshot?.latest).toBeNull()
})
