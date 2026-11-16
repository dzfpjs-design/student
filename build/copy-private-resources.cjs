'use strict'
// Private files are inputs to NSIS, never to app.asar or source archives.
// An absent explicit directory leaves the normal build free of private inputs.
const { constants } = require('node:fs')
const fs = require('node:fs/promises')
const path = require('node:path')
const crypto = require('node:crypto')
const { TextDecoder, isDeepStrictEqual } = require('node:util')
const Module = require('node:module')
const NAMES = ['household-bootstrap.bin', 'household-bootstrap.key', 'qwen-rotation.bin', 'qwen-rotation.key']
const TUTOR_NAMES = ['tutor-explanation.bin', 'tutor-explanation.key']
const fail = () => { throw new Error('PRIVATE_RESOURCES_INVALID') }
const exact = (x, names) => x && typeof x === 'object' && !Array.isArray(x) && Object.keys(x).sort().join('|') === names.slice().sort().join('|')
const compare = (a, b) => {
  if (typeof a !== 'string' || typeof b !== 'string' || Buffer.byteLength(a) !== Buffer.byteLength(b)) return false
  const left = Buffer.from(a), right = Buffer.from(b)
  try { return crypto.timingSafeEqual(left,right) } finally { left.fill(0); right.fill(0) }
}
const normalize = x => process.platform === 'win32' ? path.normalize(x).toLowerCase() : path.normalize(x)
const inside = (parent, child) => { const rel = path.relative(parent, child); return !rel || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel)) }

async function readChecked(file, limit) {
  const before = await fs.lstat(file)
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || before.size > limit) fail()
  const h = await fs.open(file, constants.O_RDONLY | (constants.O_NOFOLLOW || 0))
  try {
    const held = await h.stat()
    if (!held.isFile() || held.nlink !== 1 || held.dev !== before.dev || held.ino !== before.ino || held.size !== before.size) fail()
    const bytes = Buffer.alloc(held.size + 1)
    let offset = 0
    while (offset < bytes.length) { const read = await h.read(bytes, offset, bytes.length - offset, offset); if (!read.bytesRead) break; offset += read.bytesRead }
    const after = await h.stat(), current = await fs.lstat(file)
    if (!current.isFile() || current.isSymbolicLink() || current.nlink !== 1 || offset !== held.size
      || after.dev !== held.dev || after.ino !== held.ino || after.size !== held.size || after.mtimeMs !== held.mtimeMs || after.ctimeMs !== held.ctimeMs
      || current.dev !== before.dev || current.ino !== before.ino || current.size !== before.size || current.mtimeMs !== before.mtimeMs || current.ctimeMs !== before.ctimeMs) fail()
    return bytes.subarray(0,offset)
  } finally { await h.close() }
}

async function readPrivateDirectory(dir, project, out, names) {
  if (!path.isAbsolute(dir)) fail()
  const resolved = await fs.realpath(dir)
  if (normalize(resolved) !== normalize(path.resolve(dir)) || inside(project,resolved) || inside(out,resolved)) fail()
  const stat = await fs.lstat(resolved)
  if (!stat.isDirectory() || stat.isSymbolicLink()) fail()
  const files = await fs.readdir(resolved)
  if (files.length !== names.length || files.some(name => !names.includes(name))) fail()
  return resolved
}

