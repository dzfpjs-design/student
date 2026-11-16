#!/usr/bin/env node
/**
 * Real Electron IPC / disk / renderer / full-process restart acceptance.
 * Build the application first, then run (Node 22+, no runtime downloads):
 * node scripts/electron-voice-tutorial-e2e.mjs --electron-bin <unpackaged Electron>
 *   --python-bin <local-agent Python> --model-dir <existing read-only models>
 *   --output-dir <synthetic evidence directory> [--keep-profile] [--dry-run]
 *
 * Only disposable synthetic profiles are used. No ASR injection, recording,
 * microphone permission, camera permission, or natural-voice claim is made.
 * Setting tutorial steps through real IPC tests storage, NOT spoken practice.
 * Evidence records that distinction even when the renderer shows the tutorial.
 * The built main/preload/renderer are not modified or replaced with mocks.
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS, ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, releaseChildProcessHandles, stopWindowsProcessTree } from './electron-owned-process.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const usage = 'node scripts/electron-voice-tutorial-e2e.mjs --electron-bin <unpackaged Electron> --python-bin <Python> --model-dir <models> --output-dir <evidence> [--keep-profile] [--dry-run]'
const paths = ['--electron-bin', '--python-bin', '--model-dir', '--output-dir']
const steps = ['wake_practice', 'study_practice', 'document_practice', 'finish_practice', 'completed', 'deferred']
const sleep = (ms) => new Promise((done) => setTimeout(done, ms))
const setupPassword = `voice-tutorial-${process.pid}`
const setupUsername = 'synthetic-voice-tutorial'

function parseArgs(argv) {
  if (argv.includes('--help')) { console.log(usage); return null }
  const args = new Map()
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index]
    assert(!args.has(key), `Duplicate argument: ${key}`)
    if (key === '--keep-profile' || key === '--dry-run') args.set(key, true)
    else {
      assert(paths.includes(key) && argv[index + 1] && !argv[index + 1].startsWith('--'), `Invalid argument. ${usage}`)
      args.set(key, resolve(argv[++index]))
    }
  }
  for (const key of paths) assert(args.has(key), `${key} is required. ${usage}`)
  return args
}

async function eventually(read, label, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs
  let last
  while (Date.now() < deadline) {
    try { const result = await read(); if (result) return result } catch (error) { last = error }
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

function createSmtpSink() {
  const messages = []; let authSuccesses = 0
  const server = createServer((socket) => {
    socket.setEncoding('utf8'); socket.write('220 hsc-loopback ESMTP\r\n')
    let buffer = '', data = false, body = '', authState = '', authUser = ''
    const acceptCredentials = (user, password) => {
      if (user === setupUsername && password === setupPassword) { authSuccesses += 1; socket.write('235 2.7.0 authenticated\r\n') }
      else socket.write('535 5.7.8 invalid credentials\r\n')
    }
    socket.on('data', (chunk) => {
      buffer += chunk
      while (buffer.includes('\n')) {
        const index = buffer.indexOf('\n'); const line = buffer.slice(0, index + 1).replace(/\r?\n$/, ''); buffer = buffer.slice(index + 1)
        if (data) {
          if (line === '.') { messages.push(body); body = ''; data = false; socket.write('250 2.0.0 accepted\r\n') }
          else body += `${line}\n`
          continue
        }
        if (authState === 'plain') { const decoded = Buffer.from(line, 'base64').toString(); const parts = decoded.split('\0'); authState = ''; acceptCredentials(parts.at(-2) ?? '', parts.at(-1) ?? ''); continue }
        if (authState === 'login-user') { authUser = Buffer.from(line, 'base64').toString(); authState = 'login-pass'; socket.write('334 UGFzc3dvcmQ6\r\n'); continue }
        if (authState === 'login-pass') { authState = ''; acceptCredentials(authUser, Buffer.from(line, 'base64').toString()); continue }
        const [command, ...rest] = line.split(' '); const upper = command.toUpperCase()
        if (upper === 'EHLO' || upper === 'HELO') socket.write('250-hsc-loopback\r\n250 AUTH PLAIN LOGIN\r\n')
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN' && rest[1]) { const decoded = Buffer.from(rest[1], 'base64').toString(); const parts = decoded.split('\0'); acceptCredentials(parts.at(-2) ?? '', parts.at(-1) ?? '') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN') { authState = 'plain'; socket.write('334 \r\n') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'LOGIN') { authState = 'login-user'; socket.write('334 VXNlcm5hbWU6\r\n') }
        else if (upper === 'MAIL' || upper === 'RCPT' || upper === 'RSET' || upper === 'NOOP') socket.write('250 2.0.0 ok\r\n')
        else if (upper === 'DATA') { data = true; socket.write('354 end with <CRLF>.<CRLF>\r\n') }
        else if (upper === 'QUIT') { socket.write('221 2.0.0 bye\r\n'); socket.end() }
        else socket.write('250 2.0.0 ok\r\n')
      }
    })
  })
  return { server, messages, authSuccesses: () => authSuccesses }
}

async function setLabel(cdp, label, value) {
  await cdp.evaluate(`(() => { const node=document.querySelector('[aria-label=${JSON.stringify(label)}]'); if (!node) throw new Error('missing field: ${label}'); const proto=node instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto,'value').set.call(node,${JSON.stringify(value)}); node.dispatchEvent(new Event('input',{bubbles:true})); node.dispatchEvent(new Event('change',{bubbles:true})); })()`)
}

async function click(cdp, text) {
  await eventually(() => cdp.evaluate(`Array.from(document.querySelectorAll('button')).some((item) => !item.disabled && item.textContent?.includes(${JSON.stringify(text)}))`), `clickable: ${text}`)
  await cdp.evaluate(`Array.from(document.querySelectorAll('button')).find((item) => !item.disabled && item.textContent?.includes(${JSON.stringify(text)}))?.click()`)
}

async function completeInstallationIfRequired(cdp, smtp, smtpPort) {
  const text = await eventually(async () => {
    const current = await cdp.evaluate('document.body.innerText')
    const childSurface = await cdp.evaluate(`Boolean(document.querySelector('.child-home-surface'))`)
    return current.includes('先完成家长与日报配置') || childSurface ? current : null
  }, 'installation or child home surface', 70_000)
  if (!text.includes('先完成家长与日报配置')) return false
  const beforeMessages = smtp.messages.length, beforeAuth = smtp.authSuccesses()
  await setLabel(cdp, '初始化家长PIN', '2468'); await setLabel(cdp, '再次输入家长PIN', '2468'); await click(cdp, '保存 PIN')
  await eventually(() => cdp.evaluate(`document.body.innerText.includes('配置日报 SMTP')`), 'SMTP setup')
  await setLabel(cdp, '初始化SMTP主机', '127.0.0.1'); await setLabel(cdp, '初始化SMTP加密', 'none'); await setLabel(cdp, '初始化SMTP端口', String(smtpPort))
  await setLabel(cdp, '初始化SMTP用户名', setupUsername); await setLabel(cdp, '初始化SMTP密码', setupPassword); await setLabel(cdp, '初始化SMTP发件地址', 'tutorial@local.test'); await click(cdp, '保存 SMTP')
  await eventually(() => cdp.evaluate(`document.body.innerText.includes('设置每日报告收件人')`), 'recipient setup')
  await setLabel(cdp, '初始化收件邮箱', 'tutorial-parent@local.test'); await click(cdp, '保存收件人')
  await eventually(() => cdp.evaluate(`document.body.innerText.includes('发送固定测试邮件')`), 'delivery verification setup')
  await click(cdp, '发送并验证')
  await eventually(() => smtp.messages.length === beforeMessages + 2 && smtp.authSuccesses() === beforeAuth + 2, 'two-child installation SMTP verification', 30_000)
  await eventually(() => cdp.evaluate(`document.body.innerText.includes('今天，我们从哪里开始？')`), 'child home after setup', 70_000)
  return true
}

function isolatedEnvironment(profile, args) {
  const env = sanitizedHarnessEnvironment(process.env)
  for (const key of Object.keys(env)) {
    if (key.startsWith('HSC_') || /API_KEY|ACCESS_KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD|KEYCHAIN/i.test(key)
      || /^(NODE_OPTIONS|ELECTRON_RUN_AS_NODE|ELECTRON_RENDERER_URL|PYTHONPATH|PYTHONHOME|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)$/i.test(key)) delete env[key]
  }
  return {
    ...env, HSC_ELECTRON_USER_DATA_DIR: profile,
    HSC_DATA_DIR: join(profile, 'family-data'), HSC_DATABASE_PATH: join(profile, 'family-data', 'synthetic-tutorial.db'),
    HSC_LOCAL_AGENT_PYTHON: args.get('--python-bin'), HSC_LOCAL_MODEL_DIR: args.get('--model-dir'),
    PYTHONDONTWRITEBYTECODE: '1',
    HTTP_PROXY: 'http://127.0.0.1:9', HTTPS_PROXY: 'http://127.0.0.1:9', ALL_PROXY: 'http://127.0.0.1:9',
    NO_PROXY: '127.0.0.1,localhost,::1',
  }
}

// A temporary launcher pins isolation before importing the unchanged real app.
function guardedEntry(applicationRoot, profile, auditPath) {
  const { app, session } = require('electron')
  const { appendFileSync } = require('node:fs')
  const { join } = require('node:path')
  const { pathToFileURL } = require('node:url')
  const audit = (event, details = {}) => appendFileSync(auditPath, JSON.stringify({ event, ...details }) + '\n', { mode: 0o600 })
  if (app.isPackaged) throw new Error('Installed family applications are not allowed')
  app.setPath('userData', profile)
  app.getAppPath = () => applicationRoot
  app.on('web-contents-created', (_event, contents) => contents.setAudioMuted(true))
  app.once('ready', () => {
    const ses = session.defaultSession
    const check = ses.setPermissionCheckHandler.bind(ses)
    const request = ses.setPermissionRequestHandler.bind(ses)
    ses.setPermissionCheckHandler = () => check((_contents, permission) => { audit('permission_denied', { permission }); return false })
    ses.setPermissionRequestHandler = () => request((_contents, permission, callback) => { audit('permission_denied', { permission }); callback(false) })
    ses.setPermissionCheckHandler(); ses.setPermissionRequestHandler()
    ses.webRequest.onBeforeRequest((details, callback) => {
      const url = new URL(details.url)
      const network = ['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)
      const remote = network && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
      const media = network && /\/(capture|transcribe|synthesize|document-monitor\/start|observation\/start)(\/|$)/.test(url.pathname)
      if (remote || media) audit('request_blocked', { reason: remote ? 'non_loopback' : 'media' })
      callback({ cancel: remote || media })
    })
    audit('guard_installed', { profile: app.getPath('userData'), media_permissions_granted: 0 })
  })
  void import(pathToFileURL(join(applicationRoot, 'out/main/index.js')).href).catch(() => {
    audit('bootstrap_failed'); app.exit(1)
  })
}

class Cdp {
  constructor(url) { this.socket = new WebSocket(url); this.nextId = 0; this.pending = new Map() }
  async open() {
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data)), pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result)
    })
    this.socket.addEventListener('close', () => {
      for (const pending of this.pending.values()) pending.reject(new Error('CDP closed'))
      this.pending.clear()
    })
    await new Promise((done, reject) => {
      const timer = setTimeout(() => { this.close(); reject(new Error('CDP connection timed out')) }, CDP_COMMAND_TIMEOUT_MS)
      this.socket.addEventListener('open', () => { clearTimeout(timer); done() }, { once: true })
      this.socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('CDP connection failed')) }, { once: true })
    })
  }
  send(method, params = {}) {
    const id = ++this.nextId, response = awaitCdpResponse(this.pending, id, method)
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

async function launch(electronBin, wrapper, env) {
  const port = await freePort()
  const child = spawn(electronBin, [wrapper, `--remote-debugging-port=${port}`, '--remote-debugging-address=127.0.0.1'], {
    cwd: root, env, detached: process.platform !== 'win32', stdio: ['ignore', 'pipe', 'pipe'],
  })
  const owned = { child, port, cdp: null }
  child.stdout.resume(); child.stderr.resume()
  child.once('error', (error) => { owned.spawnError = error })
  return owned
}

async function connect(owned) {
  const page = await eventually(async () => {
    if (owned.spawnError) throw owned.spawnError
    assert(owned.child.exitCode === null, `Electron exited: ${owned.child.exitCode}`)
    const targets = await fetch(`http://127.0.0.1:${owned.port}/json/list`, { signal: AbortSignal.timeout(1000) }).then((r) => r.json())
    return targets.find((target) => target.type === 'page' && target.url.startsWith('file:') && target.webSocketDebuggerUrl)
  }, 'real Electron renderer', ELECTRON_RENDERER_STARTUP_TIMEOUT_MS)
  owned.cdp = new Cdp(page.webSocketDebuggerUrl); await owned.cdp.open()
  await owned.cdp.send('Page.enable'); await owned.cdp.send('Runtime.enable')
  owned.runtime = await eventually(async () => {
    const runtime = await owned.cdp.evaluate('window.desktop?.getRuntimeConfig()')
    if (runtime?.status === 'error') throw new Error(`Local runtime error: ${String(runtime.error ?? 'unknown').slice(0, 800)}`)
    return runtime?.status === 'ready' && runtime
  }, 'local runtime', ELECTRON_RENDERER_STARTUP_TIMEOUT_MS)
  const url = new URL(owned.runtime.localApiUrl)
  assert(url.protocol === 'http:' && url.hostname === '127.0.0.1', 'Only isolated loopback API is allowed')
  assert(await owned.cdp.evaluate('typeof window.desktop.getVoiceOnboarding === "function" && typeof window.desktop.saveVoiceOnboarding === "function"'),
    'Built preload is missing tutorial IPC; rebuild after main integration, do not substitute mocks')
}

async function stop(owned) {
  if (!owned) return
  owned.cdp?.close()
  if (owned.child.pid) {
    if (process.platform === 'win32') await stopWindowsProcessTree(owned.child, eventually)
    else {
      const signal = (name) => { try { process.kill(-owned.child.pid, name) } catch (error) { if (error.code !== 'ESRCH') throw error } }
      const gone = () => { try { process.kill(-owned.child.pid, 0); return false } catch (error) { if (error.code === 'ESRCH') return true; throw error } }
      signal('SIGTERM')
      if (!await eventually(gone, 'owned process group shutdown', 7000).catch(() => false)) signal('SIGKILL')
      await eventually(gone, 'owned process group exit', 4000)
      await eventually(() => owned.child.exitCode !== null || owned.child.signalCode !== null, 'Electron exit', 4000)
    }
    for (const url of [`http://127.0.0.1:${owned.port}/json/list`, ...(owned.runtime ? [owned.runtime.localApiUrl + '/health'] : [])]) {
      await eventually(async () => {
        try { await fetch(url, { signal: AbortSignal.timeout(300) }); return false } catch { return true }
      }, 'owned service port closed', 4000)
    }
  }
  releaseChildProcessHandles(owned.child)
}

const get = (cdp, childId) => cdp.evaluate(`window.desktop.getVoiceOnboarding(${JSON.stringify(childId)})`)
const save = (cdp, value) => cdp.evaluate(`window.desktop.saveVoiceOnboarding(${JSON.stringify(value)})`)
const progress = (childId, step) => ({ childId, version: 1, step, updatedAt: '2026-09-05T00:00:00.000Z' })

async function render(owned, runDir, filename) {
  await eventually(() => owned.cdp.evaluate('Boolean(document.querySelector(".child-home-surface"))'), 'real child home rendering')
  await screenshot(owned, runDir, filename)
}

async function screenshot(owned, runDir, filename) {
  await owned.cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false })
  const image = await owned.cdp.send('Page.captureScreenshot', { format: 'png' })
  await writeFile(join(runDir, filename), Buffer.from(image.data, 'base64'), { mode: 0o600 })
}

async function reload(owned) {
  const previous = await owned.cdp.evaluate('performance.timeOrigin')
  await owned.cdp.send('Page.reload', { ignoreCache: true })
  await eventually(async () => {
    const current = await owned.cdp.evaluate('performance.timeOrigin')
    return typeof current === 'number' && current !== previous
  }, 'new renderer document')
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (!args) return
  if (args.get('--dry-run')) {
    console.log(JSON.stringify({ mode: 'dry_run', launches_electron: false, checks: ['real_ipc', 'per_child_files', 'renderer_reload', 'full_process_restart', 'fresh_profile'], natural_voice_tested: false }))
    return
  }
  assert.equal(typeof WebSocket, 'function', 'Node 22+ with built-in WebSocket is required')
  for (const key of paths.filter((key) => key !== '--output-dir')) assert(existsSync(args.get(key)), `Missing ${key}`)
  const electronBin = args.get('--electron-bin')
  assert([join(dirname(electronBin), 'resources/default_app.asar'), join(dirname(electronBin), '../Resources/default_app.asar')].some(existsSync), 'Use unpackaged Electron, never the installed family app')
  const bundles = ['out/main/index.js', 'out/preload/index.js', 'out/renderer/index.html']
  for (const path of bundles) assert(existsSync(join(root, path)), 'Build the application before acceptance')
  const rendererScripts = (await readdir(join(root, 'out/renderer/assets'))).filter((name) => name.endsWith('.js')).map((name) => `out/renderer/assets/${name}`)
  bundles.push(...rendererScripts)
  let tutorialAvailable = false
  for (const path of rendererScripts) {
    const code = await readFile(join(root, path), 'utf8')
    if (code.includes('document_practice') && code.includes('这次不会打开高拍仪')) tutorialAvailable = true
  }
  await mkdir(args.get('--output-dir'), { recursive: true })
  const runDir = await mkdtemp(join(args.get('--output-dir'), 'voice-tutorial-'))
  const temp = await mkdtemp(join(tmpdir(), 'hsc-voice-tutorial-e2e-'))
  const smtp = createSmtpSink()
  await new Promise((done, reject) => smtp.server.once('error', reject).listen(0, '127.0.0.1', done))
  const smtpPort = smtp.server.address().port
  const report = {
    status: 'running', platform: process.platform, synthetic_data_only: true,
    natural_voice_tested: false, real_microphone_tested: false, simulated_asr_used: false, raw_audio_stored: false,
    tutorial_voice_path: { status: 'not_exercised', reason: 'Steps are written through real IPC, not inferred from speech. Device permissions and cloud synthesis requests are blocked; renderer audio is muted. Local OS speech may be attempted; there is no acoustic assessment.' },
    tutorial_render_restore: { status: 'not_available_in_build' },
    checks: {}, screenshots: [], build_sha256: {},
  }
  for (const path of bundles) report.build_sha256[path] = createHash('sha256').update(await readFile(join(root, path))).digest('hex')
  let owned, failure
  const profile = join(temp, 'profile'), freshProfile = join(temp, 'fresh-profile')
  const boot = async (directory, sequence, activeSessionId = null) => {
    await mkdir(directory, { recursive: true })
    const wrapper = join(temp, `launcher-${sequence}`), auditPath = join(runDir, `isolation-${sequence}.jsonl`)
    await mkdir(wrapper)
    await writeFile(join(wrapper, 'package.json'), JSON.stringify({ name: 'hsc-synthetic-voice-tutorial', version: '1.0.0', main: 'main.cjs' }), { mode: 0o600 })
    await writeFile(join(wrapper, 'main.cjs'), `(${guardedEntry.toString()})(${JSON.stringify(root)},${JSON.stringify(directory)},${JSON.stringify(auditPath)});`, { mode: 0o600 })
    owned = await launch(electronBin, wrapper, isolatedEnvironment(directory, args))
    await connect(owned)
    const audit = (await readFile(auditPath, 'utf8')).trim().split('\n').map(JSON.parse)
    assert(audit.some((entry) => entry.event === 'guard_installed' && entry.profile === directory && entry.media_permissions_granted === 0))
    const bootstrap = await fetch(owned.runtime.localApiUrl + '/v1/bootstrap', {
      headers: { 'X-HSC-Session': owned.runtime.sessionToken }, signal: AbortSignal.timeout(20_000),
    }).then((response) => { assert(response.ok, 'Bootstrap must succeed'); return response.json() })
    assert.equal(bootstrap.model.configured, false)
    assert.equal(bootstrap.model.fallback_configured, false)
    assert.equal(bootstrap.voice.configured, false)
    assert.equal(bootstrap.active_session?.session_id ?? null, activeSessionId)
    assert.equal(bootstrap.children.length, 2, 'Only the two defaults of a new synthetic database are expected')
    const initialized = await completeInstallationIfRequired(owned.cdp, smtp, smtpPort)
    if (initialized) report.checks.installation_setup_runs = (report.checks.installation_setup_runs ?? 0) + 1
    return bootstrap.children.map((child) => child.child_id)
  }
  try {
    const children = await boot(profile, 1)
    const [a, b] = children
    assert.notEqual(a, b)
    assert.deepEqual(await Promise.all(children.map((id) => get(owned.cdp, id))), [null, null])
    report.checks.initial_isolation = true
    await render(owned, runDir, '01-synthetic-home.png'); report.screenshots.push('01-synthetic-home.png')
    for (const step of steps) {
      const value = progress(a, step)
      assert.deepEqual(await save(owned.cdp, value), value)
      assert.deepEqual(await get(owned.cdp, a), value)
      assert.equal(await get(owned.cdp, b), null)
    }
    report.checks.all_six_steps_real_ipc = true
    const expected = [progress(a, 'document_practice'), progress(b, 'deferred')]
    assert.deepEqual(await Promise.all(expected.map((value) => save(owned.cdp, value))), expected)
    for (const invalid of [{ ...expected[0], childId: '../escape' }, { ...expected[0], step: 'invented' }, { ...expected[0], version: 2 }]) {
      await assert.rejects(() => save(owned.cdp, invalid))
    }
    assert.deepEqual(await Promise.all(children.map((id) => get(owned.cdp, id))), expected)
    const disk = async () => {
      const directory = join(profile, 'voice-onboarding')
      assert.deepEqual((await readdir(directory)).sort(), children.map((id) => `${encodeURIComponent(id)}.json`).sort(), 'Only independent child JSON files; no leftover temporary files')
      for (const value of expected) assert.deepEqual(JSON.parse(await readFile(join(directory, `${encodeURIComponent(value.childId)}.json`), 'utf8')), value)
    }
    await disk(); report.checks.independent_files_and_invalid_write_rejection = true
    await reload(owned)
    await eventually(async () => {
      assert.deepEqual(await Promise.all(children.map((id) => get(owned.cdp, id))), expected)
      return true
    }, 'IPC progress after renderer reload')
    await render(owned, runDir, '02-reloaded-home.png'); report.screenshots.push('02-reloaded-home.png')
    report.checks.renderer_reload = true
    const previousPid = owned.child.pid
    await stop(owned); owned = null
    assert.deepEqual(await boot(profile, 2), children)
    assert.notEqual(owned.child.pid, previousPid)
    assert.deepEqual(await Promise.all(children.map((id) => get(owned.cdp, id))), expected)
    await disk()
    await render(owned, runDir, '03-restarted-home.png'); report.screenshots.push('03-restarted-home.png')
    report.checks.full_electron_restart_and_disk_recovery = true
    await stop(owned); owned = null
    assert.deepEqual(await boot(freshProfile, 3), children)
    assert.deepEqual(await Promise.all(children.map((id) => get(owned.cdp, id))), [null, null])
    await disk(); report.checks.fresh_profile_does_not_read_previous_profile = true
    if (tutorialAvailable) {
      report.tutorial_render_restore = { status: 'running', setup_boundary: 'Real wake-word/progress IPC and synthetic session HTTP setup; no ASR or spoken practice.' }
      const wake = { childId: a, wakeWord: '小知', inactivitySeconds: 60, enabled: true, updatedAt: expected[0].updatedAt }
      await owned.cdp.evaluate(`window.desktop.saveWakeWordSettings(${JSON.stringify(wake)})`)
      await Promise.all(expected.map((value) => save(owned.cdp, value)))
      const response = await fetch(owned.runtime.localApiUrl + '/v1/sessions', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-HSC-Session': owned.runtime.sessionToken },
        body: JSON.stringify({ child_id: a, client_action_id: 'synthetic-tutorial-render-only' }), signal: AbortSignal.timeout(20_000),
      })
      assert(response.ok, 'Synthetic session setup must succeed')
      const session = await response.json()
      const restoredPrompt = async () => {
        await eventually(() => owned.cdp.evaluate(`(() => {
          const panel = document.querySelector('[aria-label="语音学习伙伴"]');
          return Boolean(panel && panel.getBoundingClientRect().height > 0 && panel.innerText.includes('这次不会打开高拍仪'));
        })()`), 'persisted document-practice prompt in real tutorial UI')
        assert.deepEqual(await Promise.all(children.map((id) => get(owned.cdp, id))), expected)
      }
      await reload(owned); await restoredPrompt()
      await screenshot(owned, runDir, '04-restored-tutorial.png'); report.screenshots.push('04-restored-tutorial.png')
      await stop(owned); owned = null
      assert.deepEqual(await boot(freshProfile, 4, session.session_id), children)
      await restoredPrompt()
      await screenshot(owned, runDir, '05-restarted-tutorial.png'); report.screenshots.push('05-restarted-tutorial.png')
      report.tutorial_render_restore.status = 'passed_after_reload_and_process_restart'
      report.checks.tutorial_prompt_restored_without_advancing_either_child = true
    }
    for (const path of bundles) assert.equal(createHash('sha256').update(await readFile(join(root, path))).digest('hex'), report.build_sha256[path], 'Build changed during acceptance; rerun against frozen output')
    report.status = 'passed_ipc_render_persistence_only'
  } catch (error) {
    failure = error; report.status = 'failed'; report.error = error.message
    try { report.failure_ui_text = String(await owned?.cdp?.evaluate('document.body.innerText')).slice(0, 2000) } catch {}
  } finally {
    try { await stop(owned); report.checks.owned_processes_stopped = true } catch (error) {
      failure ??= error; report.status = 'failed'; report.cleanup_error = error.message
    }
    if (args.get('--keep-profile') || !report.checks.owned_processes_stopped) report.retained_synthetic_profile_root = temp
    else {
      try { await rm(temp, { recursive: true, force: true }); report.checks.disposable_profiles_removed = true } catch (error) {
        failure ??= error; report.status = 'failed'; report.cleanup_error = error.message
      }
    }
    await new Promise((done) => smtp.server.close(() => done()))
    await writeFile(join(runDir, 'evidence.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 })
    console.log(JSON.stringify({ status: report.status, evidence: join(runDir, 'evidence.json'), checks: report.checks }))
  }
  if (failure) throw failure
}

main().catch((error) => { console.error(`[electron-voice-tutorial-e2e] ${error.message}`); process.exitCode = 1 })
