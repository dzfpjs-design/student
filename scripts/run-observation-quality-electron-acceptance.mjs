#!/usr/bin/env node
/*
 * Development-only visible acceptance.  This intentionally starts the real
 * Electron renderer and local agent, uses CDP to inject a synthetic camera,
 * and clicks the rendered pause/resume and parent controls.  It is not a
 * TestClient substitute and it never opens a physical camera or saves frames.
 */
import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { DatabaseSync } from 'node:sqlite'
import { ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, runWindowsCommand, stopWindowsProcessTree, windowsRemainingPids } from './electron-owned-process.mjs'
import { completeSyntheticInstallation, createInstallationSmtpSink, startInstallationSmtpSink, stopInstallationSmtpSink } from './electron-installation-setup-harness.mjs'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const sourceNodeModules = process.env.HSC_NODE_MODULES_DIR ?? join(root, 'node_modules')
const sourceRuntimeRoot = process.env.HSC_SOURCE_RUNTIME_ROOT ?? resolve(sourceNodeModules, '..')
const python = process.env.HSC_PYTHON_BIN ?? join(sourceRuntimeRoot, '.venv', 'bin', 'python')
const electronBin = process.env.HSC_ELECTRON_BIN ?? join(sourceNodeModules, 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron')
const outputDir = process.argv.includes('--output-dir')
  ? resolve(process.argv[process.argv.indexOf('--output-dir') + 1])
  : resolve(process.cwd(), 'artifacts', 'observation-quality-electron')
const physicalCamera = process.argv.includes('--physical-camera')
const documentCameraUnavailable = process.argv.includes('--document-camera-unavailable')
const physicalCameraLabel = process.argv.includes('--physical-camera-label')
  ? process.argv[process.argv.indexOf('--physical-camera-label') + 1]
  : (process.env.HSC_PHYSICAL_CAMERA_LABEL ?? '')
const localAgentHealthTimeoutMs = ELECTRON_RENDERER_STARTUP_TIMEOUT_MS
let temporaryRoot = ''
let nodeModulesLinkCreated = false
let agent = null
let electron = null
let activeCdp = null
let installationSmtp = null
const logHandles = []

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

async function freePort() {
  const server = createServer()
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
  const address = server.address()
  const port = typeof address === 'object' && address ? address.port : 0
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  return port
}

function processGroupStop(child) {
  if (!child?.pid || child.exitCode !== null) return
  try { process.kill(-child.pid, 'SIGTERM') } catch { try { child.kill('SIGTERM') } catch {} }
}

async function stopOwnedProcess(child, label) {
  if (!child?.pid) return { confirmed: true, tracked_pids: [], tracked_survivors: [] }
  if (process.platform === 'win32') {
    const trackedPids = await stopWindowsProcessTree(child, waitFor, runWindowsCommand)
    const survivors = await windowsRemainingPids(trackedPids, runWindowsCommand)
    if (survivors.length) throw new Error(`owned Windows ${label} process tree survived cleanup: ${survivors.join(', ')}`)
    return { confirmed: true, tracked_pids: trackedPids, tracked_survivors: survivors }
  }
  const stopped = () => {
    try { process.kill(child.pid, 0); return false } catch (error) { return error?.code === 'ESRCH' }
  }
  if (stopped()) return { confirmed: true, tracked_pids: [child.pid], tracked_survivors: [] }
  processGroupStop(child)
  const exited = await waitFor(stopped, `owned ${label} process did not exit after SIGTERM`, 5_000).catch(() => false)
  if (exited) return { confirmed: true, tracked_pids: [child.pid], tracked_survivors: [] }
  try { process.kill(-child.pid, 'SIGKILL') } catch { try { child.kill('SIGKILL') } catch {} }
  const confirmed = await waitFor(stopped, `owned ${label} process did not exit after SIGKILL`, 2_000).catch(() => false)
  return { confirmed, tracked_pids: [child.pid], tracked_survivors: confirmed ? [] : [child.pid] }
}

async function waitFor(check, description, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs
  let lastError = null
  while (Date.now() < deadline) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) { lastError = error }
    await sleep(150)
  }
  throw new Error(`${description}${lastError ? `: ${String(lastError)}` : ''}`)
}

async function request(baseUrl, path, { method = 'GET', body, headers = {} } = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    method,
    headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...headers },
    ...(body ? { body: JSON.stringify(body) } : {}),
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(`${method} ${path} failed (${response.status}): ${payload.message ?? JSON.stringify(payload)}`)
  return payload
}

function action(prefix) { return `${prefix}-${randomUUID()}` }