function validateTutorPair(payload, key) {
  const magic = Buffer.from('HSCTUT01')
  if (key.length !== 32 || payload.length <= 36 || !payload.subarray(0,8).equals(magic)) fail()
  const cipher = crypto.createDecipheriv('aes-256-gcm', key, payload.subarray(8,20))
  cipher.setAAD(magic)
  cipher.setAuthTag(payload.subarray(20,36))
  let plaintext
  try {
    plaintext = Buffer.concat([cipher.update(payload.subarray(36)), cipher.final()])
    if (!/^[!-~]{16,512}$/.test(plaintext.toString('utf8'))) fail()
  } finally { plaintext?.fill(0) }
}
function decrypt(bytes, key, magic, aad, tagFirst) {
  if (key.length !== 32 || bytes.length <= 36 || bytes.length > 65536 || !bytes.subarray(0,8).equals(Buffer.from(magic))) fail()
  let plaintext
  try {
    const cipher = crypto.createDecipheriv('aes-256-gcm', key, bytes.subarray(8,20))
    if (aad) cipher.setAAD(Buffer.from(magic))
    cipher.setAuthTag(tagFirst ? bytes.subarray(20,36) : bytes.subarray(-16))
    plaintext = Buffer.concat([cipher.update(tagFirst ? bytes.subarray(36) : bytes.subarray(20,-16)), cipher.final()])
    const text = new TextDecoder('utf8',{fatal:true}).decode(plaintext)
    const parsed = JSON.parse(text)
    if (tagFirst && JSON.stringify(parsed) !== text) fail()
    return parsed
  } finally { plaintext?.fill(0) }
}
function validateWithRuntime(projectDir, value) {
  // Compile the exact installed bootstrap validator in memory, so private builds
  // reject payloads the real startup importer would reject. Never log values.
  const entry = path.join(projectDir, 'src/main/household-provisioning.ts')
  const requireFromProject = Module.createRequire(path.join(projectDir, 'package.json'))
  const esbuild = requireFromProject('esbuild')
  const built = esbuild.buildSync({ entryPoints:[entry], bundle:true, platform:'node', format:'cjs', external:['electron'], write:false, logLevel:'silent' })
  const loaded = new Module(entry, module)
  loaded.filename = entry
  loaded.paths = Module._nodeModulePaths(path.dirname(entry))
  loaded._compile(built.outputFiles[0].text, entry)
  const normalized = loaded.exports.validateHouseholdBootstrap(value)
  if (!isDeepStrictEqual(normalized, value)) fail()
}
const QWEN_MODELS = new Set(['qwen3.6-plus','qwen3.7-plus','qwen3.8-flash','qwen3-asr-flash','qwen3-asr-flash-realtime','qwen3-vl-flash','qwen3-vl-plus','qwen3.5-omni-plus-2026-03-15','qwen3.8-omni-flash-realtime','qwen3-tts-vc-2026-01-22'])
const OTHER_MODELS = new Set(['deepseek-v4-pro','deepseek-v4-flash','deepseek-chat','deepseek-reasoner','paraformer-realtime-v2'])
function classifyModel(model) {
  if (typeof model !== 'string') fail()
  const normalized = model.toLowerCase()
  if (QWEN_MODELS.has(normalized)) return 'qwen'
  if (OTHER_MODELS.has(normalized)) return 'other'
  return fail()
}
function validatePair(bootstrap, rotation) {
  if (!exact(rotation,['version','generation','order','apiKey']) || rotation.version !== 1
    || typeof rotation.generation !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(rotation.generation)
    || !Number.isSafeInteger(rotation.order) || rotation.order < 1 || typeof rotation.apiKey !== 'string'
    || rotation.apiKey.length < 16 || rotation.apiKey.length > 512 || /[\x00-\x20\x7f]/.test(rotation.apiKey)) fail()
  if (!exact(bootstrap,['version','bootstrapId','settings']) || bootstrap.version !== 1
    || typeof bootstrap.bootstrapId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(bootstrap.bootstrapId)) fail()
  const s = bootstrap.settings
  if (!s || typeof s !== 'object' || Array.isArray(s) || Object.keys(s).some(k => !['model','asr','qwenRealtime','reportDeliverySecrets','smtpDefaults'].includes(k))) fail()
  // These slots are necessary to let an empty Windows profile start at all.
  if (!s.model || !s.asr || !s.qwenRealtime || !s.reportDeliverySecrets?.smtpPassword || !s.smtpDefaults) fail()
  if (classifyModel(s.asr.model) !== 'qwen' || s.asr.provider !== 'dashscope' || !compare(s.asr.apiKey,rotation.apiKey)
    || s.qwenRealtime.model !== 'qwen3.8-omni-flash-realtime' || !compare(s.qwenRealtime.apiKey,rotation.apiKey)) fail()
  for (const model of [s.model,s.model.fallback].filter(Boolean)) {
    if (classifyModel(model.model) === 'qwen' && !compare(model.apiKey,rotation.apiKey)) fail()
  }
  // The runtime uses the model key for intent unless a separate key exists.
  // A fresh bootstrap must never bind Qwen intent to an old non-Qwen key.
  if (classifyModel(s.model.voiceIntentModel || s.model.model) === 'qwen' && !compare(s.model.voiceIntentApiKey ?? s.model.apiKey,rotation.apiKey)) fail()
  if (s.smtpDefaults.host !== 'smtp.qq.com' || s.smtpDefaults.port !== 465 || s.smtpDefaults.security !== 'ssl') fail()
}
module.exports = async function copyPrivateResources(context, destination) {
  const dir = process.env.HSC_PRIVATE_BUILD_RESOURCES_DIR
  const tutorDir = process.env.HSC_TUTOR_EXPLANATION_BUILD_RESOURCES_DIR
  const requested = process.env.HSC_PRIVATE_BUILD
  if (!requested && !dir && !tutorDir) return []
  if (requested !== '1' || !dir) fail()
  const created = [], buffers = []
  try {
    if (typeof context.packager?.projectDir !== 'string') fail()
    const project = await fs.realpath(context.packager.projectDir), out = await fs.realpath(context.appOutDir)
    const resolved = await readPrivateDirectory(dir, project, out, NAMES)
    const targetDirectory = await fs.lstat(destination), targetReal = await fs.realpath(destination)
    if (!targetDirectory.isDirectory() || targetDirectory.isSymbolicLink()
      || normalize(targetReal) !== normalize(path.join(out,'resources'))) fail()
    const values = {}
    for (const name of NAMES) { const data = await readChecked(path.join(resolved,name),name.endsWith('.key') ? 32 : 65536); buffers.push(data); values[name] = data }
    const bootstrap = decrypt(values['household-bootstrap.bin'],values['household-bootstrap.key'],'HSCBOOT1',true,true)
    const rotation = decrypt(values['qwen-rotation.bin'],values['qwen-rotation.key'],'HSCROT01',false,false)
    validateWithRuntime(context.packager.projectDir,bootstrap)
    validatePair(bootstrap,rotation)
    if (tutorDir) {
      const resolvedTutor = await readPrivateDirectory(tutorDir, project, out, TUTOR_NAMES)
      for (const name of TUTOR_NAMES) {
        const data = await readChecked(path.join(resolvedTutor,name), name.endsWith('.key') ? 32 : 1024)
        buffers.push(data)
        values[name] = data
      }
      validateTutorPair(values[TUTOR_NAMES[0]], values[TUTOR_NAMES[1]])
    }
    for (const name of Object.keys(values)) {
      const target = path.join(destination,name)
      const file = await fs.open(target,'wx',0o600); created.push(target)
      try { await file.writeFile(values[name]); await file.sync() } finally { await file.close() }
    }
    return created
  } catch {
    for (const file of created) await fs.unlink(file).catch(() => {})
    fail()
  } finally { for (const bytes of buffers) bytes.fill(0) }
}
