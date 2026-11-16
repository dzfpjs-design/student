#!/usr/bin/env node
/** Isolated real Electron + packaged-agent fault gate and resumable soak. */
import assert from 'node:assert/strict'
import { spawn, execFile } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, createReadStream } from 'node:fs'
import { mkdir, mkdtemp, readFile, writeFile, rename, appendFile, readdir, rm } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir, cpus } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'
import { awaitCdpResponse, windowsProcessTree, stopWindowsProcessTree, releaseChildProcessHandles } from './electron-owned-process.mjs'

const exec = promisify(execFile), root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const sleep = ms => new Promise(done => setTimeout(done, ms))
const usage = 'node scripts/windows-runtime-stability.mjs --mode gate|recovery|soak --electron-bin PATH --exe PATH --python-bin PATH --model-dir PATH --output-dir PATH [--app-asar PATH] [--app-binary PATH] [--approved-build SHA256] [--gate-seconds 180]\nResume: --resume checkpoint.json --approved-build SHA256\nStatus: --status checkpoint.json\nBuild: --print-build --exe PATH --electron-bin PATH --python-bin PATH [--app-asar PATH] [--app-binary PATH]\nRecovery is a short interruption drill, never eight-hour evidence. Soak never starts without an explicit matching --approved-build fingerprint.'
const pathKeys = ['--electron-bin', '--exe', '--python-bin', '--model-dir', '--output-dir']
const optionalPaths = ['--app-asar', '--app-binary']

export function argumentsFor(argv) {
  const args = {}
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i]
    assert(!Object.hasOwn(args, key), `Duplicate ${key}`)
    if (key === '--help' || key === '--print-build') args[key] = true
    else {
      assert([...pathKeys, ...optionalPaths, '--mode', '--resume', '--status', '--approved-build', '--gate-seconds'].includes(key), `Unknown ${key}`)
      assert(argv[i + 1] && !argv[i + 1].startsWith('--'), `Missing ${key}`)
      args[key] = argv[++i]
    }
  }
  if (args['--help'] || args['--status'] || args['--resume']) return args
  if (args['--print-build']) { for (const key of ['--exe', '--electron-bin', '--python-bin']) assert(args[key], `Build fingerprint requires ${key}`); return args }
  assert(['gate', 'recovery', 'soak'].includes(args['--mode']), 'Choose gate, recovery or soak explicitly')
  for (const key of pathKeys) assert(args[key], `Missing ${key}`)
  if (args['--mode'] !== 'gate') assert(/^[a-f0-9]{64}$/.test(args['--approved-build'] ?? ''), 'Recovery/soak needs the approved build SHA256')
  const seconds = Number(args['--gate-seconds'] ?? 180)
  assert(Number.isInteger(seconds) && seconds >= 30 && seconds <= 600, 'Gate duration must be 30..600 seconds')
  return args
}

export function isolatedEnvironment(config, temp) {
  const env = sanitizedHarnessEnvironment(process.env)
  for (const key of Object.keys(env)) if (key.startsWith('HSC_') || /API_KEY|ACCESS_KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD|KEYCHAIN/i.test(key)
    || /^(NODE_OPTIONS|ELECTRON_RUN_AS_NODE|ELECTRON_RENDERER_URL|PYTHONPATH|PYTHONHOME|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)$/i.test(key)) delete env[key]
  return { ...env, HSC_ELECTRON_USER_DATA_DIR: join(temp, 'profile'), HSC_DATA_DIR: join(temp, 'profile/family-data'),
    HSC_DATABASE_PATH: join(temp, 'profile/family-data/stability.db'), HSC_LOCAL_AGENT_PYTHON: config['--python-bin'],
    HSC_LOCAL_MODEL_DIR: config['--model-dir'], PYTHONDONTWRITEBYTECODE: '1',
    HTTP_PROXY: 'http://127.0.0.1:9', HTTPS_PROXY: 'http://127.0.0.1:9', ALL_PROXY: 'http://127.0.0.1:9', NO_PROXY: '127.0.0.1,localhost,::1' }
}

