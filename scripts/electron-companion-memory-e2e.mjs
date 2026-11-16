#!/usr/bin/env node
/** Real local API + parent UI acceptance, using synthetic data and a disposable profile. */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS, ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, releaseChildProcessHandles, stopWindowsProcessTree } from './electron-owned-process.mjs'
import { completeSyntheticInstallation, createInstallationSmtpSink, startInstallationSmtpSink, stopInstallationSmtpSink } from './electron-installation-setup-harness.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const usage = 'node scripts/electron-companion-memory-e2e.mjs --electron-bin <unpackaged Electron executable> --python-bin <local-agent Python> --output-dir <evidence directory> --model-dir <existing model directory>'
const sleep = (ms) => new Promise((done) => setTimeout(done, ms))
const panel = '[aria-label="孩子长期记忆"]'
const syntheticPin = '2468'
const syntheticSource = '记住我喜欢画图讲解'
const expectedSourceEvidence = '我喜欢画图讲解'

function parseArgs(values) {
  if (values.includes('--help')) { console.log(usage); return null }
  const keys = ['--electron-bin', '--python-bin', '--output-dir', '--model-dir']
  const options = new Map()
  for (let index = 0; index < values.length; index += 2) {
    assert(keys.includes(values[index]) && values[index + 1] && !values[index + 1].startsWith('--'), `Invalid argument. ${usage}`)
    assert(!options.has(values[index]), `Duplicate argument: ${values[index]}`)
    options.set(values[index], resolve(values[index + 1]))
  }
  for (const key of keys) assert(options.has(key), `${key} is required. ${usage}`)
  return options
}

async function eventually(read, label, timeoutMs = 25_000) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    try { const value = await read(); if (value) return value } catch (error) { last = error }
    await sleep(150)
  }
  throw new Error(`${label} timed out${last ? `: ${last.message}` : ''}`)
}

async function freePort() {
  const server = createServer()
  await new Promise((done, reject) => server.once('error', reject).listen(0, '127.0.0.1', done))
  const port = server.address().port
  await new Promise((done, reject) => server.close((error) => error ? reject(error) : done()))
  return port
}

function isolatedEnvironment(profile, pythonBin, modelDir) {
  const env = sanitizedHarnessEnvironment(process.env)
  for (const key of Object.keys(env)) {
    if (key.startsWith('HSC_') || /API_KEY|ACCESS_KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD|KEYCHAIN/i.test(key)) delete env[key]
    if (/^(?:NODE_OPTIONS|ELECTRON_RUN_AS_NODE|ELECTRON_RENDERER_URL|PYTHONPATH|PYTHONHOME|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)$/i.test(key)) delete env[key]
  }
  return {
    ...env,
    HSC_ELECTRON_USER_DATA_DIR: profile,
    HSC_DATA_DIR: join(profile, 'family-data'),
    HSC_DATABASE_PATH: join(profile, 'family-data', 'synthetic-memory.db'),
    HSC_LOCAL_AGENT_PYTHON: pythonBin,
    HSC_LOCAL_MODEL_DIR: modelDir,
    // No remote service is needed; an inherited proxy must not supply one.
    HTTP_PROXY: 'http://127.0.0.1:9', HTTPS_PROXY: 'http://127.0.0.1:9', ALL_PROXY: 'http://127.0.0.1:9',
    NO_PROXY: '127.0.0.1,localhost,::1',
  }
}

