#!/usr/bin/env node
/**
 * Partial visible Electron acceptance for child voice-calibration recovery.
 *
 * It launches the real Electron main process, real local FastAPI agent and
 * isolated SQLite profile, then attaches only through Electron's explicit CDP
 * port. It visibly verifies the child learning surface. Initial parent
 * calibration setup uses the real protected HTTP endpoint, not the parent UI,
 * so this is not a full onboarding-UI acceptance. No window-name matching,
 * production test hook, credential, raw audio, or real child profile is used.
 * The sole simulated boundary is getUserMedia:
 * a WebAudio-generated MediaStream makes the browser device-open lifecycle
 * deterministic while the UI, HTTP, leases, and persistence remain real.
 */
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { resolve, join } from 'node:path'
import { spawn } from 'node:child_process'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS, ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, releaseChildProcessHandles, stopWindowsProcessTree } from './electron-owned-process.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const required = new Map([
  ['--electron-bin', 'An existing Electron executable; the harness never downloads one.'],
  ['--python-bin', 'An existing development Python for the real local agent.'],
  ['--model-dir', 'An approved read-only local model directory.'],
  ['--output-dir', 'Directory for the non-sensitive JSON record and screenshot.'],
])

function parseArgs(argv) {
  const values = new Map()
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index]
    if (name === '--keep-profile' || name === '--dry-run') values.set(name, true)
    else if (required.has(name)) values.set(name, argv[++index])
    else throw new Error(`Unsupported argument: ${name}`)
  }
  for (const [name, reason] of required) if (!values.get(name)) throw new Error(`${name} is required. ${reason}`)
  return values
}

const sleep = (milliseconds) => new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds))
async function freePort() {
  const server = createServer()
  await new Promise((resolvePromise, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolvePromise))
  const port = server.address().port
  await new Promise((resolvePromise, reject) => server.close((error) => error ? reject(error) : resolvePromise()))
  return port
}
async function eventually(read, label, timeoutMs = 20_000) {
  const started = Date.now(); let lastError
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
      const message = JSON.parse(String(event.data)); const pending = this.waiting.get(message.id)
      if (!pending) return
      this.waiting.delete(message.id)
      if (message.error) pending.reject(new Error(`CDP ${message.error.message}`)); else pending.resolve(message.result)
    })
  }
  send(method, params = {}, timeoutMs = CDP_COMMAND_TIMEOUT_MS) {
    const id = ++this.nextId
    const response = awaitCdpResponse(this.waiting, id, method, timeoutMs)
    this.socket.send(JSON.stringify({ id, method, params }))
    return response
  }
  async evaluate(expression, awaitPromise = true) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text ?? 'CDP page evaluation failed')
    return result.result.value
  }
  close() { this.socket.close() }
}

const SCREENSHOT_TIMEOUT_MS = ELECTRON_RENDERER_STARTUP_TIMEOUT_MS
async function captureSyntheticScreenshot(cdp, path) {
  const errors = []
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const image = await cdp.send('Page.captureScreenshot', { format: 'png' }, SCREENSHOT_TIMEOUT_MS)
      await writeFile(path, Buffer.from(image.data, 'base64'), { mode: 0o600 })
      return { captured: true, attempts: attempt, errors }
    } catch (error) { errors.push(error instanceof Error ? error.message : String(error)) }
  }
  return { captured: false, attempts: 2, errors }
}

const syntheticProfile = {
  version: 2, childId: 'child-primary-4', consentedAt: '2026-08-28T10:00:00Z', updatedAt: '2026-08-28T10:00:00Z',
  sampleCount: 48_000, calibrationCount: 3, energy: 0.12, zeroCrossingRate: 0.08,
  spectralBands: [0.2, 0.3, 0.3, 0.2], environmentSpread: 0.1,
}
const syntheticMicrophone = {
  system_id_hash: 'a'.repeat(64), label: 'Synthetic USB Microphone',
  capabilities: { transport: 'browser-media-device', echo_cancellation_requested: true, noise_suppression_requested: true, auto_gain_control_requested: true },
}