async function prepareStudySession(baseUrl, camera = {
  deviceId: 'hsc-synthetic-camera-0001', label: 'Synthetic Observation Camera',
  capabilities: { transport: 'synthetic_cdp', width: 320, height: 240, frame_rate: 10 },
}) {
  const parentStatus = await request(baseUrl, '/v1/parent/status')
  const parent = await request(baseUrl, parentStatus.configured ? '/v1/parent/unlock' : '/v1/parent/setup', { method: 'POST', body: { pin: '2468' } })
  const parentHeaders = { 'X-HSC-Parent': parent.parent_token }
  const deviceHash = createHash('sha256').update(camera.deviceId).digest('hex')
  await request(baseUrl, '/v1/parent/device-bindings', { method: 'PUT', headers: parentHeaders, body: {
    client_action_id: action('electron-visible-binding'),
    bindings: [{ device_type: camera.deviceType ?? 'camera', system_id_hash: deviceHash, label: camera.label, verified_at: new Date().toISOString(), capabilities: camera.capabilities }],
  } })
  await request(baseUrl, '/v1/parent/children/child-primary-4/family-rule', { method: 'PUT', headers: parentHeaders, body: {
    latest_end_time: null, max_learning_minutes: 90, task_segment_minutes: 25, break_minutes: 5,
    pet_min_minutes: 0, competition_minutes: 0, observation_mode: 'gentle',
    reminder_budget_per_10m: 1, misclassification_shadow_threshold: 2,
  } })
  const created = await request(baseUrl, '/v1/sessions', { method: 'POST', body: { child_id: 'child-primary-4', client_action_id: action('electron-visible-session') } })
  const sessionId = created.session_id
  await request(baseUrl, `/v1/sessions/${sessionId}/transition`, { method: 'POST', body: { expected_state: 'check_in', target_state: 'classroom_recall', reason: 'synthetic Electron acceptance', client_action_id: action('electron-visible-transition') } })
  await request(baseUrl, `/v1/sessions/${sessionId}/recalls/none`, { method: 'POST', body: { client_action_id: action('electron-visible-recall-none') } })
  await request(baseUrl, `/v1/sessions/${sessionId}/transition`, { method: 'POST', body: { expected_state: 'classroom_recall', target_state: 'homework_capture', reason: 'synthetic Electron acceptance', client_action_id: action('electron-visible-transition') } })
  await request(baseUrl, `/v1/sessions/${sessionId}/homework`, { method: 'POST', body: { subject: '数学', title: '合成观察验收', expected_minutes: 10, client_action_id: action('electron-visible-homework') } })
  const plan = await request(baseUrl, `/v1/sessions/${sessionId}/plan/generate`, { method: 'POST', body: { client_action_id: action('electron-visible-plan') } })
  await request(baseUrl, `/v1/sessions/${sessionId}/transition`, { method: 'POST', body: { expected_state: 'plan_review', target_state: 'study', reason: 'synthetic Electron acceptance', client_action_id: action('electron-visible-transition') } })
  await request(baseUrl, `/v1/plan-items/${plan.plan_items[0].plan_item_id}/start`, { method: 'POST', body: { client_action_id: action('electron-visible-task') } })
  return { sessionId, parentHeaders }
}

class Cdp {
  constructor(url) { this.url = url; this.sequence = 0; this.pending = new Map(); this.socket = null }
  async connect() {
    this.socket = new WebSocket(this.url)
    await new Promise((resolve, reject) => { this.socket.addEventListener('open', resolve, { once: true }); this.socket.addEventListener('error', reject, { once: true }) })
    this.socket.addEventListener('message', (event) => {
      const message = JSON.parse(String(event.data)); const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id); message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result)
    })
  }
  send(method, params = {}) {
    const id = ++this.sequence
    return new Promise((resolve, reject) => { this.pending.set(id, { resolve, reject }); this.socket.send(JSON.stringify({ id, method, params })) })
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text ?? 'renderer evaluation failed')
    return result.result.value
  }
  close() { this.socket?.close() }
}