// Serialized into a temporary Electron entry point; the frozen application
// bundle remains unchanged. Permissions are pinned before its first renderer.
function guardedElectronEntry(applicationRoot, profile, auditPath) {
  const { app, session } = require('electron')
  const { appendFileSync } = require('node:fs')
  const { pathToFileURL } = require('node:url')
  const { join } = require('node:path')
  const audit = (event, details = {}) => appendFileSync(auditPath, JSON.stringify({ event, ...details }) + '\n', { mode: 0o600 })
  if (app.isPackaged) throw new Error('Only unpackaged Electron is accepted')
  app.setPath('userData', profile)
  // The wrapper package lives in temp; service/model resolution still uses the real repo.
  app.getAppPath = () => applicationRoot
  app.on('web-contents-created', (_event, contents) => contents.setAudioMuted(true))
  app.once('ready', () => {
    const ses = session.defaultSession
    const installCheck = ses.setPermissionCheckHandler.bind(ses)
    const installRequest = ses.setPermissionRequestHandler.bind(ses)
    const denyCheck = (_contents, permission) => { audit('permission_check_denied', { permission }); return false }
    const denyRequest = (_contents, permission, callback) => { audit('permission_request_denied', { permission }); callback(false) }
    // The app installs its own handlers during startup. Keep this harness restriction sticky.
    ses.setPermissionCheckHandler = () => installCheck(denyCheck)
    ses.setPermissionRequestHandler = () => installRequest(denyRequest)
    ses.setPermissionCheckHandler()
    ses.setPermissionRequestHandler()
    ses.webRequest.onBeforeRequest((details, callback) => {
      const url = new URL(details.url)
      const network = ['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)
      const remote = network && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      const media = network && /\/(?:capture|transcribe|synthesize|document-monitor\/start|observation\/start)(?:\/|$)/.test(url.pathname)
      if (remote || media) audit('request_blocked', { reason: remote ? 'non_loopback' : 'media', path: url.pathname })
      callback({ cancel: remote || media })
    })
    audit('guard_installed', { profile: app.getPath('userData'), media_permissions_granted: 0, renderer_audio_muted: true })
  })
  void import(pathToFileURL(join(applicationRoot, 'out', 'main', 'index.js')).href).catch((error) => {
    audit('bootstrap_failed', { message: error.message })
    app.exit(1)
  })
}

class Cdp {
  constructor(url) { this.socket = new WebSocket(url); this.nextId = 0; this.pending = new Map(); this.responses = [] }
  async open() {
    await new Promise((done, reject) => {
      this.socket.addEventListener('open', done, { once: true })
      this.socket.addEventListener('error', reject, { once: true })
    })
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data))
      const pending = this.pending.get(message.id)
      if (pending) {
        this.pending.delete(message.id)
        message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result)
      } else if (message.method === 'Network.responseReceived') {
        this.responses.push(message.params)
        if (this.responses.length > 1000) this.responses.shift()
      }
    })
    this.socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) pending.reject(new Error('CDP closed'))
      this.pending.clear()
    })
  }
  send(method, params = {}) {
    const id = ++this.nextId
    const response = awaitCdpResponse(this.pending, id, method, CDP_COMMAND_TIMEOUT_MS)
    this.socket.send(JSON.stringify({ id, method, params }))
    return response
  }
  async evaluate(expression) {
    const response = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description ?? response.exceptionDetails.text)
    return response.result.value
  }
  close() { this.socket.close() }
}

async function startElectron(electronBin, wrapper, env, runDir, sequence) {
  const port = await freePort()
  const child = spawn(electronBin, [wrapper, `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1'], {
    cwd: root, env, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'],
  })
  const app = { child, port, cdp: null, logs: '', sequence, runDir }
  child.stdout.on('data', (chunk) => { app.logs = (app.logs + String(chunk)).slice(-80_000) })
  child.stderr.on('data', (chunk) => { app.logs = (app.logs + String(chunk)).slice(-80_000) })
  child.once('error', (error) => { app.spawnError = error })
  return app
}

async function connectElectron(app) {
  const page = await eventually(async () => {
    if (app.spawnError) throw app.spawnError
    if (app.child.exitCode !== null) throw new Error(`Electron exited ${app.child.exitCode}: ${app.logs.slice(-1200)}`)
    const targets = await fetch(`http://127.0.0.1:${app.port}/json/list`, { signal: AbortSignal.timeout(1_000) }).then((response) => response.json())
    return targets.find((target) => target.type === 'page' && target.webSocketDebuggerUrl)
  }, 'Electron renderer', ELECTRON_RENDERER_STARTUP_TIMEOUT_MS)
  app.cdp = new Cdp(page.webSocketDebuggerUrl)
  await app.cdp.open()
  for (const method of ['Page.enable', 'Runtime.enable', 'Network.enable']) await app.cdp.send(method)
  await app.cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false })
  const runtime = await eventually(async () => {
    const value = await app.cdp.evaluate('window.desktop?.getRuntimeConfig()')
    return value?.status === 'ready' && value
  }, 'isolated local API')
  const url = new URL(runtime.localApiUrl)
  assert(url.protocol === 'http:' && url.hostname === '127.0.0.1', 'Runtime must use IPv4 loopback')
  app.runtime = runtime
}

