#!/usr/bin/env node
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { resolve, join } from 'node:path'
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS, ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, releaseChildProcessHandles } from './electron-owned-process.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const electronBin = join(root, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron')
const pythonBin = join(root, '.venv', 'bin', 'python')
const modelDir = join(root, 'services', 'local_agent', 'models')
const outputDir = join(root, 'docs', 'evidence', 'child-voice-surface')
const interactiveSelector = 'button,input,textarea,select,[role="button"],[contenteditable="true"]'

const sleep = (milliseconds) => new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds))

async function freePort() {
  const server = createServer()
  await new Promise((resolvePromise, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolvePromise))
  const port = server.address().port
  await new Promise((resolvePromise, reject) => server.close((error) => error ? reject(error) : resolvePromise()))
  return port
}

async function eventually(read, label, timeoutMs = 25_000) {
  const started = Date.now()
  let lastError
  while (Date.now() - started < timeoutMs) {
    try { return await read() } catch (error) { lastError = error; await sleep(150) }
  }
  throw new Error(`${label} timed out: ${lastError instanceof Error ? lastError.message : String(lastError)}`)
}

class Cdp {
  constructor(url) { this.socket = new WebSocket(url); this.nextId = 0; this.waiting = new Map() }
  async open() {
    await new Promise((resolvePromise, reject) => {
      this.socket.addEventListener('open', resolvePromise, { once: true })
      this.socket.addEventListener('error', reject, { once: true })
    })
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data))
      const pending = this.waiting.get(message.id)
      if (!pending) return
      this.waiting.delete(message.id)
      if (message.error) pending.reject(new Error(message.error.message)); else pending.resolve(message.result)
    })
  }
  send(method, params = {}) {
    const id = ++this.nextId
    const response = awaitCdpResponse(this.waiting, id, method, CDP_COMMAND_TIMEOUT_MS)
    this.socket.send(JSON.stringify({ id, method, params }))
    return response
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text ?? 'CDP evaluation failed')
    return result.result.value
  }
  close() { this.socket.close() }
}

async function screenshot(cdp, path) {
  const result = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  await writeFile(path, Buffer.from(result.data, 'base64'), { mode: 0o600 })
}

async function stop(child, cdp) {
  cdp?.close()
  if (!child?.pid || child.exitCode !== null) return
  try { process.kill(-child.pid, 'SIGTERM') } catch { child.kill('SIGTERM') }
  await eventually(() => child.exitCode !== null || Promise.reject(new Error('still running')), 'Electron exit', 7_000).catch(() => {
    try { process.kill(-child.pid, 'SIGKILL') } catch { child.kill('SIGKILL') }
  })
  releaseChildProcessHandles(child)
}