// Same synthetic grayscale buffer drives renderer health and the backend JPEG
// boundary (PGM is a local OpenCV-decodable image despite the canvas MIME tag).
const syntheticCameraInjection = `(() => {
  const width = 320, height = 240;
  window.__hscObservationSynthetic = { mode: 'dark', stops: 0 };
  const pixels = () => {
    const bright = window.__hscObservationSynthetic.mode !== 'dark';
    const out = new Uint8Array(width * height); out.fill(bright ? 150 : 8);
    if (!bright) return out;
    const set = (x, y, value) => { if (x >= 0 && x < width && y >= 0 && y < height) out[y * width + x] = value; };
    if (window.__hscObservationSynthetic.mode !== 'absent') {
      for (let y = 30; y < 210; y += 1) for (let x = 70; x < 250; x += 1) {
        const dx = (x - 160) / 70, dy = (y - 120) / 90; if (dx * dx + dy * dy <= 1) set(x, y, 205);
      }
      for (const cx of [135, 185]) for (let y = 84; y < 106; y += 1) for (let x = cx - 11; x < cx + 11; x += 1) {
        if ((x - cx) ** 2 + (y - 95) ** 2 <= 100) set(x, y, 25);
      }
      for (let x = 125; x < 196; x += 1) for (let y = 145; y < 170; y += 1) {
        const dx = (x - 160) / 35, dy = (y - 150) / 15; if (dx * dx + dy * dy > .7 && dx * dx + dy * dy < 1.2 && y >= 150) set(x, y, 35);
      }
    } else {
      // A textured, normally exposed room without a face.  This must pass the
      // renderer's frame-health check so the backend, not the harness, decides
      // that the child has not yet established a person baseline.
      for (let y = 0; y < height; y += 1) for (let x = 0; x < width; x += 1) {
        const checker = (Math.floor(x / 20) + Math.floor(y / 20)) % 2;
        set(x, y, checker ? 175 : 75);
      }
    }
    return out;
  };
  const toBase64 = (bytes) => { let output = ''; for (let index = 0; index < bytes.length; index += 8192) output += String.fromCharCode(...bytes.subarray(index, index + 8192)); return btoa(output); };
  const pgm = () => { const header = new TextEncoder().encode('P5\\n' + width + ' ' + height + '\\n255\\n'); const image = pixels(); const all = new Uint8Array(header.length + image.length); all.set(header); all.set(image, header.length); return 'data:image/jpeg;base64,' + toBase64(all); };
  const rgba = () => { const source = pixels(); const output = new Uint8ClampedArray(width * height * 4); for (let index = 0; index < source.length; index += 1) { output[index * 4] = source[index]; output[index * 4 + 1] = source[index]; output[index * 4 + 2] = source[index]; output[index * 4 + 3] = 255; } return new ImageData(output, width, height); };
  const proto = HTMLCanvasElement.prototype;
  proto.toDataURL = function () { return pgm(); };
  const contextProto = CanvasRenderingContext2D.prototype;
  contextProto.drawImage = function () {};
  contextProto.getImageData = function () { return rgba(); };
  Object.defineProperties(HTMLVideoElement.prototype, {
    readyState: { configurable: true, get: () => HTMLMediaElement.HAVE_CURRENT_DATA },
    videoWidth: { configurable: true, get: () => width }, videoHeight: { configurable: true, get: () => height },
  });
  Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', { configurable: true, get() { return this.__hscSyntheticStream ?? null }, set(value) { this.__hscSyntheticStream = value; this.style.background = window.__hscObservationSynthetic.mode === 'bright' ? 'radial-gradient(circle at 50% 45%, #cfc2af 0 30%, #969696 31% 100%)' : '#080808'; } });
  Object.defineProperty(HTMLMediaElement.prototype, 'currentTime', {
    configurable: true,
    get() { return this.__hscPlayStartedAt ? (performance.now() - this.__hscPlayStartedAt) / 1_000 : 0; },
    set() {},
  });
  HTMLMediaElement.prototype.play = async function () {
    // Real camera elements emit a usable-frame event after play() while the
    // stream remains attached.  Reproduce that lifecycle so the product's
    // 12-second no-frame watchdog is exercised instead of bypassed.
    this.__hscPlayStartedAt = performance.now();
    for (const delay of [100, 400, 1_000]) setTimeout(() => {
      if (!this.__hscSyntheticStream) return;
      this.dispatchEvent(new Event('loadeddata'));
      this.dispatchEvent(new Event('timeupdate'));
    }, delay);
    return undefined;
  };
  const device = { kind: 'videoinput', deviceId: 'hsc-synthetic-camera-0001', groupId: 'hsc-synthetic-group', label: 'Synthetic Observation Camera' };
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
    enumerateDevices: async () => [device], addEventListener() {}, removeEventListener() {},
    getUserMedia: async () => { const track = { label: device.label, readyState: 'live', getSettings: () => ({ deviceId: device.deviceId, groupId: device.groupId, width, height, frameRate: 10 }), getCapabilities: () => ({}), addEventListener() {}, stop() { window.__hscObservationSynthetic.stops += 1; this.readyState = 'ended'; } }; return { getVideoTracks: () => [track], getTracks: () => [track] }; },
  } });
})()`

async function discoverPhysicalCamera(cdp) {
  // This is deliberately an in-memory permission probe in the isolated Electron
  // profile. It never serializes a frame, and it stops the probe track before
  // the product surface opens the same selected device.
  return cdp.evaluate(`(async () => {
    const permissionProbe = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const candidates = devices.filter((item) => item.kind === 'videoinput');
      const requestedLabel = ${JSON.stringify(physicalCameraLabel)}.trim().toLocaleLowerCase();
      const device = requestedLabel
        ? candidates.find((item) => item.label.toLocaleLowerCase() === requestedLabel || item.label.toLocaleLowerCase().includes(requestedLabel))
        : candidates[0];
      if (!device?.deviceId || !device.label) throw new Error('The selected camera is unavailable or its label is still permission-unknown.');
      const selectedStream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: device.deviceId }, width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 10, max: 15 } }, audio: false });
      try {
        const settings = selectedStream.getVideoTracks()[0]?.getSettings?.() ?? {};
        return { deviceId: device.deviceId, label: device.label, capabilities: { transport: 'electron_getUserMedia', width: settings.width ?? null, height: settings.height ?? null, frame_rate: settings.frameRate ?? null } };
      } finally { selectedStream.getTracks().forEach((track) => track.stop()); }
    } finally { permissionProbe.getTracks().forEach((track) => track.stop()); }
  })()`)
}