async function stopElectron(app) {
  if (!app) return
  app.cdp?.close()
  if (app.child.pid) {
    if (process.platform === 'win32') await stopWindowsProcessTree(app.child, eventually)
    else {
      try { process.kill(-app.child.pid, 'SIGTERM') } catch (error) { if (error.code !== 'ESRCH') throw error }
      if (!await eventually(() => app.child.exitCode !== null, 'Electron shutdown', 7_000).catch(() => false)) {
        try { process.kill(-app.child.pid, 'SIGKILL') } catch (error) { if (error.code !== 'ESRCH') throw error }
      }
      await eventually(() => app.child.exitCode !== null, 'Electron final exit', 4_000)
      await eventually(() => {
        try { process.kill(-app.child.pid, 0); return false } catch (error) { if (error.code === 'ESRCH') return true; throw error }
      }, 'owned process group exit', 4_000)
    }
    await eventually(async () => {
      try { await fetch(`http://127.0.0.1:${app.port}/json/list`, { signal: AbortSignal.timeout(300) }); return false } catch { return true }
    }, 'CDP port closed', 4_000)
    if (app.runtime) await eventually(async () => {
      try { await fetch(`${app.runtime.localApiUrl}/health`, { signal: AbortSignal.timeout(300) }); return false } catch { return true }
    }, 'owned backend port closed', 4_000)
  }
  releaseChildProcessHandles(app.child)
}