export function creditedSeconds(delta, interval = 60) { return delta >= 0 && delta <= interval * 2 ? delta : 0 }
export function sameProcess(actual, expected) { return Boolean(actual && expected && actual.pid === expected.pid && actual.created === expected.created && actual.exe?.toLowerCase() === expected.exe?.toLowerCase()) }
async function atomic(path, value) { const temp = `${path}.${randomUUID()}.tmp`; await writeFile(temp, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 }); await rename(temp, path) }
async function eventually(read, label, ms = 30_000) {
  const until = Date.now() + ms; let error
  while (Date.now() < until) { try { const value = await read(); if (value) return value } catch (e) { error = e } await sleep(250) }
  throw new Error(`${label}: ${error?.message ?? 'timeout'}`)
}
async function ps(command) { const { stdout } = await exec('powershell.exe', ['-NoProfile', '-Command', command], { windowsHide: true, timeout: 20_000, maxBuffer: 1024 * 1024 }); return stdout.trim() }
async function processInfo(ids) {
  assert(ids.every(id => Number.isSafeInteger(id) && id > 0))
  if (!ids.length) return []
  const result = await ps(`$ids=@(${ids.join(',')}); @(Get-CimInstance Win32_Process | Where-Object {$ids -contains [int]$_.ProcessId} | ForEach-Object { [pscustomobject]@{pid=[int]$_.ProcessId;parent=[int]$_.ParentProcessId;exe=$_.ExecutablePath;created=$_.CreationDate.ToUniversalTime().ToString('o');rss=[double]$_.WorkingSetSize;cpu=([double]$_.KernelModeTime+[double]$_.UserModeTime)/10000000;handles=[int]$_.HandleCount}}) | ConvertTo-Json -Compress`)
  const value = result ? JSON.parse(result) : []; return Array.isArray(value) ? value : [value]
}
async function freePort() { const s = createServer(); await new Promise((ok, no) => s.once('error', no).listen(0, '127.0.0.1', ok)); const p = s.address().port; await new Promise(ok => s.close(ok)); return p }
async function stopIdentities(identities) {
  for (const identity of identities.filter(Boolean)) {
    if (sameProcess((await processInfo([identity.pid]))[0], identity)) await stopWindowsProcessTree({ pid: identity.pid }, eventually)
  }
}

// Test-only launcher selection; no application code or packaged resources change.
// LocalServiceManager still owns the real executable's start/crash/recovery lifecycle.
function entry(appRoot, temp, python, executable, auditPath) {
  const { app, session } = require('electron'), childProcess = require('node:child_process')
  const { appendFileSync } = require('node:fs'), { join, resolve } = require('node:path')
  const audit = (event, values = {}) => appendFileSync(auditPath, JSON.stringify({ event, ...values }) + '\n')
  if (app.isPackaged) throw new Error('Never launch an installed family application')
  app.setPath('userData', join(temp, 'profile')); app.getAppPath = () => appRoot
  const original = childProcess.spawn
  childProcess.spawn = function(command, args, options) {
    if (resolve(command).toLowerCase() === resolve(python).toLowerCase() && args?.[0] === '-m' && args[1] === 'home_study_agent') {
      if (options?.env?.HSC_DATABASE_PATH !== join(temp, 'profile/family-data/stability.db')) throw new Error('backend isolation mismatch')
      const process = original.call(this, executable, args.slice(2), options)
      audit('packaged_backend_spawn', { pid: process.pid }); return process
    }
    return original.call(this, command, args, options)
  }
  require('node:module').syncBuiltinESMExports()
  app.on('web-contents-created', (_, contents) => contents.setAudioMuted(true))
  app.once('ready', () => {
    const ses = session.defaultSession, check = ses.setPermissionCheckHandler.bind(ses), request = ses.setPermissionRequestHandler.bind(ses)
    ses.setPermissionCheckHandler = () => check((_, permission) => { audit('permission_denied', { permission }); return false })
    ses.setPermissionRequestHandler = () => request((_, permission, callback) => { audit('permission_denied', { permission }); callback(false) })
    ses.setPermissionCheckHandler(); ses.setPermissionRequestHandler()
    ses.webRequest.onBeforeRequest((details, callback) => {
      const u = new URL(details.url), network = ['http:', 'https:', 'ws:', 'wss:'].includes(u.protocol)
      callback({ cancel: network && (!['127.0.0.1', 'localhost', '[::1]'].includes(u.hostname) || /\/(capture|transcribe|synthesize|observation\/start|document-monitor\/start)(\/|$)/.test(u.pathname)) })
    })
    audit('isolation_ready', { profile: app.getPath('userData') })
  })
  import(require('node:url').pathToFileURL(join(appRoot, 'out/main/index.js')).href).catch(() => app.exit(1))
}

