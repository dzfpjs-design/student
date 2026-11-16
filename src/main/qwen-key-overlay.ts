import { safeStorage } from 'electron'
import { createHash, randomBytes } from 'node:crypto'
import { closeSync, fstatSync, fsyncSync, lstatSync, openSync, readdirSync, readSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export const QWEN_OVERLAY_FILES = ['model-settings.json', 'asr-settings.json', 'qwen-realtime-settings.json', 'voice-settings.json', 'pet-audio-settings.bin'] as const
export type QwenOverlayFile = typeof QWEN_OVERLAY_FILES[number]
type OverlayEntry = { name: QwenOverlayFile; sourceHash: string; after: string | null }
export type QwenOverlay = { version: 1; generation: string; order: number; payloadHash: string; entries: OverlayEntry[] }
export type QwenOverlayPointer = { version: 1; generation: string; order: number; payloadHash: string }
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const HEX = /^[a-f0-9]{64}$/
const own = (value: unknown, names: string[]): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join('|') === [...names].sort().join('|')
const digest = (bytes: Buffer): string => createHash('sha256').update(bytes).digest('hex')
const absent = (value: string): boolean => value === 'absent' || value === 'absent-implicit'
export class QwenOverlayError extends Error { constructor() { super('QWEN_ROTATION_SOURCE_CHANGED') } }
const blocked = (): never => { throw new QwenOverlayError() }

function readBounded(path: string, limit: number): Buffer | null {
  let observed = false
  try {
    const initial = lstatSync(path); observed = true
    if (!initial.isFile() || initial.isSymbolicLink() || initial.nlink !== 1 || initial.size > limit) return blocked()
    const fd = openSync(path, 'r')
    try {
      const held = fstatSync(fd)
      if (!held.isFile() || held.nlink !== 1 || held.dev !== initial.dev || held.ino !== initial.ino || held.size !== initial.size) return blocked()
      const bytes = Buffer.alloc(held.size + 1), n = readSync(fd, bytes, 0, bytes.length, 0)
      const after = fstatSync(fd), pathname = lstatSync(path)
      if (n !== held.size || !pathname.isFile() || pathname.isSymbolicLink() || pathname.nlink !== 1
        || after.nlink !== 1 || after.dev !== held.dev || after.ino !== held.ino || pathname.dev !== held.dev || pathname.ino !== held.ino
        || after.size !== held.size || pathname.size !== held.size || after.mtimeMs !== held.mtimeMs || after.ctimeMs !== held.ctimeMs) return blocked()
      return bytes.subarray(0,n)
    } finally { closeSync(fd) }
  } catch (error) {
    if (!observed && (error as NodeJS.ErrnoException)?.code === 'ENOENT') return null
    return blocked()
  }
}
function decrypt(value: Buffer): unknown {
  if (!safeStorage.isEncryptionAvailable()) return blocked()
  try { return JSON.parse(safeStorage.decryptString(value)) } catch { return blocked() }
}
function pointerOf(value: unknown): QwenOverlayPointer {
  if (!own(value,['version','generation','order','payloadHash']) || value.version !== 1 || typeof value.generation !== 'string' || !UUID.test(value.generation)
    || !Number.isSafeInteger(value.order) || Number(value.order) < 1 || typeof value.payloadHash !== 'string' || !HEX.test(value.payloadHash)) return blocked()
  return value as QwenOverlayPointer
}
function overlayOf(value: unknown): QwenOverlay {
  if (!own(value,['version','generation','order','payloadHash','entries']) || value.version !== 1 || typeof value.generation !== 'string' || !UUID.test(value.generation)
    || !Number.isSafeInteger(value.order) || Number(value.order) < 1 || typeof value.payloadHash !== 'string' || !HEX.test(value.payloadHash)
    || !Array.isArray(value.entries) || !value.entries.length || value.entries.length > QWEN_OVERLAY_FILES.length) return blocked()
  const seen = new Set<string>()
  for (const entry of value.entries) {
    if (!own(entry,['name','sourceHash','after']) || typeof entry.name !== 'string' || !QWEN_OVERLAY_FILES.includes(entry.name as QwenOverlayFile) || seen.has(entry.name)
      || typeof entry.sourceHash !== 'string' || !(HEX.test(entry.sourceHash) || (entry.name === 'voice-settings.json' && absent(entry.sourceHash)))
      || !(entry.after === null || (typeof entry.after === 'string' && !!entry.after && entry.after.length <= 174_772
        && Buffer.from(entry.after,'base64').toString('base64') === entry.after))) return blocked()
    seen.add(entry.name)
  }
  return value as QwenOverlay
}
export function readQwenOverlay(root: string, allowPending = false): { pointer: QwenOverlayPointer; overlay: QwenOverlay } | null {
  if (!allowPending && readBounded(join(root,'qwen-key-edit-journal.bin'), 524_288)) return blocked()
  const directory = join(root,'qwen-key-rotation'), guard = join(directory,'high-water.bin')
  const required = readBounded(join(root,'qwen-key-rotation-required.bin'),65_536)
  if (required) {
    const marker = decrypt(required)
    if (!own(marker,['version','required']) || marker.version !== 1 || marker.required !== true) return blocked()
  }
  try {
    const entry = lstatSync(directory)
    if (!entry.isDirectory() || entry.isSymbolicLink()) return blocked()
  } catch (error) { if ((error as NodeJS.ErrnoException)?.code === 'ENOENT' && !required) return null; return blocked() }
  if (!required) return blocked()
  const bytes = readBounded(guard, 65_536)
  if (!bytes) return blocked()
  const pointer = pointerOf(decrypt(bytes)), encrypted = readBounded(join(directory,`${pointer.generation}.bin`),2_000_000)
  if (!encrypted) return blocked()
  const overlay = overlayOf(decrypt(encrypted))
  if (overlay.generation !== pointer.generation || overlay.order !== pointer.order || overlay.payloadHash !== pointer.payloadHash) return blocked()
  // Detect a rolled-back pointer while a later committed (or staged) generation
  // remains. Startup journal recovery runs before normal settings are read.
  for (const file of readdirSync(directory)) {
    if (!file.endsWith('.bin') || !UUID.test(file.slice(0, -4)) || file === `${pointer.generation}.bin`) continue
    const candidateBytes = readBounded(join(directory, file), 2_000_000)
    if (!candidateBytes) return blocked()
    const candidate = overlayOf(decrypt(candidateBytes))
    if (`${candidate.generation}.bin` !== file || candidate.order >= pointer.order) return blocked()
  }
  return { pointer, overlay }
}
export function applyQwenOverlay(root: string, name: QwenOverlayFile, raw: Buffer | null): Buffer | null {
  const active = readQwenOverlay(root)
  const entry = active?.overlay.entries.find(value => value.name === name)
  if (!entry) return raw
  if (!(absent(entry.sourceHash) ? raw === null : !!raw && digest(raw) === entry.sourceHash)) return blocked()
  const edit = readOverlayEdit(root, name, active!.pointer, entry.sourceHash)
  if (edit) return edit.after === null ? null : Buffer.from(edit.after,'base64')
  return entry.after === null ? null : Buffer.from(entry.after,'base64')
}

type OverlayEdit = { version: 1; generation: string; revision: number; sourceHash: string; after: string | null }
const editBase = (root: string, name: QwenOverlayFile, generation: string) => join(root,'qwen-key-rotation',`${name}.${generation}`)
function sealed(value: unknown): Buffer {
  if (!safeStorage.isEncryptionAvailable()) return blocked()
  try { const plain=JSON.stringify(value), encrypted=safeStorage.encryptString(plain);if(safeStorage.decryptString(encrypted)!==plain)return blocked();return encrypted }
  catch { return blocked() }
}
function atomicSync(path: string, bytes: Buffer): void {
  const temporary=`${path}.${randomBytes(12).toString('hex')}.tmp`
  try { const fd=openSync(temporary,'wx',0o600);try {writeFileSync(fd,bytes);fsyncSync(fd)} finally {closeSync(fd)};renameSync(temporary,path) }
  finally { try { unlinkSync(temporary) } catch {} }
}
function readOverlayEdit(root: string, name: QwenOverlayFile, pointer: QwenOverlayPointer, sourceHash: string): OverlayEdit | null {
  const base=editBase(root,name,pointer.generation), marker=readBounded(`${base}.required`,65_536), headBytes=readBounded(`${base}.head`,65_536)
  if (!marker && !headBytes) {
    if (readdirSync(join(root,'qwen-key-rotation')).some(file=>file.startsWith(`${name}.${pointer.generation}.`) && file.endsWith('.edit'))) return blocked()
    return null
  }
  if (!marker || !headBytes) return blocked()
  const mark=decrypt(marker), head=decrypt(headBytes)
  if (!own(mark,['version','generation','name']) || mark.version!==1 || mark.generation!==pointer.generation || mark.name!==name
    || !own(head,['version','generation','name','revision','hash']) || head.version!==1 || head.generation!==pointer.generation || head.name!==name
    || !Number.isSafeInteger(head.revision) || Number(head.revision)<1 || typeof head.hash!=='string' || !HEX.test(head.hash)) return blocked()
  const revisions=readdirSync(join(root,'qwen-key-rotation')).filter(file=>file.startsWith(`${name}.${pointer.generation}.`) && file.endsWith('.edit'))
    .map(file=>Number(file.slice(`${name}.${pointer.generation}.`.length,-5)))
  if (revisions.some(value=>!Number.isSafeInteger(value)||value<1) || Math.max(0,...revisions)!==head.revision) return blocked()
  const bytes = readBounded(`${base}.${head.revision}.edit`, 262_144)
  if (!bytes || digest(bytes)!==head.hash) return blocked()
  const value = decrypt(bytes)
  if (!own(value,['version','generation','revision','sourceHash','after']) || value.version !== 1 || value.generation !== pointer.generation || value.revision !== head.revision
    || typeof value.sourceHash !== 'string' || !(HEX.test(value.sourceHash) || (name==='voice-settings.json' && absent(value.sourceHash)))
    || !(value.after === null || (typeof value.after === 'string' && value.after.length <= 174_772 && Buffer.from(value.after,'base64').toString('base64') === value.after))) return blocked()
  if (value.sourceHash !== sourceHash) return blocked()
  return value as OverlayEdit
}
export function qwenOverlayTombstoned(root: string, name: QwenOverlayFile): boolean {
  const active = readQwenOverlay(root), entry = active?.overlay.entries.find(value => value.name === name)
  if (!active || !entry) return false
  const edit = readOverlayEdit(root,name,active.pointer,entry.sourceHash)
  return edit ? edit.after === null : entry.after === null
}
export function qwenOverlayImplicitVoice(root: string): boolean {
  const active=readQwenOverlay(root),entry=active?.overlay.entries.find(value=>value.name==='voice-settings.json')
  return !!(active && entry?.sourceHash==='absent-implicit' && !readOverlayEdit(root,'voice-settings.json',active.pointer,entry.sourceHash))
}
export function qwenOverlayFingerprint(root: string): string {
  const active = readQwenOverlay(root), hash = createHash('sha256')
  if (!active) return hash.update('none').digest('hex')
  hash.update(JSON.stringify(active))
  for (const name of QWEN_OVERLAY_FILES) {
    const entry=active.overlay.entries.find(value=>value.name===name)
    hash.update(name).update('\0').update(entry ? JSON.stringify(readOverlayEdit(root,name,active.pointer,entry.sourceHash)) : 'none')
  }
  return hash.digest('hex')
}
/** A controlled parent save/clear updates only an encrypted overlay edit. */
export function writeQwenOverlayEdit(root: string, name: QwenOverlayFile, after: Buffer | null): boolean {
  const active = readQwenOverlay(root), entry = active?.overlay.entries.find(value => value.name === name)
  if (!active || !entry) return false
  const original = readBounded(join(root,name), 131_072)
  if (!(absent(entry.sourceHash) ? original===null : !!original && digest(original)===entry.sourceHash) || (after && after.length > 131_072)) return blocked()
  const previous=readOverlayEdit(root,name,active.pointer,entry.sourceHash),revision=(previous?.revision ?? 0)+1
  if (!Number.isSafeInteger(revision)) return blocked()
  const value: OverlayEdit = { version:1, generation:active.pointer.generation, revision, sourceHash:entry.sourceHash, after:after?.toString('base64') ?? null }
  const oldHead=readBounded(`${editBase(root,name,active.pointer.generation)}.head`,65_536)
  const bytes=sealed(value), journal={version:1,name,generation:active.pointer.generation,sourceHash:entry.sourceHash,revision,priorHeadHash:oldHead?digest(oldHead):null,bytes:bytes.toString('base64')}
  atomicSync(join(root,'qwen-key-edit-journal.bin'),sealed(journal))
  finishOverlayEdit(root,journal)
  return true
}

function finishOverlayEdit(root: string, journal: {name:QwenOverlayFile;generation:string;sourceHash:string;revision:number;priorHeadHash:string|null;bytes:string}): void {
  const active=readQwenOverlay(root,true), entry=active?.overlay.entries.find(value=>value.name===journal.name), original=readBounded(join(root,journal.name),131_072)
  if (!active || active.pointer.generation!==journal.generation || !entry || entry.sourceHash!==journal.sourceHash
    || !(absent(journal.sourceHash) ? original===null : !!original && digest(original)===journal.sourceHash)) return blocked()
  const base=editBase(root,journal.name,journal.generation), target=`${base}.${journal.revision}.edit`, expected=Buffer.from(journal.bytes,'base64')
  const parsed=decrypt(expected)
  if (!own(parsed,['version','generation','revision','sourceHash','after']) || parsed.version!==1 || parsed.generation!==journal.generation
    || parsed.revision!==journal.revision || parsed.sourceHash!==journal.sourceHash) return blocked()
  let current=readBounded(target,262_144)
  if (current && !current.equals(expected)) {
    if (!expected.subarray(0,current.length).equals(current)) return blocked()
    unlinkSync(target);current=null
  }
  if (!current) { const fd=openSync(target,'wx',0o600);try {writeFileSync(fd,expected);fsyncSync(fd)} finally {closeSync(fd)} }
  const marker=readBounded(`${base}.required`,65_536)
  if (!marker) atomicSync(`${base}.required`,sealed({version:1,generation:journal.generation,name:journal.name}))
  else { const value=decrypt(marker);if(!own(value,['version','generation','name'])||value.version!==1||value.generation!==journal.generation||value.name!==journal.name)return blocked() }
  const head=readBounded(`${base}.head`,65_536)
  if (head) { const value=decrypt(head);if(!own(value,['version','generation','name','revision','hash'])||value.version!==1||value.generation!==journal.generation||value.name!==journal.name
    || !((value.revision===journal.revision && value.hash===digest(expected)) || (value.revision===journal.revision-1 && journal.priorHeadHash===digest(head))))return blocked() }
  else if (journal.revision!==1 || journal.priorHeadHash!==null) return blocked()
  atomicSync(`${base}.head`,sealed({version:1,generation:journal.generation,name:journal.name,revision:journal.revision,hash:digest(expected)}))
  const confirmed=readOverlayEdit(root,journal.name,active.pointer,journal.sourceHash)
  if (!confirmed || confirmed.revision!==journal.revision) return blocked()
  unlinkSync(join(root,'qwen-key-edit-journal.bin'))
}
export function recoverQwenOverlayEdit(root: string): boolean {
  const bytes=readBounded(join(root,'qwen-key-edit-journal.bin'),524_288)
  if (!bytes) return false
  const value=decrypt(bytes)
  if (!own(value,['version','name','generation','sourceHash','revision','priorHeadHash','bytes']) || value.version!==1 || typeof value.name!=='string' || !QWEN_OVERLAY_FILES.includes(value.name as QwenOverlayFile)
    || typeof value.generation!=='string' || !UUID.test(value.generation) || typeof value.sourceHash!=='string' || !(HEX.test(value.sourceHash)||(value.name==='voice-settings.json' && absent(value.sourceHash)))
    || !Number.isSafeInteger(value.revision) || Number(value.revision)<1 || !(value.priorHeadHash===null || (typeof value.priorHeadHash==='string' && HEX.test(value.priorHeadHash)))
    || typeof value.bytes!=='string' || value.bytes.length>350_000 || Buffer.from(value.bytes,'base64').toString('base64')!==value.bytes) return blocked()
  finishOverlayEdit(root,value as unknown as {name:QwenOverlayFile;generation:string;sourceHash:string;revision:number;priorHeadHash:string|null;bytes:string})
  return true
}