async function api(app, path, { method = 'GET', body, parentToken, expectedStatus } = {}) {
  assert(path.startsWith('/v1/'), 'Only local application API paths are allowed')
  const response = await fetch(app.runtime.localApiUrl + path, {
    method, headers: { 'Content-Type': 'application/json', 'X-HSC-Session': app.runtime.sessionToken, ...(parentToken ? { 'X-HSC-Parent': parentToken } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(30_000),
  })
  if (expectedStatus !== undefined) { assert.equal(response.status, expectedStatus, `${method} ${path}`); return null }
  assert(response.ok, `${method} ${path}: HTTP ${response.status}`)
  return response.json()
}

async function click(cdp, selector, text) {
  await eventually(() => cdp.evaluate(`(() => {
    const candidates = Array.from(document.querySelectorAll(${JSON.stringify(selector)})).filter(el => !el.disabled && el.getBoundingClientRect().height > 0);
    const node = candidates.find(el => ${text === undefined ? 'true' : `(el.textContent || '').trim() === ${JSON.stringify(text)}`});
    if (!node) return false; node.scrollIntoView({block:'center'}); node.click(); return true;
  })()`), `click ${text ?? selector}`)
}

async function waitText(cdp, selector, text) {
  return eventually(() => cdp.evaluate(`Boolean(document.querySelector(${JSON.stringify(selector)})?.innerText.includes(${JSON.stringify(text)}))`), `visible ${text}`)
}

async function openAdvancedSettings(cdp) {
  await eventually(() => cdp.evaluate(`Boolean(document.querySelector('.parent-advanced-workspace'))`), 'advanced settings')
  await cdp.evaluate(`(() => { const details=document.querySelector('.parent-advanced-workspace'); if (!details.open) details.querySelector('summary')?.click(); return details.open })()`)
  await eventually(() => cdp.evaluate(`document.querySelector(${JSON.stringify(panel)})?.getBoundingClientRect().height > 0`), 'visible memory panel')
}

async function screenshot(cdp, runDir, name, selector) {
  if (selector) await cdp.evaluate(`document.querySelector(${JSON.stringify(selector)})?.scrollIntoView({block:'center'})`)
  await cdp.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
  const image = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await writeFile(join(runDir, name), Buffer.from(image.data, 'base64'), { mode: 0o600 })
}

async function unlockParent(app, first, runDir) {
  const cdp = app.cdp
  await eventually(() => cdp.evaluate(`Boolean(document.querySelector('.child-home-surface'))`), 'home parent entry')
  await click(cdp, 'button', '家长中心')
  await eventually(() => cdp.evaluate(`Boolean(document.querySelector('input[aria-label="家长 PIN"]'))`), 'parent PIN input')
  await screenshot(cdp, runDir, '01-parent-pin.png', '.parent-unlock')
  await cdp.evaluate(`(() => {
    const input = document.querySelector('input[aria-label="家长 PIN"]');
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(syntheticPin)});
    input.dispatchEvent(new Event('input', {bubbles:true})); input.dispatchEvent(new Event('change', {bubbles:true}));
  })()`)
  const start = cdp.responses.length
  await click(cdp, 'button', first ? '设置并进入' : '解锁')
  await eventually(() => cdp.evaluate(`Boolean(document.querySelector(${JSON.stringify(panel)}))`), 'unlocked memory panel')
  const response = await eventually(() => cdp.responses.slice(start).find((item) => item.response.url.endsWith(first ? '/v1/parent/setup' : '/v1/parent/unlock')), 'UI PIN authentication response')
  assert.equal(response.response.status, 200, 'PIN must be authenticated by the real backend')
  const raw = await cdp.send('Network.getResponseBody', { requestId: response.requestId })
  const parsed = JSON.parse(raw.base64Encoded ? Buffer.from(raw.body, 'base64').toString('utf8') : raw.body)
  assert(parsed.parent_token, 'UI unlock must return a parent token')
  return parsed.parent_token
}

async function inspectVoices(cdp) {
  return cdp.evaluate(`(async () => {
    if (!window.speechSynthesis) return {supported:false, total_count:0, local_count:0, local_chinese_count:0, voices:[], evidence:'enumeration_only_not_acoustic'};
    const synth = window.speechSynthesis;
    const started = Date.now();
    while (!synth.getVoices().length && Date.now() - started < 5000) await new Promise(resolve => setTimeout(resolve, 200));
    const voices = synth.getVoices().map(v => ({name:v.name, lang:v.lang, local_service:v.localService === true}));
    return {supported:true, total_count:voices.length, local_count:voices.filter(v=>v.local_service).length,
      local_chinese_count:voices.filter(v=>v.local_service && v.lang.toLowerCase().startsWith('zh')).length,
      voices, evidence:'enumeration_only_not_acoustic', speech_audition_performed:false};
  })()`)
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (!args) return
  assert.equal(typeof WebSocket, 'function', 'Node.js with a built-in WebSocket (Node 22+) is required')
  const electronBin = args.get('--electron-bin'), pythonBin = args.get('--python-bin'), modelDir = args.get('--model-dir')
  for (const path of [electronBin, pythonBin, modelDir, join(root, 'out/main/index.js'), join(root, 'out/renderer/index.html')]) assert(existsSync(path), `Missing runtime input: ${path}; build the frozen app before running`)
  assert([join(dirname(electronBin), 'resources/default_app.asar'), join(dirname(electronBin), '../Resources/default_app.asar')].some(existsSync), 'Use the unpackaged Electron executable, never the installed family application')
  await mkdir(args.get('--output-dir'), { recursive: true })
  const runDir = await mkdtemp(join(args.get('--output-dir'), 'memory-'))
  const temp = await mkdtemp(join(tmpdir(), 'hsc-memory-e2e-'))
  const profile = join(temp, 'profile'), wrapper = join(temp, 'wrapper'), guardLog = join(runDir, 'isolation.jsonl')
  const setup = { username: 'synthetic-memory', password: `memory-${process.pid}`, recipient: 'memory-parent@local.test' }
  const smtp = createInstallationSmtpSink(setup)
  const smtpPort = await startInstallationSmtpSink(smtp)
  await mkdir(profile); await mkdir(wrapper)
  await writeFile(join(wrapper, 'package.json'), JSON.stringify({ name: 'hsc-synthetic-memory-acceptance', version: '1.0.0', main: 'main.cjs' }), { mode: 0o600 })
  await writeFile(join(wrapper, 'main.cjs'), `(${guardedElectronEntry.toString()})(${JSON.stringify(root)},${JSON.stringify(profile)},${JSON.stringify(guardLog)});`, { mode: 0o600 })
  const report = {
    status: 'running', synthetic_data_only: true, platform: process.platform, node: process.version,
    started_at: new Date().toISOString(), output_dir: runDir,
    build_main_sha256: createHash('sha256').update(await readFile(join(root, 'out/main/index.js'))).digest('hex'),
    checks: {}, screenshots: [],
    limits: ['Synthetic API/UI acceptance, not real-child learning evidence.', 'Voice inventory is not acoustic, intelligibility, or human voice-quality evidence.', 'Harness denies device permissions; it does not test real microphone/camera operation.'],
  }
  let app, failure
  try {
    const env = isolatedEnvironment(profile, pythonBin, modelDir)
    app = await startElectron(electronBin, wrapper, env, runDir, 1)
    await connectElectron(app)
    const guard = (await readFile(guardLog, 'utf8')).trim().split('\n').map((line) => JSON.parse(line)).find((entry) => entry.event === 'guard_installed')
    assert.equal(guard?.profile, profile, 'The real application must use the disposable profile')
    const bootstrap = await api(app, '/v1/bootstrap')
    assert.equal(bootstrap.model.configured, false, 'No cloud model may be configured')
    assert.equal(bootstrap.model.fallback_configured, false, 'No fallback cloud model may be configured')
    assert.equal(bootstrap.voice.configured, false, 'No cloud TTS may be configured')
    assert.equal(bootstrap.active_session, null, 'Profile must start without a family session')
    report.checks.installation_setup = {
      completed: await completeSyntheticInstallation({ cdp: app.cdp, sink: smtp, port: smtpPort, ...setup }),
      accepted_test_messages: smtp.messages.length,
    }
    // Let App consume its initial empty-session bootstrap before seeding. A
    // concurrent initial bootstrap could otherwise navigate away from the PIN UI.
    await eventually(() => app.cdp.evaluate(`Boolean(document.querySelector('.child-home-surface'))`), 'initial empty-session home')
    await app.cdp.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    const child = bootstrap.children[0]
    assert(child?.child_id, 'Synthetic default child missing')
    const session = await api(app, '/v1/sessions', { method: 'POST', body: { child_id: child.child_id, client_action_id: randomUUID() } })
    const turn = await api(app, `/v1/sessions/${session.session_id}/agent-turn`, { method: 'POST', body: { transcript: syntheticSource, client_action_id: randomUUID() } })
    assert.equal(turn.intent, 'memory_command')
    assert.equal(turn.action.local_only, true)
    assert.equal(turn.action.cloud_transmission_allowed, false)
    await api(app, `/v1/agent-turns/${turn.agent_turn_id}/outcome`, { method: 'PUT', body: { final_response: turn.response_text, tool_status: 'completed' } })
    // Close this empty synthetic session through legal API transitions so reload
    // tests return to the parent entry, not a voice-only active learning screen.
    const stages = ['check_in', 'classroom_recall', 'homework_capture', 'plan_review', 'study', 'reflection', 'evolving', 'completed']
    for (let index = 0; index < stages.length - 1; index += 1) {
      if (stages[index] === 'classroom_recall') await api(app, `/v1/sessions/${session.session_id}/recalls/none`, { method: 'POST', body: { client_action_id: randomUUID() } })
      if (stages[index] === 'homework_capture') await api(app, `/v1/sessions/${session.session_id}/homework/none`, { method: 'POST', body: { client_action_id: randomUUID() } })
      await api(app, `/v1/sessions/${session.session_id}/transition`, { method: 'POST', body: {
        expected_state: stages[index], target_state: stages[index + 1],
        reason: 'Synthetic memory acceptance only; no learning assessment.', client_action_id: randomUUID(),
      } })
    }
    assert.equal((await api(app, '/v1/bootstrap')).active_session, null)
    report.checks.synthetic_session_closed_via_api = { passed: true, learning_outcomes_claimed: false }
    const memoryPath = `/v1/parent/children/${child.child_id}/companion-memory`
    await api(app, memoryPath, { expectedStatus: 401 })
    const parentToken = await unlockParent(app, false, runDir)
    await openAdvancedSettings(app.cdp)
    assert.equal((await api(app, `/v1/parent/children/${child.child_id}/model-calls`, { parentToken })).items.length, 0, 'Memory preparation must not call a cloud model')
    report.screenshots.push('01-parent-pin.png')
    const listing = await api(app, memoryPath, { parentToken })
    assert.equal(listing.cloud_transmission_allowed, false)
    assert.equal(listing.items.length, 1, 'Exactly one synthetic memory is expected')
    const memory = listing.items[0]
    assert.equal(memory.source_turn_id, turn.agent_turn_id)
    // The API exposes the extracted evidence clause, not the command prefix.
    assert.equal(memory.source_text, expectedSourceEvidence, 'Memory must retain the synthetic evidence clause')
    await waitText(app.cdp, panel, memory.content)
    await click(app.cdp, `${panel} article summary`, '来源详情')
    await waitText(app.cdp, `${panel} article details[open]`, memory.source_text)
    await screenshot(app.cdp, runDir, '02-memory-source.png', panel)
    report.screenshots.push('02-memory-source.png')
    report.checks.creation_and_source = { passed: true, child_id: child.child_id, memory_id: memory.memory_id, source_turn_id: turn.agent_turn_id, submitted_transcript: syntheticSource, source_evidence: memory.source_text, parent_authentication: 'UI_PIN_real_API', unauthenticated_read_status: 401 }
    report.voice_inventory = await inspectVoices(app.cdp)
    report.voice_inventory.local_only_tts_readiness = report.voice_inventory.local_chinese_count > 0 ? 'local_chinese_voice_enumerated_not_auditioned' : 'no_verified_local_chinese_voice_text_only_required'
    await click(app.cdp, `${panel} button`, '忘记这条记忆')
    await waitText(app.cdp, panel, '确认忘记这条记忆？')
    await screenshot(app.cdp, runDir, '03-confirm-forget.png', panel)
    report.screenshots.push('03-confirm-forget.png')
    await click(app.cdp, `${panel} button`, '确认忘记')
    await waitText(app.cdp, panel, '已忘记这条记忆。')
    assert.equal((await api(app, memoryPath, { parentToken })).items.length, 0, 'UI deletion must reach the durable API')
    await click(app.cdp, `${panel} button[aria-label="刷新记忆"]`)
    await waitText(app.cdp, panel, '这个孩子还没有长期记忆。')
    await screenshot(app.cdp, runDir, '04-after-refresh.png', panel)
    report.screenshots.push('04-after-refresh.png')
    report.checks.ui_delete_and_refresh = { passed: true, active_memories: 0 }

    await app.cdp.send('Page.reload', { ignoreCache: true })
    const reloadedToken = await unlockParent(app, false, runDir)
    await openAdvancedSettings(app.cdp)
    await waitText(app.cdp, panel, '这个孩子还没有长期记忆。')
    assert.equal((await api(app, memoryPath, { parentToken: reloadedToken })).items.length, 0)
    await screenshot(app.cdp, runDir, '05-after-page-reload.png', panel)
    report.screenshots.push('05-after-page-reload.png')
    report.checks.page_reload = { passed: true, parent_reauthenticated: true, active_memories: 0 }

    await stopElectron(app); app = null
    app = await startElectron(electronBin, wrapper, env, runDir, 2)
    await connectElectron(app)
    const restartedToken = await unlockParent(app, false, runDir)
    await openAdvancedSettings(app.cdp)
    await waitText(app.cdp, panel, '这个孩子还没有长期记忆。')
    assert.equal((await api(app, memoryPath, { parentToken: restartedToken })).items.length, 0)
    const workspace = await api(app, `/v1/sessions/${session.session_id}`)
    assert.equal(workspace.sensor_release.active_leases, 0)
    assert.equal(workspace.sensor_release.opened_leases, 0)
    await screenshot(app.cdp, runDir, '06-after-app-backend-restart.png', panel)
    report.screenshots.push('06-after-app-backend-restart.png')
    report.checks.app_backend_restart = { passed: true, parent_reauthenticated: true, active_memories: 0, opened_device_leases: 0 }
    report.status = 'passed'
  } catch (error) {
    failure = error
    report.status = 'failed'
    report.error = error.message
    if (app?.cdp) {
      report.failure_ui_text = await app.cdp.evaluate('document.body.innerText').catch(() => null)
      report.failure_memory_responses = app.cdp.responses.filter((item) => item.response.url.includes('/companion-memory')).map((item) => ({ url: item.response.url, status: item.response.status }))
      await screenshot(app.cdp, runDir, 'failure.png').then(() => report.screenshots.push('failure.png')).catch(() => {})
    }
  } finally {
    try {
      const cleanup = await Promise.allSettled([stopElectron(app), stopInstallationSmtpSink(smtp)])
      const rejected = cleanup.find((item) => item.status === 'rejected')
      if (rejected) throw rejected.reason
      report.checks.owned_process_cleanup = { passed: true }
      await rm(temp, { recursive: true, force: true })
    } catch (error) {
      report.status = 'failed'; report.cleanup_error = error.message
      report.retained_synthetic_profile = profile
      failure ??= error
    }
    const audit = await readFile(guardLog, 'utf8').then((value) => value.trim().split('\n').filter(Boolean).map((line) => JSON.parse(line))).catch(() => [])
    report.isolation = { isolated_profile_verified: audit.some((entry) => entry.event === 'guard_installed' && entry.profile === profile),
      cloud_credentials_configured: false, media_permissions_granted: 0,
      denied_permission_checks: audit.filter((entry) => entry.event === 'permission_check_denied').length,
      denied_permission_requests: audit.filter((entry) => entry.event === 'permission_request_denied').length,
      blocked_network_requests: audit.filter((entry) => entry.event === 'request_blocked').length,
      real_family_profile_accessed: false, synthetic_profile_removed: !report.cleanup_error,
    }
    report.finished_at = new Date().toISOString()
    await writeFile(join(runDir, 'result.json'), JSON.stringify(report, null, 2), { mode: 0o600 })
    console.log(JSON.stringify({ status: report.status, report: join(runDir, 'result.json'), local_chinese_voice_count: report.voice_inventory?.local_chinese_count, error: report.error ?? report.cleanup_error }))
  }
  if (failure) process.exitCode = 1
}

main().catch((error) => { console.error(error.message); process.exitCode = 1 })