async function main() {
  for (const path of [electronBin, pythonBin, modelDir, join(root, 'out', 'main', 'index.js')]) {
    if (!existsSync(path)) throw new Error(`Required runtime input is missing: ${path}`)
  }
  await mkdir(outputDir, { recursive: true })
  const profileDir = await mkdtemp(join(tmpdir(), 'hsc-child-voice-surface-'))
  const port = await freePort()
  let child
  let cdp
  try {
    child = spawn(electronBin, [root, `--remote-debugging-port=${port}`, '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'], {
      cwd: root,
      detached: true,
      env: {
        ...sanitizedHarnessEnvironment(process.env),
        HSC_ELECTRON_USER_DATA_DIR: profileDir,
        HSC_LOCAL_AGENT_PYTHON: pythonBin,
        HSC_LOCAL_MODEL_DIR: modelDir,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const stderr = []
    child.stderr.on('data', (chunk) => stderr.push(String(chunk)))
    const page = await eventually(async () => {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      const target = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl)
      if (!target) throw new Error('renderer target missing')
      return target
    }, 'Electron renderer', ELECTRON_RENDERER_STARTUP_TIMEOUT_MS)
    cdp = new Cdp(page.webSocketDebuggerUrl)
    await cdp.open()
    await cdp.send('Page.enable')
    await cdp.send('Runtime.enable')

    const runtime = await eventually(() => cdp.evaluate('window.desktop?.getRuntimeConfig()'), 'local runtime')
    if (runtime?.status !== 'ready') throw new Error(`local runtime is ${runtime?.status ?? 'missing'}: ${stderr.join('').slice(-500)}`)
    await eventually(async () => {
      const ready = await cdp.evaluate(`Boolean(document.querySelector('.child-home-surface .household-standby-voice'))`)
      if (!ready) throw new Error('child home not rendered')
      return true
    }, 'child home')

    const home = await cdp.evaluate(`(() => {
      const visible = (element) => { const style = getComputedStyle(element); const rect = element.getBoundingClientRect(); return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0 }
      return Array.from(document.querySelectorAll(${JSON.stringify(interactiveSelector)})).filter(visible).map((element) => ({ tag: element.tagName, text: element.textContent?.trim() || element.getAttribute('aria-label') || '' }))
    })()`)
    if (home.length !== 1 || !home[0].text.includes('家长中心')) throw new Error(`child home exposes unexpected controls: ${JSON.stringify(home)}`)
    await screenshot(cdp, join(outputDir, 'home.png'))

    const setup = await cdp.evaluate(`(async () => {
      const runtime = await window.desktop.getRuntimeConfig();
      const headers = { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken };
      const bootstrap = await fetch(runtime.localApiUrl + '/v1/bootstrap', { headers }).then((response) => response.json());
      const child = bootstrap.children[0];
      if (!child) throw new Error('default child profile missing');
      const response = await fetch(runtime.localApiUrl + '/v1/sessions', { method: 'POST', headers, body: JSON.stringify({ child_id: child.child_id, client_action_id: 'voice-surface-e2e-session' }) });
      if (!response.ok) throw new Error('session start:' + response.status);
      return response.json();
    })()`)
    if (!setup?.session_id) throw new Error('session was not created')
    await cdp.evaluate('window.location.reload()')
    await eventually(async () => {
      const ready = await cdp.evaluate(`Boolean(document.querySelector('.child-voice-only[data-interaction-mode="voice-only"]'))`)
      if (!ready) throw new Error('voice-only session not rendered')
      return true
    }, 'voice-only session')

    const session = await cdp.evaluate(`(() => {
      const surface = document.querySelector('.child-voice-only');
      const visible = (element) => { const style = getComputedStyle(element); const rect = element.getBoundingClientRect(); return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0 }
      return {
        visibleControls: Array.from(surface.querySelectorAll(${JSON.stringify(interactiveSelector)})).filter(visible).map((element) => element.textContent?.trim() || element.getAttribute('aria-label') || element.tagName),
        mountedControls: surface.querySelectorAll(${JSON.stringify(interactiveSelector)}).length,
        handsFreeMounted: Boolean(surface.querySelector('.hands-free-control')),
        agentVisible: Boolean(surface.querySelector('[aria-label="语音学习伙伴"]')),
      };
    })()`)
    if (session.visibleControls.length !== 0) throw new Error(`session exposes child controls: ${JSON.stringify(session.visibleControls)}`)
    if (!session.handsFreeMounted || !session.agentVisible || session.mountedControls === 0) throw new Error(`voice runtime was unmounted: ${JSON.stringify(session)}`)
    const probeId = randomUUID()
    const receipt = await cdp.evaluate(`window.desktop.recordRendererDiagnostic({ event: 'e2e_redaction_probe', correlationId: ${JSON.stringify(probeId)}, details: { apiKey: 'sk-synthetic-05f2095cdc7ffa9c4784', rawAudio: 'must-not-be-recorded' } })`)
    if (receipt?.recorded !== true) throw new Error('diagnostic probe was not accepted by the owned renderer')
    const diagnostics = await cdp.evaluate('window.desktop.getDiagnosticSummary()')
    const diagnosticLog = await eventually(async () => {
      const content = await readFile(diagnostics.logPath, 'utf8')
      const events = content.split('\n').filter(Boolean).map(line => JSON.parse(line))
      const probe = events.find(event => event.correlation_id === probeId && event.source === 'renderer')
      if (!probe || !events.some(event => event.event === 'local_api_request')) throw new Error('diagnostic events not flushed yet')
      if (probe.event !== 'unregistered_event' || probe.privacy_policy_version !== 'diagnostic_metadata_v2') throw new Error('diagnostic event-name privacy boundary changed')
      return content
    }, 'diagnostic log')
    if (diagnosticLog.includes('e2e_redaction_probe') || diagnosticLog.includes('sk-synthetic-05f2095cdc7ffa9c4784') || diagnosticLog.includes('must-not-be-recorded')) throw new Error('diagnostic log exposed an unregistered event, credential or raw media field')
    if (diagnostics.rawMediaRecorded !== false || diagnostics.uploadEnabled !== false) throw new Error(`diagnostic privacy boundary changed: ${JSON.stringify(diagnostics)}`)
    await screenshot(cdp, join(outputDir, 'session.png'))
    await writeFile(join(outputDir, 'result.json'), JSON.stringify({ home, session, diagnostics: { currentLogBytes: diagnostics.currentLogBytes, rotatedLogCount: diagnostics.rotatedLogCount, rawMediaRecorded: diagnostics.rawMediaRecorded, uploadEnabled: diagnostics.uploadEnabled }, screenshots: ['home.png', 'session.png'] }, null, 2), { mode: 0o600 })
    process.stdout.write(JSON.stringify({ status: 'passed', home_visible_controls: home.length, session_visible_controls: session.visibleControls.length, session_mounted_controls: session.mountedControls, diagnostics_verified: true, output_dir: outputDir }) + '\n')
  } finally {
    await stop(child, cdp)
    await rm(profileDir, { recursive: true, force: true })
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