async function openParentHealth(cdp) {
  await cdp.evaluate("document.querySelector('button[title=\"返回首页\"]')?.click()")
  await waitFor(async () => (await cdp.evaluate("Array.from(document.querySelectorAll('button')).some((button) => button.textContent?.includes('家长中心'))")), 'home page did not render')
  await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('家长中心'))?.click()")
  await waitFor(async () => (await cdp.evaluate("Boolean(document.querySelector('input[aria-label=\"家长 PIN\"]'))")), 'parent PIN surface did not render')
  await cdp.evaluate("(async () => { const input = document.querySelector('input[aria-label=\"家长 PIN\"]'); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; setter.call(input, '2468'); input.dispatchEvent(new Event('input', { bubbles: true })); await new Promise((resolve) => setTimeout(resolve, 100)); Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('解锁'))?.click(); })()")
  await waitFor(async () => cdp.evaluate("Boolean(document.querySelector('.parent-advanced-workspace'))"), 'parent advanced workspace did not render')
  await cdp.evaluate("(() => { const details = document.querySelector('.parent-advanced-workspace'); if (details && !details.open) details.querySelector('summary')?.click(); return Boolean(details?.open); })()")
  await waitFor(async () => (await cdp.evaluate("document.body.innerText.includes('最近24小时观察有效性')")), 'parent health surface did not render')
}

async function restartIsolatedAgent(apiUrl, dataDir) {
  await stopOwnedProcess(agent, 'local Agent')
  const port = new URL(apiUrl).port
  const restartLog = join(outputDir, 'local-agent-restart.log')
  const restartOut = await (await import('node:fs/promises')).open(restartLog, 'w')
  logHandles.push(restartOut)
  agent = spawn(python, ['-m', 'home_study_agent', '--host', '127.0.0.1', '--port', port], {
    cwd: root,
    detached: true,
    stdio: ['ignore', restartOut.fd, restartOut.fd],
    env: { ...process.env, PYTHONPATH: 'services/local_agent/src', HSC_DATA_DIR: dataDir, HSC_DATABASE_PATH: join(dataDir, 'agent.db'), HSC_MAINTENANCE_ENABLED: '0' },
  })
  await waitFor(async () => (await fetch(`${apiUrl}/v1/health`)).ok, 'restarted local agent did not start', localAgentHealthTimeoutMs)
}

async function enterOrResumeStudy(cdp, readyExpression, description) {
  const state = await waitFor(async () => cdp.evaluate(`(() => {
    const resume = Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('继续学习'));
    if (resume) return 'resume';
    return (${readyExpression}) ? 'ready' : false;
  })()`), description)
  if (state === 'resume') {
    await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('继续学习'))?.click()")
  }
  await waitFor(async () => cdp.evaluate(`Boolean(${readyExpression})`), description)
}