class Cdp {
  constructor(url) { this.ws = new WebSocket(url); this.id = 0; this.pending = new Map() }
  async open() {
    this.ws.addEventListener('message', e => { const m = JSON.parse(e.data), p = this.pending.get(m.id); if (p) { this.pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result) } })
    this.ws.addEventListener('close', () => { for (const p of this.pending.values()) p.reject(new Error('CDP closed')); this.pending.clear() })
    await new Promise((ok, no) => { this.ws.addEventListener('open', ok, { once: true }); this.ws.addEventListener('error', no, { once: true }) })
  }
  send(method, params = {}) { const id = ++this.id, p = awaitCdpResponse(this.pending, id, method, 30_000); this.ws.send(JSON.stringify({ id, method, params })); return p }
  async evaluate(expression) { const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text); return r.result.value }
  close() { this.ws.close() }
}

export async function buildManifest(config, appRoot = root, nodeBin = process.execPath) {
  const files = []
  const add = (label, path) => files.push({ label, path: resolve(path) })
  const tree = async (label, path) => {
    for (const item of (await readdir(path, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name, 'en'))) {
      assert(!item.isSymbolicLink(), `Fingerprint refuses linked input: ${join(path, item.name)}`)
      if (item.isDirectory()) await tree(`${label}/${item.name}`, join(path, item.name))
      else if (item.isFile()) add(`${label}/${item.name}`, join(path, item.name))
    }
  }
  for (const file of ['out/main/index.js', 'out/preload/index.js', 'out/renderer/index.html']) assert(existsSync(join(appRoot, file)), `Missing ${file}`)
  add('runtime/node', nodeBin); add('runtime/python', config['--python-bin']); add('backend/exe', config['--exe']); add('runtime/electron-selected', config['--electron-bin'])
  await tree('ui', join(appRoot, 'out'))
  await tree('runtime/electron', dirname(resolve(config['--electron-bin'])))
  for (const file of ['windows-runtime-stability.mjs', 'runtime-stability-sqlite.py', 'electron-harness-env.mjs', 'electron-owned-process.mjs']) add(`harness/${file}`, join(appRoot, 'scripts', file))
  for (const key of optionalPaths) if (config[key]) add(`optional/${key.slice(2)}`, config[key])
  for (const file of files) {
    const hash = createHash('sha256'); let bytes = 0
    for await (const chunk of createReadStream(file.path)) { hash.update(chunk); bytes += chunk.length }
    file.sha256 = hash.digest('hex'); file.bytes = bytes
  }
  return { schema: 2, repo: resolve(appRoot), node: resolve(nodeBin), files,
    digest: createHash('sha256').update(JSON.stringify(files.map(({ label, bytes, sha256 }) => ({ label, bytes, sha256 })))).digest('hex') }
}
async function fingerprint(config) { return (await buildManifest(config)).digest }