const mediaShim = `(() => {
  const calls = []; Object.defineProperty(window, '__hscSyntheticMediaCalls', { value: calls, configurable: false });
  const devices = [{ kind: 'audioinput', deviceId: 'synthetic-bound-mic', groupId: 'synthetic-group', label: 'Synthetic USB Microphone' }];
  Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', { configurable: true, value: async () => devices });
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: async (constraints) => {
    calls.push(constraints); const source = new AudioContext(); const destination = source.createMediaStreamDestination();
    const oscillator = source.createOscillator(); const gain = source.createGain(); gain.gain.value = 0.16;
    oscillator.frequency.value = 220; oscillator.connect(gain).connect(destination); oscillator.start();
    const track = destination.stream.getAudioTracks()[0]; const stop = track.stop.bind(track);
    Object.defineProperty(track, 'getSettings', { value: () => ({ deviceId: 'synthetic-bound-mic', groupId: 'synthetic-group' }) });
    Object.defineProperty(track, 'getCapabilities', { value: () => ({}) });
    Object.defineProperty(track, 'label', { value: 'Synthetic USB Microphone' });
    Object.defineProperty(track, 'stop', { value: () => { stop(); oscillator.stop(); void source.close(); } });
    return destination.stream;
  }});
})()`

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (args.get('--dry-run')) {
    process.stdout.write(JSON.stringify({ mode: 'dry_run', boundary: 'CDP page attachment plus WebAudio synthetic getUserMedia only', required: [...required.keys()] }) + '\n')
    return
  }
  const electronBin = resolve(args.get('--electron-bin')); const pythonBin = resolve(args.get('--python-bin')); const modelDir = resolve(args.get('--model-dir')); const outputDir = resolve(args.get('--output-dir'))
  for (const path of [electronBin, pythonBin]) if (!existsSync(path)) throw new Error(`Required executable is missing: ${path}`)
  if (!existsSync(modelDir)) throw new Error(`Approved model directory is missing: ${modelDir}`)
  if (!existsSync(join(root, 'out', 'main', 'index.js'))) throw new Error('Run npm run build before Electron acceptance.')
  await mkdir(outputDir, { recursive: true })
  const profileDir = await mkdtemp(join(tmpdir(), 'hsc-electron-voice-e2e-'))
  const port = await freePort(); let child; let cdp
  const record = { record_type: 'electron_voice_child_recovery_e2e_v1', synthetic_profile: true, raw_audio_stored: false, natural_voice_tested: false, parent_calibration_boundary: 'Initial parent calibration is saved through the real protected HTTP endpoint, not a visible parent UI action.', media_boundary: 'WebAudio synthetic MediaStream replaces getUserMedia only; child UI, FastAPI, leases and SQLite are real.', checks: {} }
  try {
    child = spawn(electronBin, [root, `--remote-debugging-port=${port}`], {
      cwd: root,
      env: { ...sanitizedHarnessEnvironment(process.env), HSC_ELECTRON_USER_DATA_DIR: profileDir, HSC_LOCAL_AGENT_PYTHON: pythonBin, HSC_LOCAL_MODEL_DIR: modelDir },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const stderr = []; child.stderr.on('data', (chunk) => stderr.push(String(chunk)))
    const page = await eventually(async () => {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      const target = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl)
      if (!target) throw new Error('Electron renderer target not published')
      return target
    }, 'Electron CDP target', ELECTRON_RENDERER_STARTUP_TIMEOUT_MS)
    cdp = new Cdp(page.webSocketDebuggerUrl); await cdp.open(); await cdp.send('Page.enable'); await cdp.send('Runtime.enable')
    await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: mediaShim })
    const runtime = await eventually(() => cdp.evaluate('window.desktop?.getRuntimeConfig()'), 'desktop bridge')
    if (!runtime || runtime.status !== 'ready') throw new Error('Electron local runtime was not ready')
    record.checks.runtime = { ready: true, local_api_loopback: String(runtime.localApiUrl).startsWith('http://127.0.0.1:') }
    const setup = await cdp.evaluate(`(async () => {
      const runtime = await window.desktop.getRuntimeConfig(); const headers = { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken };
      const api = async (path, init = {}) => { const response = await fetch(runtime.localApiUrl + path, { ...init, headers: { ...headers, ...(init.headers || {}) } }); if (!response.ok) throw new Error(path + ':' + response.status); return response.json() };
      const parent = await api('/v1/parent/setup', { method: 'POST', body: JSON.stringify({ pin: '2468' }) });
      const saved = await api('/v1/parent/children/child-primary-4/voice-calibration', { method: 'PUT', headers: { 'X-HSC-Parent': parent.parent_token }, body: JSON.stringify({ profile: ${JSON.stringify(syntheticProfile)}, vocabulary: ['合成验收词'], microphone: ${JSON.stringify(syntheticMicrophone)}, client_action_id: 'electron-synthetic-calibration-save-v1' }) });
      const session = await api('/v1/sessions', { method: 'POST', body: JSON.stringify({ child_id: 'child-primary-4', client_action_id: 'electron-synthetic-session-v1' }) });
      return { saved, session_id: session.session_id };
    })()`)
    if (setup.saved.raw_audio_stored !== false) throw new Error('Calibration response incorrectly reported raw audio storage')
    record.checks.parent_save = { via_real_http: true, raw_audio_stored: false, visible_parent_ui: false }
    await cdp.evaluate('window.location.reload()')
    await eventually(async () => {
      const state = await cdp.evaluate(`({ panel: Boolean(document.querySelector('[aria-label="语音学习伙伴"]')), text: document.body.innerText })`)
      if (!state.panel && !state.text.includes('继续今天的学习')) throw new Error('neither zero-click recovery nor session resume UI is visible')
      return state
    }, 'zero-click child recovery')
    if (!await cdp.evaluate(`Boolean(document.querySelector('[aria-label="语音学习伙伴"]'))`)) {
      throw new Error('active child session did not recover without a forbidden child button')
    }
    const childUi = await eventually(async () => {
      const state = await cdp.evaluate(`(() => { const panel = document.querySelector('.agent-calibration'); return { calibration: panel?.textContent ?? '', visible: document.body.innerText, display: panel ? getComputedStyle(panel).display : null, controls: panel?.querySelectorAll('button,input,select').length ?? -1 } })()`)
      if (!state.calibration.includes('已恢复家长保存的三段本机环境特征') || !state.calibration.includes('校准与麦克风绑定由家长中心管理')) throw new Error('read-only recovered calibration was not hydrated')
      if (state.display !== 'none' || state.controls !== 0) throw new Error('child calibration management controls were exposed')
      return state
    }, 'child calibration recovery')
    record.checks.child_read_only_recovery = { service_profile_hydrated: true, child_management_controls_hidden: true }
    const goHome = async () => {
      await cdp.evaluate(`(() => { const button = document.querySelector('button[title="返回首页"]'); if (!(button instanceof HTMLButtonElement)) throw new Error('visible return-home button missing'); button.click() })()`)
      await eventually(async () => {
        const text = await cdp.evaluate('document.body.innerText')
        if (!text.includes('继续今天的学习')) throw new Error('home screen did not return')
        return text
      }, 'home screen')
    }
    const enterParent = async () => {
      await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.includes('家长中心')); if (!(button instanceof HTMLButtonElement)) throw new Error('visible parent-center button missing'); button.click() })()`)
      await eventually(async () => {
        const text = await cdp.evaluate('document.body.innerText')
        if (!text.includes('家长中心')) throw new Error('parent center did not render')
        return text
      }, 'parent center')
      const needsUnlock = await cdp.evaluate('Boolean(document.querySelector("input[aria-label=\\"家长 PIN\\"]"))')
      if (needsUnlock) {
        await cdp.evaluate(`(() => {
        const input = document.querySelector('input[aria-label="家长 PIN"]'); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(input, '2468'); input.dispatchEvent(new Event('input', { bubbles: true }));
      })()`)
        await eventually(() => cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.includes('设置并进入') || item.textContent?.includes('解锁')); if (!(button instanceof HTMLButtonElement) || button.disabled) throw new Error('parent unlock button is not enabled'); button.click(); return true })()`), 'enabled parent unlock')
      }
      await eventually(async () => {
        const tabs = await cdp.evaluate('Array.from(document.querySelectorAll(".parent-tabs button")).map((item) => item.textContent)')
        if (!Array.isArray(tabs) || !tabs.length) throw new Error('parent controls remain locked')
        return tabs
      }, 'parent unlock')
      await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('.parent-tabs button')).find((item) => item.textContent?.includes('四年级同学')); if (!(button instanceof HTMLButtonElement)) throw new Error('synthetic primary child tab missing'); button.click() })()`)
    }
    const closeParent = async () => {
      await cdp.evaluate(`(() => { const button = document.querySelector('button[title="返回孩子首页"]'); if (!(button instanceof HTMLButtonElement)) throw new Error('parent return button missing'); button.click() })()`)
      await eventually(async () => { const text = await cdp.evaluate('document.body.innerText'); if (!text.includes('继续今天的学习')) throw new Error('home not restored from parent center'); return text }, 'parent return home')
    }
    const resumeChild = async () => {
      await cdp.evaluate('window.location.reload()')
      await eventually(async () => { const panel = await cdp.evaluate(`Boolean(document.querySelector('[aria-label="语音学习伙伴"]'))`); if (!panel) throw new Error('active child session did not resume without a child button'); return panel }, 'zero-click child resume')
    }
    await goHome(); await enterParent()
    await eventually(async () => { const text = await cdp.evaluate('document.body.innerText'); if (!text.includes('删除声音特征')) throw new Error('parent calibration delete control not visible'); return text }, 'parent calibration controls')
    await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.includes('删除声音特征')); if (!(button instanceof HTMLButtonElement)) throw new Error('parent delete button missing'); button.click() })()`)
    await eventually(async () => { const text = await cdp.evaluate('document.body.innerText'); if (!text.includes('本机声音特征已删除')) throw new Error('parent delete did not persist'); return text }, 'parent delete persistence')
    await closeParent(); await resumeChild()
    await eventually(async () => { const text = await cdp.evaluate(`document.querySelector('.agent-calibration')?.textContent ?? ''`); if (!text.includes('尚未由家长完成三段本机声音校准')) throw new Error('child did not synchronize parent deletion'); return text }, 'child deletion synchronization')
    await goHome(); await enterParent()
    await cdp.evaluate(`(() => { const checkbox = document.querySelector('.voice-calibration input[type="checkbox"]'); if (!(checkbox instanceof HTMLInputElement)) throw new Error('parent calibration consent missing'); checkbox.click(); const select = document.querySelector('select[aria-label="选择校准麦克风"]'); if (!(select instanceof HTMLSelectElement)) throw new Error('parent calibration microphone selector missing'); select.value = 'synthetic-bound-mic'; select.dispatchEvent(new Event('change', { bubbles: true })) })()`)
    await sleep(120)
    for (let sample = 0; sample < 3; sample += 1) {
      await eventually(() => cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('.voice-calibration button')).find((item) => item.textContent?.includes('录第一段校准') || item.textContent?.includes('录下一段校准')); if (!(button instanceof HTMLButtonElement) || button.disabled) throw new Error('calibration record button is not ready'); button.click(); return true })()`), `calibration sample ${sample + 1} ready`)
      await eventually(async () => { const text = await cdp.evaluate(`document.querySelector('.voice-calibration')?.textContent ?? ''`); if (!text.includes('结束并识别')) throw new Error('synthetic calibration recorder did not open'); return text }, `calibration sample ${sample + 1} open`)
      // The production quality gate deliberately rejects sub-second samples.  Keep
      // this synthetic WebAudio utterance long enough to exercise the same visible
      // parent flow rather than bypassing the calibration component.
      await sleep(1_500)
      await eventually(() => cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('.voice-calibration button')).find((item) => item.textContent?.includes('结束并识别')); if (!(button instanceof HTMLButtonElement) || button.disabled) throw new Error('calibration stop button is not ready'); button.click(); return true })()`), `calibration sample ${sample + 1} stop`)
      await eventually(async () => {
        const text = await cdp.evaluate('document.body.innerText')
        if (sample < 2 && !text.includes(`第 ${sample + 2} / 3`)) throw new Error(`calibration sample ${sample + 1} was not accepted`)
        if (sample === 2 && !text.includes('校准完成：三段特征已保存在本机服务')) throw new Error('third calibration sample did not persist')
        return text
      }, `calibration sample ${sample + 1} accepted`)
    }
    await eventually(async () => { const text = await cdp.evaluate('document.body.innerText'); if (!text.includes('校准完成：三段特征已保存在本机服务')) throw new Error('parent UI calibration did not save'); return text }, 'parent UI calibration save')
    record.checks.parent_ui_delete_recalibration = { visible_delete: true, visible_recalibration_save: true, raw_audio_stored: false }
    await cdp.evaluate(`(() => document.querySelector('.voice-calibration')?.scrollIntoView({ block: 'center' }))()`)
    await sleep(150)
    const parentVisual = await captureSyntheticScreenshot(cdp, join(outputDir, 'parent-voice-calibration-synthetic.png'))
    await closeParent(); await resumeChild()
    await eventually(async () => { const text = await cdp.evaluate(`document.querySelector('.agent-calibration')?.textContent ?? ''`); if (!text.includes('已恢复家长保存的三段本机环境特征')) throw new Error('child did not synchronize parent recalibration'); return text }, 'child recalibration synchronization')
    const handsFreeOwnsMicrophone = await cdp.evaluate(`(() => {
      const button = document.querySelector('.hands-free-control button')
      return button instanceof HTMLButtonElement && button.classList.contains('is-recording')
    })()`)
    if (handsFreeOwnsMicrophone) {
      await cdp.evaluate(`(() => { const button = document.querySelector('.hands-free-control button'); if (!(button instanceof HTMLButtonElement)) throw new Error('hands-free stop control missing'); button.click() })()`)
      await eventually(async () => {
        const ready = await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.trim() === '开始说话'); return button instanceof HTMLButtonElement && !button.disabled })()`)
        if (!ready) throw new Error('manual recorder is still blocked after hands-free stop')
        return ready
      }, 'microphone handoff to manual control')
    }
    const controlledCaptureStart = await cdp.evaluate('window.__hscSyntheticMediaCalls.length')
    await eventually(async () => {
      const state = await cdp.evaluate(`(() => {
        const button = document.querySelector('.agent-voice-controls > .local-input-control:not(.hands-free-control) button')
        return { found: button instanceof HTMLButtonElement, disabled: button instanceof HTMLButtonElement ? button.disabled : null, text: button?.textContent?.trim() ?? '' }
      })()`)
      if (!state.found || state.disabled || state.text !== '开始说话') throw new Error(`manual control is not ready: ${JSON.stringify(state)}`)
      return state
    }, 'manual control ready')
    await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls > .local-input-control:not(.hands-free-control) button'); if (!(button instanceof HTMLButtonElement)) throw new Error('manual voice control missing'); button.click() })()`)
    await eventually(async () => {
      const state = await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls > .local-input-control:not(.hands-free-control) button'); return { text: button?.textContent?.trim() ?? '', disabled: button instanceof HTMLButtonElement ? button.disabled : null } })()`)
      if (state.text !== '结束并识别') throw new Error(`manual recorder did not open: ${JSON.stringify(state)}`)
      return state
    }, 'manual synthetic open')
    await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls > .local-input-control:not(.hands-free-control) button'); if (button instanceof HTMLButtonElement && button.textContent?.trim() === '结束并识别') button.click() })()`)
    await eventually(() => cdp.evaluate(`(async () => {
      const runtime = await window.desktop.getRuntimeConfig(); const headers = { 'X-HSC-Session': runtime.sessionToken };
      return (await (await fetch(runtime.localApiUrl + '/v1/sessions/${setup.session_id}', { headers })).json()).sensor_release.active_leases;
    })().then((active) => { if (active !== 0) throw new Error('manual lease is still releasing'); return active })`), 'manual lease release')
    await eventually(async () => {
      const available = await cdp.evaluate(`(() => {
        const button = document.querySelector('.hands-free-control button')
        return button instanceof HTMLButtonElement && !button.disabled
      })()`)
      if (!available) throw new Error('manual control has not handed the microphone back to hands-free mode')
      return available
    }, 'microphone handoff to hands-free control')
    const handsFreeStart = await cdp.evaluate(`(() => {
      const button = document.querySelector('.hands-free-control button')
      if (!(button instanceof HTMLButtonElement)) throw new Error('hands-free start control missing')
      const state = button.textContent?.trim() ?? ''
      const alreadyActive = /正在听|正在处理|取消打开麦克风/.test(state)
      const callsBefore = window.__hscSyntheticMediaCalls.length
      if (!alreadyActive) button.click()
      return { alreadyActive, callsBefore }
    })()`)
    await eventually(async () => {
      const opened = await cdp.evaluate(`(() => {
        const button = document.querySelector('.hands-free-control button')
        return {
          calls: window.__hscSyntheticMediaCalls.length,
          state: button instanceof HTMLButtonElement ? button.textContent?.trim() : ''
        }
      })()`)
      const expectedCalls = handsFreeStart.callsBefore + (handsFreeStart.alreadyActive ? 0 : 1)
      if (opened.calls < expectedCalls || !/正在听|正在处理|取消打开麦克风/.test(opened.state ?? '')) throw new Error(`hands-free recorder did not open: ${JSON.stringify(opened)}`)
      return opened
    }, 'hands-free synthetic open')
    await cdp.evaluate(`(() => {
      const button = document.querySelector('.hands-free-control button')
      if (!(button instanceof HTMLButtonElement) || !/正在听|正在处理|取消打开麦克风/.test(button.textContent ?? '')) throw new Error('hands-free stop control missing')
      button.click()
    })()`)
    const afterStop = await eventually(async () => {
      const state = await cdp.evaluate(`(async () => {
        const runtime = await window.desktop.getRuntimeConfig(); const headers = { 'X-HSC-Session': runtime.sessionToken };
        const workspace = await (await fetch(runtime.localApiUrl + '/v1/sessions/${setup.session_id}', { headers })).json();
        return { calls: window.__hscSyntheticMediaCalls, release: workspace.sensor_release };
      })()`)
      if (state.release.active_leases !== 0 || state.release.released_leases < 2) throw new Error(`microphone leases still active: ${JSON.stringify(state.release)}`)
      return state
    }, 'lease release')
    const bindingsUsed = afterStop.calls.slice(controlledCaptureStart).map((call) => call?.audio?.deviceId?.exact)
    if (bindingsUsed.length < 2 || bindingsUsed.some((id) => id !== 'synthetic-bound-mic')) throw new Error(`manual and hands-free controls did not receive the same bound microphone: ${JSON.stringify(bindingsUsed)}`)
    if (afterStop.release.active_leases !== 0 || afterStop.release.released_leases < 2) throw new Error('synthetic microphone leases were not released')
    record.checks.bound_microphone = { manual_and_hands_free: true, device_id: 'synthetic-bound-mic' }
    record.checks.stop_release = { active_leases: afterStop.release.active_leases, released_leases: afterStop.release.released_leases }
    const restarted = await cdp.evaluate('window.desktop.restartLocalService()')
    if (restarted.status !== 'ready') throw new Error('local agent did not restart')
    await cdp.evaluate('window.location.reload()')
    await eventually(async () => {
      const state = await cdp.evaluate(`({ panel: Boolean(document.querySelector('[aria-label="语音学习伙伴"]')), text: document.body.innerText })`)
      if (!state.panel && !state.text.includes('继续今天的学习')) throw new Error('session did not recover after local-agent restart')
      return state
    }, 'restart session recovery')
    if (!await cdp.evaluate(`Boolean(document.querySelector('[aria-label="语音学习伙伴"]'))`)) {
      throw new Error('active child session did not recover without a forbidden child button after restart')
    }
    await eventually(async () => {
      const text = await cdp.evaluate(`document.querySelector('.agent-calibration')?.textContent ?? ''`)
      if (!text.includes('已恢复家长保存的三段本机环境特征')) throw new Error('calibration did not recover after restart')
      return text
    }, 'restart recovery')
    record.checks.restart_recovery = { renderer_visible: true, sqlite_profile_reused: true }
    const childVisual = await captureSyntheticScreenshot(cdp, join(outputDir, 'electron-voice-onboarding-synthetic.png'))
    record.screenshots = {}
    if (parentVisual.captured) record.screenshots.parent_visible_recalibration = 'parent-voice-calibration-synthetic.png'
    if (childVisual.captured) record.screenshots.child_recovery = 'electron-voice-onboarding-synthetic.png'
    record.visual_evidence = { parent: parentVisual, child: childVisual }
    record.status = parentVisual.captured && childVisual.captured ? 'passed' : 'visual_evidence_missing'
    record.dom_evidence = { has_read_only_recovery_copy: childUi.calibration.includes('校准与麦克风绑定由家长中心管理'), child_controls_hidden: childUi.display === 'none', has_natural_voice_claim: childUi.visible.includes('自然女声') }
    await writeFile(join(outputDir, 'electron-voice-onboarding-evidence.json'), JSON.stringify(record, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
    process.stdout.write(JSON.stringify({ status: record.status, outputDir, checks: record.checks }) + '\n')
    if (record.status === 'visual_evidence_missing') process.exitCode = 2
  } catch (error) {
    record.error = error instanceof Error ? error.message : String(error)
    try {
      // This isolated profile contains only seeded synthetic fixtures. Keep a
      // bounded UI diagnostic beside a failed record; it is never committed.
      record.failure_ui_text = String(await cdp?.evaluate('document.body.innerText')).slice(0, 8_000)
    } catch {}
    await writeFile(join(outputDir, 'electron-voice-onboarding-evidence.json'), JSON.stringify(record, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 }).catch(() => undefined)
    throw error
  } finally {
    try { await cdp?.send('Browser.close') } catch {}
    cdp?.close()
    if (process.platform === 'win32') await stopWindowsProcessTree(child, eventually)
    else if (child?.exitCode === null) child.kill('SIGTERM')
    releaseChildProcessHandles(child)
    if (!args.get('--keep-profile')) await rm(profileDir, { recursive: true, force: true })
  }
}

main().catch((error) => { console.error(`[electron-voice-onboarding-e2e] ${error instanceof Error ? error.message : String(error)}`); process.exit(1) })