async function runPhysicalCameraAcceptance({ cdp, apiUrl, sessionId, dataDir }) {
  const databasePath = join(dataDir, 'agent.db')
  await enterOrResumeStudy(cdp, "document.querySelector('[aria-label=\"本地观察状态\"]')", 'child observation surface did not render')
  await waitFor(async () => {
    const text = await cdp.evaluate('document.body.innerText')
    if (!text.includes('通过前只做本机取景确认')) throw new Error(text.slice(0, 1_500))
    return true
  }, 'physical camera did not expose a truthful qualification state', 15_000)
  const beforePause = await stableCounts(databasePath, sessionId)
  if (beforePause.stateEvents !== 0 || beforePause.longitudinal !== 0) throw new Error(`unqualified physical camera persisted behavior evidence: ${JSON.stringify(beforePause)}`)
  const beforePauseQualification = await request(apiUrl, `/v1/sessions/${sessionId}/observation/qualification`)
  if (beforePauseQualification.status !== 'unqualified') throw new Error(`physical camera did not reach an unqualified diagnostic state: ${JSON.stringify(beforePauseQualification)}`)
  // Keep the evidence screenshot free of camera pixels while preserving the
  // actual renderer's diagnosis and retry/pause controls.
  await cdp.evaluate("document.querySelector('[aria-label=\"本地观察状态\"] video')?.style.setProperty('visibility', 'hidden')")
  await capture(cdp, join(outputDir, '00-physical-camera-unqualified.png'))
  await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.title === '暂停本地观察')?.click()")
  await waitFor(async () => (await cdp.evaluate("document.body.innerText.includes('本地观察已暂停')")), 'visible physical-camera pause did not complete')
  await waitFor(async () => (await stableCounts(databasePath, sessionId)).activeLeases === 0, 'physical-camera pause left an active lease')
  const qualification = await request(apiUrl, `/v1/sessions/${sessionId}/observation/qualification`)
  if (qualification.status !== 'paused') throw new Error(`physical-camera qualification did not persist paused: ${JSON.stringify(qualification)}`)
  // Screenshot only after pausing: the renderer has detached the video stream,
  // so this UI evidence contains no camera frame.
  await capture(cdp, join(outputDir, '01-physical-camera-paused.png'))
  await restartIsolatedAgent(apiUrl, dataDir)
  const afterAgentRestart = await request(apiUrl, `/v1/sessions/${sessionId}/observation/qualification`)
  if (afterAgentRestart.status !== 'paused') throw new Error(`physical-camera pause was not durable across local-agent restart: ${JSON.stringify(afterAgentRestart)}`)
  await cdp.send('Page.reload', { ignoreCache: true })
  await enterOrResumeStudy(cdp, "document.body.innerText.includes('观察已按上次选择保持暂停')", 'paused observation surface did not render after refresh')
  await waitFor(async () => (await cdp.evaluate("document.body.innerText.includes('观察已按上次选择保持暂停')")), 'paused qualification did not remain truthful after renderer refresh')
  await openParentHealth(cdp)
  await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('四年级同学'))?.click()")
  await cdp.evaluate("document.querySelector('.operations-health')?.scrollIntoView({ block: 'center' })")
  await sleep(250)
  const parentHealthText = await cdp.evaluate('document.querySelector(\'.operations-health\')?.innerText ?? \'\'')
  if (!parentHealthText.includes('最近24小时观察有效性') || !parentHealthText.includes('已暂停')) {
    throw new Error(`parent health did not visibly reflect the paused physical-device state: ${parentHealthText.slice(0, 1_500)}`)
  }
  await capture(cdp, join(outputDir, '02-physical-parent-health.png'))
  const final = await stableCounts(databasePath, sessionId)
  const report = {
    schema: 'physical_camera_observation_electron_acceptance_v1', passed: true,
    device: 'local physical camera (label intentionally omitted from artifact)',
    checks: { unqualified_state_and_longitudinal_zero: beforePause.stateEvents === 0 && beforePause.longitudinal === 0, qualification_paused_persisted: qualification.status === 'paused', agent_restart_preserved_pause: afterAgentRestart.status === 'paused', renderer_refresh_preserved_pause: true, parent_health_visible: true, final_leases_released: final.activeLeases === 0, raw_media_files_stored: final.rawMedia },
    qualification_before_pause: { status: beforePauseQualification.status, scene_quality: beforePauseQualification.scene_quality, person_baseline: beforePauseQualification.person_baseline, reason: beforePauseQualification.reason, source: beforePauseQualification.source, raw_frame_stored: beforePauseQualification.raw_frame_stored },
    screenshots: ['00-physical-camera-unqualified.png', '01-physical-camera-paused.png', '02-physical-parent-health.png'],
    boundary: 'This verifies device qualification, pause, persistence, and privacy release. It does not measure behavior-model accuracy.'
  }
  await writeFile(join(outputDir, 'result.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ outputDir, passed: true, checks: report.checks }))
}

async function runUnavailableDocumentCameraAcceptance({ cdp, dataDir, sessionId }) {
  const databasePath = join(dataDir, 'agent.db')
  await enterOrResumeStudy(cdp, "Array.from(document.querySelectorAll('button')).some((button) => button.textContent?.includes('用高拍仪拍题'))", 'OCR capture entry did not render')
  await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('用高拍仪拍题'))?.click()")
  await waitFor(async () => (await cdp.evaluate("Boolean(document.querySelector('[aria-label=\"高拍仪采集\"]'))")), 'document capture surface did not render')
  await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('打开高拍仪'))?.click()")
  await waitFor(async () => {
    const text = await cdp.evaluate('document.body.innerText')
    if (!text.includes('未找到已绑定的高拍仪“Fic760xUsbCamera”')) throw new Error(text.slice(0, 1_500))
    return true
  }, 'missing document camera was not explained in the visible child UI')
  const afterFirst = await stableCounts(databasePath, sessionId)
  if (afterFirst.activeLeases !== 0 || afterFirst.rawMedia !== 0) throw new Error(`missing document camera created local media or a lease: ${JSON.stringify(afterFirst)}`)
  if (await cdp.evaluate("Boolean(document.querySelector('[aria-label=\"高拍仪采集\"] video, [aria-label=\"高拍仪采集\"] canvas, [aria-label=\"高拍仪采集\"] img'))")) throw new Error('missing document camera displayed an unexpected media element')
  await cdp.evaluate("document.querySelector('[aria-label=\"高拍仪采集\"]')?.scrollIntoView({ block: 'center' })")
  await sleep(200)
  await capture(cdp, join(outputDir, '01-document-camera-not-found.png'))
  await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('重新查找高拍仪'))?.click()")
  await waitFor(async () => (await cdp.evaluate("document.body.innerText.includes('未找到已绑定的高拍仪“Fic760xUsbCamera”')")), 'document-camera retry lost its exact-bound-device diagnosis')
  const final = await stableCounts(databasePath, sessionId)
  const report = {
    schema: 'physical_document_camera_unavailable_electron_acceptance_v1', passed: true,
    checks: { exact_fic_binding_not_silently_replaced: true, visible_retry: true, final_leases_released: final.activeLeases === 0, raw_media_files_stored: final.rawMedia, ocr_candidate_created: false },
    support: { printed_text: 'not_attempted_device_absent', handwriting: 'not_attempted_device_absent', math_formula: 'not_attempted_device_absent', marking_symbols: 'not_attempted_device_absent' },
    screenshots: ['01-document-camera-not-found.png'],
    boundary: 'Only the real missing-device/retry path was validated. No document was captured, no OCR result was produced, and the observation camera was not used as a fallback.'
  }
  await writeFile(join(outputDir, 'result.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ outputDir, passed: true, checks: report.checks }))
}

async function main() {
  if (!existsSync(electronBin)) throw new Error(`Electron executable is missing: ${electronBin}`)
  if (!existsSync(python)) throw new Error(`Python executable is missing: ${python}`)
  if (!existsSync(join(root, 'out', 'main', 'index.js'))) throw new Error('Run npm run build before Electron acceptance.')
  await mkdir(outputDir, { recursive: true })
  temporaryRoot = await (await import('node:fs/promises')).mkdtemp(join(tmpdir(), 'hsc-observation-electron-'))
  const dataDir = join(temporaryRoot, 'data'), profileDir = join(temporaryRoot, 'profile')
  const harnessEnvironment = sanitizedHarnessEnvironment(process.env)
  const installationSetup = { username: 'synthetic-observation', password: `observation-${process.pid}`, recipient: 'observation-parent@local.test' }
  installationSmtp = createInstallationSmtpSink(installationSetup)
  const installationSmtpPort = await startInstallationSmtpSink(installationSmtp)
  const dataEncryptionKey = randomBytes(32).toString('base64')
  const apiPort = await freePort(), cdpPort = await freePort(), apiUrl = `http://127.0.0.1:${apiPort}`
  const agentLog = join(outputDir, 'local-agent.log'), electronLog = join(outputDir, 'electron.log')
  const agentOut = await (await import('node:fs/promises')).open(agentLog, 'w'); logHandles.push(agentOut)
  agent = spawn(python, ['-m', 'home_study_agent', '--host', '127.0.0.1', '--port', String(apiPort)], { cwd: root, detached: true, stdio: ['ignore', agentOut.fd, agentOut.fd], env: { ...harnessEnvironment, PYTHONPATH: 'services/local_agent/src', HSC_DATA_DIR: dataDir, HSC_DATABASE_PATH: join(dataDir, 'agent.db'), HSC_DATA_ENCRYPTION_KEY: dataEncryptionKey, HSC_MAINTENANCE_ENABLED: '0' } })
  await waitFor(async () => (await fetch(`${apiUrl}/v1/health`)).ok, 'local agent did not start', localAgentHealthTimeoutMs)

  const localNodeModules = join(root, 'node_modules')
  if (!existsSync(localNodeModules)) { await symlink(sourceNodeModules, localNodeModules); nodeModulesLinkCreated = true }
  const electronOut = await (await import('node:fs/promises')).open(electronLog, 'w'); logHandles.push(electronOut)
  electron = spawn(electronBin, [root, `--remote-debugging-port=${cdpPort}`], { cwd: root, detached: true, stdio: ['ignore', electronOut.fd, electronOut.fd], env: { ...harnessEnvironment, HSC_LOCAL_API_URL: apiUrl, HSC_ELECTRON_USER_DATA_DIR: profileDir } })
  const targets = await waitFor(async () => {
    const response = await fetch(`http://127.0.0.1:${cdpPort}/json/list`); if (!response.ok) return null
    return (await response.json()).find((item) => item.type === 'page' && item.webSocketDebuggerUrl)
  }, 'Electron CDP did not start')
  const cdp = new Cdp(targets.webSocketDebuggerUrl); activeCdp = cdp; await cdp.connect()
  try {
    await cdp.send('Page.enable'); await cdp.send('Runtime.enable')
    await completeSyntheticInstallation({ cdp, sink: installationSmtp, port: installationSmtpPort, ...installationSetup })
    const camera = physicalCamera ? await discoverPhysicalCamera(cdp) : documentCameraUnavailable ? {
      deviceType: 'document_camera', deviceId: 'hsc-fic760x-absent-device', label: 'Fic760xUsbCamera',
      capabilities: { transport: 'expected_document_camera', width: null, height: null, frame_rate: null },
    } : undefined
    const { sessionId, parentHeaders } = await prepareStudySession(apiUrl, camera)
    if (documentCameraUnavailable) {
      await cdp.send('Page.reload', { ignoreCache: true })
      await runUnavailableDocumentCameraAcceptance({ cdp, dataDir, sessionId })
      return
    }
    if (physicalCamera) {
      await cdp.send('Page.reload', { ignoreCache: true })
      await runPhysicalCameraAcceptance({ cdp, apiUrl, sessionId, dataDir })
      return
    }
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: syntheticCameraInjection })
    await cdp.send('Page.reload', { ignoreCache: true })
    await enterOrResumeStudy(cdp, "document.querySelector('[aria-label=\"本地观察状态\"]')", 'child observation surface did not render')
    await waitFor(async () => {
      const text = await cdp.evaluate('document.body.innerText')
      if (!text.includes('先补光，再重新检测') || !text.includes('当前画面太暗或对比度不足') || !text.includes('立即重新检测')) throw new Error(text.slice(0, 1_500))
      return true
    }, 'dark synthetic stream did not reach the visible rejection state')
    await capture(cdp, join(outputDir, '01-dark-unqualified.png'))
    const databasePath = join(dataDir, 'agent.db')
    const afterDark = await stableCounts(databasePath, sessionId)
    if (afterDark.stateEvents !== 0 || afterDark.longitudinal !== 0) throw new Error(`dark stream persisted evidence: ${JSON.stringify(afterDark)}`)

    await cdp.evaluate("window.__hscObservationSynthetic.mode = 'absent'; Array.from(document.querySelectorAll('button')).find((button) => button.title === '恢复本地观察')?.click();")
    await waitFor(async () => {
      const text = await cdp.evaluate('document.body.innerText')
      if (!text.includes('请入镜并正对镜头') || !text.includes('常坐位置，保持上半身在画面内')) throw new Error(text.slice(0, 1_500))
      return true
    }, 'no-person synthetic stream did not expose the visible positioning guidance', 12_000)
    await capture(cdp, join(outputDir, '02-awaiting-person.png'))
    const afterAbsent = await stableCounts(databasePath, sessionId)
    if (afterAbsent.stateEvents !== 0 || afterAbsent.longitudinal !== 0) throw new Error(`no-person stream persisted evidence: ${JSON.stringify(afterAbsent)}`)

    await cdp.evaluate("window.__hscObservationSynthetic.mode = 'bright'; Array.from(document.querySelectorAll('button')).find((button) => button.textContent?.includes('立即重新检测'))?.click();")
    await waitFor(async () => {
      const text = await cdp.evaluate('document.body.innerText')
      if (!text.includes('观察摄像头画面可用')) throw new Error(text.slice(0, 1_500))
      return true
    }, 'bright synthetic stream did not become visible local observation', 12_000)
    await waitFor(async () => (await stableCounts(databasePath, sessionId)).longitudinal > 0, 'qualified gentle stream did not reach the longitudinal model', 12_000)
    await capture(cdp, join(outputDir, '03-bright-qualified.png'))
    const gentle = await stableCounts(databasePath, sessionId)

    await request(apiUrl, `/v1/sessions/${sessionId}/observation/correct`, { method: 'POST', body: { correction_text: '合成验收：这不是行为结论。', client_action_id: action('electron-visible-correction') } })
    await cdp.evaluate("window.__hscObservationSynthetic.mode = 'absent'")
    await waitFor(async () => (await stableCounts(databasePath, sessionId)).stateEvents > gentle.stateEvents, 'second gentle observation state was not produced for the independent correction', 8_000)
    const beforeShadow = await stableCounts(databasePath, sessionId)
    await request(apiUrl, `/v1/sessions/${sessionId}/observation/correct`, { method: 'POST', body: { correction_text: '合成验收：继续保持影子模式。', client_action_id: action('electron-visible-correction') } })
    const rule = await request(apiUrl, '/v1/parent/children/child-primary-4/family-rule', { headers: parentHeaders })
    if (rule.observation_mode !== 'shadow') throw new Error('corrections did not persist automatic shadow mode')
    await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.title === '暂停本地观察')?.click();")
    await waitFor(async () => (await cdp.evaluate("document.body.innerText.includes('本地观察已暂停')")), 'visible pause did not complete')
    await waitFor(async () => (await stableCounts(databasePath, sessionId)).activeLeases === 0, 'pause left an active camera lease')
    await cdp.evaluate("window.__hscObservationSynthetic.mode = 'bright'; Array.from(document.querySelectorAll('button')).find((button) => button.title === '恢复本地观察')?.click();")
    await waitFor(async () => {
      const text = await cdp.evaluate('document.body.innerText')
      if (!text.includes('观察摄像头画面可用')) throw new Error(text.slice(0, 1_500))
      return true
    }, 'shadow-mode resume did not render')
    await sleep(2_500)
    const shadow = await stableCounts(databasePath, sessionId)
    if (shadow.longitudinal !== beforeShadow.longitudinal) throw new Error(`shadow stream entered longitudinal model: before=${beforeShadow.longitudinal}, after=${shadow.longitudinal}`)
    await cdp.evaluate("Array.from(document.querySelectorAll('button')).find((button) => button.title === '暂停本地观察')?.click();")
    await waitFor(async () => (await stableCounts(databasePath, sessionId)).activeLeases === 0, 'final pause left an active camera lease')

    await openParentHealth(cdp)
    const parentText = await cdp.evaluate('document.body.innerText')
    if (!parentText.includes('已因纠正自动切换为影子模式')) throw new Error('parent health did not truthfully show current automatic shadow state')
    await cdp.evaluate("document.querySelector('.operations-health')?.scrollIntoView({ block: 'center' })")
    await sleep(200)
    await capture(cdp, join(outputDir, '04-parent-health-shadow.png'))
    const final = await stableCounts(databasePath, sessionId)
    const report = { schema: 'observation_electron_cdp_acceptance_v1', passed: true, session: 'synthetic-local-only', checks: { dark_state_and_longitudinal_zero: afterDark, absent_person_state_and_longitudinal_zero: afterAbsent, gentle_longitudinal_recorded: gentle, shadow_longitudinal_stopped: shadow, final_leases_released: final.activeLeases === 0, parent_health_visible: true }, screenshots: ['01-dark-unqualified.png', '02-awaiting-person.png', '03-bright-qualified.png', '04-parent-health-shadow.png'], privacy: { physical_camera_opened: false, raw_frames_stored: false, raw_media_files_stored: final.rawMedia } }
    await writeFile(join(outputDir, 'result.json'), JSON.stringify(report, null, 2))
    console.log(JSON.stringify({ outputDir, passed: true, checks: report.checks }))
  } finally { cdp.close(); activeCdp = null }
}