async function main(argv) {
  const args = argumentsFor(argv)
  if (args['--help']) { console.log(usage); return }
  if (args['--print-build']) { console.log(await fingerprint(args)); return }
  if (args['--status']) {
    const s = JSON.parse(await readFile(resolve(args['--status']), 'utf8'))
    const alive = process.platform === 'win32' && s.runner ? sameProcess((await processInfo([s.runner.pid]))[0], s.runner) : null
    console.log(JSON.stringify({ status: s.status === 'running' && alive === false ? 'interrupted_runner_not_alive' : s.status, runner_alive: alive,
      observed_seconds: s.observed_seconds, target_seconds: s.target_seconds, checkpoint: s.updated_at,
      checkpoint_age_seconds: Math.round((Date.now() - Date.parse(s.updated_at)) / 1000), stage: s.stage,
      interruptions: s.interruptions, failures: s.failures?.length ?? 0, learning_days: Object.keys(s.learning_days ?? {}),
      error: s.error, build: s.build, latest: s.latest })); return
  }
  assert.equal(process.platform, 'win32', 'Run fault/soak gates on Windows, not against local family apps')
  let state, checkpoint
  if (args['--resume']) {
    checkpoint = resolve(args['--resume']); state = JSON.parse(await readFile(checkpoint, 'utf8'))
    assert(['recovery', 'soak'].includes(state.mode), 'Only an interrupted recovery/soak can resume')
    assert.equal(args['--approved-build'], state.build, 'Resume requires explicit approved-build hash')
    assert(['running', 'interrupted', 'failed'].includes(state.status), 'Already complete')
    assert.equal(dirname(resolve(state.temp)).toLowerCase(), resolve(tmpdir()).toLowerCase())
    assert(/^hsc-runtime-stability-/.test(state.temp.split(/[\\/]/).pop()))
    assert.deepEqual(JSON.parse(await readFile(join(state.temp, 'owner.json'), 'utf8')), { owner: state.owner, synthetic_only: true })
    assert(!sameProcess((await processInfo([state.runner.pid]))[0], state.runner), 'Previous runner still active')
    await stopIdentities([state.electron, ...(state.owned ?? [])])
  } else {
    const config = Object.fromEntries([...pathKeys, ...optionalPaths.filter(k => args[k])].map(k => [k, resolve(args[k])]))
    for (const key of pathKeys.filter(k => k !== '--output-dir')) assert(existsSync(config[key]), `Missing ${key}`)
    assert(existsSync(join(dirname(config['--electron-bin']), 'resources/default_app.asar')), 'Unpackaged Electron required')
    await mkdir(config['--output-dir'], { recursive: true })
    const runDir = await mkdtemp(join(config['--output-dir'], 'stability-')), temp = await mkdtemp(join(tmpdir(), 'hsc-runtime-stability-')), owner = randomUUID()
    await atomic(join(temp, 'owner.json'), { owner, synthetic_only: true }); await mkdir(join(temp, 'profile'))
    state = { version: 1, mode: args['--mode'], config, runDir, temp, owner, status: 'running', observed_seconds: 0,
      target_seconds: args['--mode'] === 'soak' ? 8 * 3600 : Number(args['--gate-seconds'] ?? 180), checks: {}, journeys: 0,
      limitations: ['No real microphone/camera success or cloud model quality is tested.', 'Repeated synthetic sessions are not elapsed calendar days.', 'Soak time excludes runner downtime; interruptions remain recorded.'], interruptions: 0 }
    checkpoint = join(runDir, 'checkpoint.json')
  }
  const manifest = await buildManifest(state.config), build = manifest.digest
  if (state.mode !== 'gate') assert.equal(args['--approved-build'], build, 'Build approval does not match current binaries')
  if (state.build) assert.equal(state.build, build, 'Build changed; do not mix evidence across versions')
  await atomic(join(state.runDir, 'build-manifest.json'), manifest)
  state.build = build; state.runner = (await processInfo([process.pid]))[0]; state.status = 'running'; state.interruptions += args['--resume'] ? 1 : 0
  state.failures ??= []; state.learning_days ??= {}; delete state.error
  let child, cdp, runtime, stopped = false, failure, lastSample, lastTick = performance.now(), stopRequested = false
  const save = async stage => { state.stage = stage; state.updated_at = new Date().toISOString(); await atomic(checkpoint, state) }
  const signal = () => { stopRequested = true }
  process.on('SIGINT', signal); process.on('SIGTERM', signal)
  const api = async (path, body, expected = 200, parent) => {
    assert(path.startsWith('/v1/'))
    const response = await fetch(runtime.localApiUrl + path, { method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken, ...(parent ? { 'X-HSC-Parent': parent } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(45_000), redirect: 'error' })
    assert.equal(response.status, expected, `${path} returned ${response.status}`); return response.json()
  }
  const healthy = async () => {
    const r = await cdp.evaluate('window.desktop.getRuntimeConfig()'); if (r?.status !== 'ready') return false
    assert.equal(new URL(r.localApiUrl).hostname, '127.0.0.1'); runtime = r
    return (await api('/v1/health')).status === 'ok'
  }
  const db = async () => { const { stdout } = await exec(state.config['--python-bin'], [join(root, 'scripts/runtime-stability-sqlite.py'), '--root', state.temp, '--owner', state.owner], { env: isolatedEnvironment(state.config, state.temp), timeout: 15_000, maxBuffer: 128_000 }); const result = JSON.parse(stdout); assert.equal(result.quick_check, 'ok'); return result }
  const sample = async () => {
    assert(await healthy(), 'Local runtime is unhealthy')
    const all = await processInfo(await windowsProcessTree(child.pid)), now = performance.now()
    const cpu = all.reduce((sum, p) => sum + p.cpu, 0), rss = all.reduce((sum, p) => sum + p.rss, 0)
    const delta = (now - lastTick) / 1000
    state.latest = { at: new Date().toISOString(), rss_mb: Math.round(rss / 1048576), process_count: all.length, handles: all.reduce((s, p) => s + p.handles, 0),
      cpu_machine_percent: lastSample ? Math.max(0, Math.round((cpu - lastSample.cpu) / ((now - lastSample.time) / 1000) / cpus().length * 1000) / 10) : null,
      api_port: Number(new URL(runtime.localApiUrl).port), sqlite: await db() }
    state.owned = all; state.observed_seconds += creditedSeconds(delta); lastTick = now
    lastSample = { cpu, time: now }; await appendFile(join(state.runDir, 'samples.jsonl'), JSON.stringify(state.latest) + '\n'); await save('observing')
  }
  const journey = async () => {
    const n = state.journeys, childId = n % 2 ? 'child-primary-4' : 'child-junior-3', key = `stability:${state.owner}:${n}`
    const bootstrap = await api('/v1/bootstrap')
    const session = bootstrap.active_session ?? await api('/v1/sessions', { child_id: childId, client_action_id: key + ':session' }, 201)
    assert.equal(session.child_id, childId, 'Unfinished journey belongs to a different synthetic child')
    const path = `/v1/sessions/${session.session_id}`
    let workspace = await api(path)
    const transitions = { check_in: 'classroom_recall', classroom_recall: 'homework_capture', homework_capture: 'plan_review', plan_review: 'study', study: 'reflection' }
    while (workspace.session.status === 'active' && workspace.session.state !== 'reflection') {
      const before = workspace.session.state
      if (before === 'classroom_recall') await api(path + '/recalls/none', { client_action_id: key + ':no-recall' })
      if (before === 'homework_capture') await api(path + '/homework', { subject: '数学', title: `合成长稳作业${n}`, expected_minutes: 10, client_action_id: key + ':homework' }, 201)
      assert(transitions[before], `Unexpected journey state ${before}`)
      await api(path + '/transition', { expected_state: before, target_state: transitions[before], reason: 'Synthetic recovery journey; no real mastery claim', client_action_id: key + ':' + before })
      workspace = await api(path)
    }
    const finish = { skipped: true, client_action_id: key + ':finish' }
    await api(path + '/reflection/complete', finish)
    const summary = await api(path + '/learning-summary'); assert.equal(summary.cloud_transmission_allowed, false); assert.equal(summary.child_id, childId)
    const before = await db(); await api(path + '/reflection/complete', finish); assert.deepEqual((await db()).counts, before.counts)
    for (const [dayKey, receipt] of Object.entries(state.learning_days)) {
      if (receipt.day < summary.learning_date) {
        const previous = await api(`/v1/children/${receipt.child}/daily-learning-summary?learning_date=${receipt.day}`)
        assert.equal(createHash('sha256').update(JSON.stringify(previous)).digest('hex'), receipt.hash, `Historical learning day changed: ${dayKey}`)
      }
    }
    const daily = await api(`/v1/children/${childId}/daily-learning-summary?learning_date=${summary.learning_date}`)
    assert.equal(daily.child_id, childId); assert.equal(daily.cloud_transmission_allowed, false)
    state.learning_days[`${childId}:${summary.learning_date}`] = { child: childId, day: summary.learning_date, hash: createHash('sha256').update(JSON.stringify(daily)).digest('hex') }
    state.journeys += 1; state.checks.synthetic_session_replay = true; await save('journey_complete')
  }
  try {
    await save('starting')
    const wrapper = join(state.temp, 'launcher'); await mkdir(wrapper, { recursive: true })
    await atomic(join(wrapper, 'package.json'), { name: 'hsc-isolated-stability', version: '1.0.0', main: 'main.cjs' })
    const auditPath = join(state.runDir, 'isolation.jsonl')
    await writeFile(join(wrapper, 'main.cjs'), `(${entry.toString()})(${[root, state.temp, state.config['--python-bin'], state.config['--exe'], auditPath].map(JSON.stringify).join(',')});`)
    state.cdp_port = await freePort()
    child = spawn(state.config['--electron-bin'], [wrapper, `--remote-debugging-port=${state.cdp_port}`, '--remote-debugging-address=127.0.0.1'], { cwd: root, env: isolatedEnvironment(state.config, state.temp), stdio: 'ignore' })
    let spawnError; child.once('error', error => { spawnError = error })
    state.electron = await eventually(async () => (await processInfo([child.pid]))[0], 'owned Electron identity')
    await save('electron_started')
    const page = await eventually(async () => { if (spawnError) throw spawnError; assert(child.exitCode === null, 'Electron exited'); const list = await fetch(`http://127.0.0.1:${state.cdp_port}/json/list`, { signal: AbortSignal.timeout(1000) }).then(r => r.json()); return list.find(p => p.type === 'page' && p.url.startsWith('file:')) }, 'renderer', 120_000)
    cdp = new Cdp(page.webSocketDebuggerUrl); await cdp.open(); await cdp.send('Page.enable'); await cdp.send('Network.enable')
    await eventually(healthy, 'packaged backend ready', 90_000)
    const bootstrap = await api('/v1/bootstrap'); assert.equal(bootstrap.model.configured, false); assert.equal(bootstrap.model.fallback_configured, false); assert.equal(bootstrap.voice.configured, false)
    const guard = (await readFile(auditPath, 'utf8')).trim().split('\n').map(JSON.parse)
    assert(guard.some(e => e.event === 'isolation_ready' && resolve(e.profile) === join(state.temp, 'profile')))
    assert(guard.some(e => e.event === 'packaged_backend_spawn'))
    lastTick = performance.now(); await sample()
    await journey(); await journey()
    await save('renderer_reload')
    const origin = await cdp.evaluate('performance.timeOrigin'), beforeReload = await db()
    await cdp.send('Page.reload')
    await eventually(async () => await cdp.evaluate('performance.timeOrigin') !== origin && await healthy(), 'renderer reload recovery')
    assert.deepEqual((await db()).counts, beforeReload.counts); state.checks.renderer_reload = true
    await save('network_failure')
    await cdp.send('Network.emulateNetworkConditions', { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 })
    assert(await cdp.evaluate(`fetch(${JSON.stringify(runtime.localApiUrl + '/v1/health')}).then(()=>false,()=>true)`), 'Renderer network failure was not observed')
    await cdp.send('Page.reload')
    await eventually(() => cdp.evaluate(`document.body.innerText.includes('暂时无法进入')`), 'real UI network error state')
    await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 })
    await eventually(healthy, 'backend after network restoration')
    await eventually(() => cdp.evaluate(`(async()=>{const r=await window.desktop.getRuntimeConfig(); const response=await fetch(r.localApiUrl+'/v1/health',{headers:{'X-HSC-Session':r.sessionToken}}); if(!response.ok)throw new Error('health HTTP '+response.status);return true})()`), 'authenticated renderer network recovery')
    await eventually(() => cdp.evaluate(`Boolean(document.querySelector('.child-home-surface'))`), 'UI automatic recovery after network restoration', 45_000)
    state.checks.renderer_network_restored = true
    await save('device_denial')
    const denial = await cdp.evaluate(`navigator.mediaDevices.getUserMedia({audio:true}).then(s=>{s.getTracks().forEach(t=>t.stop());return 'unexpected_success'},e=>e.name)`)
    assert(['NotAllowedError', 'PermissionDeniedError'].includes(denial), `Expected real permission denial, got ${denial}`)
    assert(await healthy()); state.checks.permission_denial_without_backend_failure = true
    await save('backend_crash')
    const all = await processInfo(await windowsProcessTree(child.pid)), audit = (await readFile(auditPath, 'utf8')).trim().split('\n').map(JSON.parse)
    const pid = audit.filter(e => e.event === 'packaged_backend_spawn').at(-1)?.pid
    const victim = all.find(p => p.pid === pid)
    assert(victim && victim.exe.toLowerCase() === state.config['--exe'].toLowerCase(), 'Only the owned packaged backend can be killed')
    assert(sameProcess((await processInfo([victim.pid]))[0], victim), 'Backend PID identity changed')
    const beforeCrash = await db(), crashAt = performance.now()
    await exec('taskkill.exe', ['/PID', String(victim.pid), '/T', '/F'], { timeout: 15_000 })
    await eventually(async () => {
      const events = (await readFile(auditPath, 'utf8')).trim().split('\n').map(JSON.parse)
      const next = events.filter(e => e.event === 'packaged_backend_spawn').at(-1)?.pid
      return next && next !== victim.pid && await healthy()
    }, 'LocalServiceManager automatic packaged-backend recovery', 120_000)
    assert.deepEqual((await db()).counts, beforeCrash.counts)
    state.checks.backend_crash_automatic_recovery = true; state.backend_recovery_ms = Math.round(performance.now() - crashAt)
    lastSample = null; lastTick = performance.now(); await sample(); await journey()
    while (state.observed_seconds < state.target_seconds && !stopRequested) {
      await sleep(Math.min(60, state.target_seconds - state.observed_seconds) * 1000)
      if (stopRequested) break
      await sample()
      if (state.mode === 'soak' && Math.floor(state.observed_seconds / 1800) + 3 > state.journeys) { await journey(); await cdp.send('Page.reload'); await eventually(healthy, 'soak renderer reload') }
    }
    const finalDb = await db(); assert.equal(finalDb.counts.model_call, 0, 'Unexpected cloud call record')
    assert.equal(finalDb.counts.capture_artifact, 0, 'Permission-denied run unexpectedly captured media')
    const visual = await cdp.send('Page.captureScreenshot', { format: 'png' }); await writeFile(join(state.runDir, 'synthetic-final.png'), Buffer.from(visual.data, 'base64'))
    assert.equal(await fingerprint(state.config), state.build, 'Build changed during gate/soak')
    state.status = stopRequested ? 'interrupted' : state.failures.length ? 'completed_with_failures' : 'passed'; await save(state.status)
  } catch (error) {
    failure = error; state.status = 'failed'; state.error = error.message; state.failures.push({ at: new Date().toISOString(), stage: state.stage, error: error.message }); await save(state.stage)
    try { state.failure_ui = String(await cdp?.evaluate('document.body.innerText')).slice(0, 1800); const shot = await cdp.send('Page.captureScreenshot', { format: 'png' }); await writeFile(join(state.runDir, 'synthetic-failure.png'), Buffer.from(shot.data, 'base64')) } catch {}
  } finally {
    try { if (cdp) await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }) } catch {}
    cdp?.close()
    try {
      if (child?.pid) await stopIdentities([state.electron, ...(state.owned ?? [])])
      releaseChildProcessHandles(child); stopped = true; state.checks.owned_pid_tree_stopped = true
    } catch (error) { failure ??= error; state.status = 'failed'; state.cleanup_error = error.message }
    if (stopped && (state.mode === 'gate' || state.mode === 'recovery' && state.status === 'passed')) { try { await rm(state.temp, { recursive: true, force: true }); state.checks.temporary_profile_removed = true } catch (e) { failure ??= e; state.status = 'failed'; state.cleanup_error = e.message } }
    await save(state.stage); process.removeListener('SIGINT', signal); process.removeListener('SIGTERM', signal)
    console.log(JSON.stringify({ status: state.status, checkpoint, build: state.build, observed_seconds: state.observed_seconds, checks: state.checks, error: state.error, cleanup_error: state.cleanup_error }))
  }
  if (failure || state.status === 'completed_with_failures') process.exitCode = 1
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main(process.argv.slice(2)).catch(error => { console.error(error.message); process.exitCode = 1 })