function queryCounts(databasePath, sessionId) {
  const database = new DatabaseSync(databasePath, { readOnly: true })
  try {
    return {
      stateEvents: database.prepare("SELECT COUNT(*) AS count FROM child_state_event WHERE session_id = ?").get(sessionId).count,
      longitudinal: database.prepare("SELECT COUNT(*) AS count FROM observable_event WHERE session_id = ? AND source = 'local_vision'").get(sessionId).count,
      activeLeases: database.prepare("SELECT COUNT(*) AS count FROM sensor_lease WHERE status = 'active'").get().count,
      rawMedia: database.prepare("SELECT COUNT(*) AS count FROM capture_artifact WHERE local_path IS NOT NULL").get().count,
    }
  } finally { database.close() }
}

async function stableCounts(databasePath, sessionId) {
  return waitFor(() => {
    try { return queryCounts(databasePath, sessionId) }
    catch (error) {
      if (String(error).includes('database is locked')) return false
      throw error
    }
  }, 'SQLite reader did not become available', 3_000)
}

async function capture(cdp, path) {
  const result = await cdp.send('Page.captureScreenshot', { format: 'png' })
  await writeFile(path, Buffer.from(result.data, 'base64'))
}

try {
  await main()
} catch (error) {
  await mkdir(outputDir, { recursive: true }).catch(() => undefined)
  await writeFile(join(outputDir, 'failure.txt'), `${error.stack ?? error}\n`).catch(() => undefined)
  const visibleText = await activeCdp?.evaluate('document.body.innerText').catch(() => null)
  if (visibleText) await writeFile(join(outputDir, 'failure-ui.txt'), String(visibleText)).catch(() => undefined)
  console.error(String(error.stack ?? error))
  process.exitCode = 1
} finally {
  const electronCleanup = await stopOwnedProcess(electron, 'Electron')
  const agentCleanup = await stopOwnedProcess(agent, 'local Agent')
  if (installationSmtp) await stopInstallationSmtpSink(installationSmtp).catch(() => { process.exitCode = 1 })
  const trackedSurvivors = [...electronCleanup.tracked_survivors, ...agentCleanup.tracked_survivors]
  await writeFile(join(outputDir, 'finalization.json'), JSON.stringify({
    owned_electron_exit_confirmed: electronCleanup.confirmed,
    owned_agent_exit_confirmed: agentCleanup.confirmed,
    tracked_pids: { electron: electronCleanup.tracked_pids, agent: agentCleanup.tracked_pids },
    tracked_survivors: trackedSurvivors,
    node_modules_link_removed_by_harness: nodeModulesLinkCreated,
  }, null, 2)).catch(() => undefined)
  if (!electronCleanup.confirmed || !agentCleanup.confirmed || trackedSurvivors.length) {
    console.error('owned Electron or local Agent process could not be confirmed stopped')
    process.exitCode = 1
  }
  await Promise.all(logHandles.map((handle) => handle.close().catch(() => undefined)))
  if (nodeModulesLinkCreated) await rm(join(root, 'node_modules'), { force: true }).catch(() => undefined)
  if (temporaryRoot) await rm(temporaryRoot, { recursive: true, force: true }).catch(() => undefined)
}
