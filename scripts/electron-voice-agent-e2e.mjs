#!/usr/bin/env node
/**
 * Isolated Electron acceptance for the child voice-command vertical slice.
 *
 * The real Electron main process, FastAPI agent, SQLite, local sherpa-onnx
 * ASR and renderer are used. Semantic command coverage requires an explicitly
 * approved adult/synthetic WAV fixture manifest; without it the harness runs
 * only a programmatic non-speech ASR boundary. It never claims either mode is
 * child speech or natural TTS. Cloud acceptance imports only named local
 * Keychain credentials into a disposable Electron profile.
 */
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { createServer } from 'node:net'
import { resolve, join, relative, dirname } from 'node:path'
import { spawn } from 'node:child_process'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS, ELECTRON_RENDERER_STARTUP_TIMEOUT_MS, releaseChildProcessHandles, stopWindowsProcessTree } from './electron-owned-process.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const required = new Map([
  ['--electron-bin', 'An existing Electron executable; this harness never downloads one.'],
  ['--python-bin', 'An existing development Python for the real local agent.'],
  ['--model-dir', 'An approved read-only local ASR model directory.'],
  ['--output-dir', 'Directory for non-sensitive JSON and screenshots.'],
])
const optionalValueArgs = new Set(['--fixture-dir', '--model-keychain-service', '--qwen-realtime-keychain-service', '--qwen-realtime-endpoint', '--trial-profile'])

function parseArgs(argv) {
  const values = new Map()
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index]
    if (name === '--dry-run' || name === '--asr-boundary-only' || name === '--drop-public-tool-event' || name === '--drop-task-tool-event') values.set(name, true)
    else if (required.has(name) || optionalValueArgs.has(name)) values.set(name, argv[++index])
    else throw new Error(`Unsupported argument: ${name}`)
  }
  for (const [name, reason] of required) if (!values.get(name)) throw new Error(`${name} is required. ${reason}`)
  return values
}

const sleep = (milliseconds) => new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds))
function createInstallationSmtpSink(username, password) {
  const messages = []; let authentications = 0
  const server = createServer((socket) => {
    socket.setEncoding('utf8'); socket.write('220 hsc-voice-loopback ESMTP\r\n')
    let buffer = '', data = false, body = '', authState = '', authUser = ''
    const authenticate = (user, secret) => {
      if (user === username && secret === password) { authentications += 1; socket.write('235 2.7.0 authenticated\r\n') }
      else socket.write('535 5.7.8 invalid credentials\r\n')
    }
    socket.on('data', (chunk) => {
      buffer += chunk
      while (buffer.includes('\n')) {
        const end = buffer.indexOf('\n'); const line = buffer.slice(0, end + 1).replace(/\r?\n$/, ''); buffer = buffer.slice(end + 1)
        if (data) {
          if (line === '.') { messages.push(body); body = ''; data = false; socket.write('250 2.0.0 accepted\r\n') }
          else body += `${line}\n`
          continue
        }
        if (authState === 'plain') { const parts = Buffer.from(line, 'base64').toString().split('\0'); authState = ''; authenticate(parts.at(-2) ?? '', parts.at(-1) ?? ''); continue }
        if (authState === 'login-user') { authUser = Buffer.from(line, 'base64').toString(); authState = 'login-pass'; socket.write('334 UGFzc3dvcmQ6\r\n'); continue }
        if (authState === 'login-pass') { authState = ''; authenticate(authUser, Buffer.from(line, 'base64').toString()); continue }
        const [command, ...rest] = line.split(' '); const upper = command.toUpperCase()
        if (upper === 'EHLO' || upper === 'HELO') socket.write('250-hsc-voice-loopback\r\n250 AUTH PLAIN LOGIN\r\n')
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN' && rest[1]) { const parts = Buffer.from(rest[1], 'base64').toString().split('\0'); authenticate(parts.at(-2) ?? '', parts.at(-1) ?? '') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'PLAIN') { authState = 'plain'; socket.write('334 \r\n') }
        else if (upper === 'AUTH' && rest[0]?.toUpperCase() === 'LOGIN') { authState = 'login-user'; socket.write('334 VXNlcm5hbWU6\r\n') }
        else if (upper === 'MAIL' || upper === 'RCPT' || upper === 'RSET' || upper === 'NOOP') socket.write('250 2.0.0 ok\r\n')
        else if (upper === 'DATA') { data = true; socket.write('354 end with <CRLF>.<CRLF>\r\n') }
        else if (upper === 'QUIT') { socket.write('221 2.0.0 bye\r\n'); socket.end() }
        else socket.write('250 2.0.0 ok\r\n')
      }
    })
  })
  return { server, messages, authentications: () => authentications }
}
async function eventually(read, label, timeoutMs = 20_000) {
  const started = Date.now(); let lastError
  while (Date.now() - started < timeoutMs) {
    try { return await read() } catch (error) { lastError = error; await sleep(150) }
  }
  throw new Error(`${label} timed out: ${lastError instanceof Error ? lastError.message : String(lastError)}`)
}
async function freePort() {
  const server = createServer()
  await new Promise((resolvePromise, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolvePromise))
  const port = server.address().port
  await new Promise((resolvePromise, reject) => server.close((error) => error ? reject(error) : resolvePromise()))
  return port
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
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text ?? 'CDP page evaluation failed')
    return result.result.value
  }
  close() { this.socket.close() }
}

// Reads PCM from a queued fixture into a normal browser MediaStream.  The
// production recorder still does device registration, recording, resampling,
// base64 transport, local ASR, and lease release itself.
const mediaShim = `(() => {
  const protocol = []; window.__hscRealtimeProtocol = protocol;
  const observedChannels = new WeakSet();
  const observeChannel = channel => {
    if (observedChannels.has(channel)) return;
    observedChannels.add(channel);
    channel.addEventListener('message', event => {
      try {
        const item = JSON.parse(event.data);
        const dropped = window.__hscDropPublicToolEvent === true && item.type === 'response.function_call_arguments.done';
        if (dropped) { window.__hscDropPublicToolEvent = false; window.__hscDroppedPublicToolEvents = (window.__hscDroppedPublicToolEvents ?? 0) + 1; event.stopImmediatePropagation(); }
        protocol.push({ type: item.type, time: Date.now(), fault_injected: dropped, response_id: item.response_id ?? item.response?.id, status: item.response?.status, error_code: typeof item.error?.code === 'string' && /^[a-zA-Z0-9_.-]{1,80}$/.test(item.error.code) ? item.error.code : undefined, transcript_chars: typeof item.transcript === 'string' ? item.transcript.length : 0 });
        if (protocol.length > 200) protocol.shift();
      } catch {}
    });
  };
  const OriginalPeer = window.RTCPeerConnection;
  window.RTCPeerConnection = class extends OriginalPeer {
    constructor(...args) { super(...args); this.addEventListener('datachannel', event => observeChannel(event.channel)); }
    createDataChannel(...args) { const channel = super.createDataChannel(...args); observeChannel(channel); return channel; }
  };
  const calls = []; Object.defineProperty(window, '__hscVoiceAgentMediaCalls', { value: calls, configurable: false });
  const destinations = []; Object.defineProperty(window, '__hscVoiceAgentDestinations', { value: destinations, configurable: false });
  window.__hscVoiceAgentInjectWav = async (encoded) => {
    let count = 0;
    for (const entry of destinations) {
      if (entry.track.readyState !== 'live' || !entry.track.enabled) continue;
      const binary = atob(encoded); const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      const buffer = await entry.context.decodeAudioData(bytes.buffer);
      const source = entry.context.createBufferSource(); source.buffer = buffer;
      source.connect(entry.destination); source.start(entry.context.currentTime + 0.2); count += 1;
    }
    return count;
  };
  const devices = [{ kind: 'audioinput', deviceId: 'synthetic-voice-agent-mic', groupId: 'synthetic-agent-group', label: 'Synthetic adult acceptance microphone' }];
  Object.defineProperty(navigator.mediaDevices, 'enumerateDevices', { configurable: true, value: async () => devices });
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: async (constraints) => {
    calls.push(constraints); const context = new AudioContext(); const destination = context.createMediaStreamDestination();
    const queued = Array.isArray(window.__hscVoiceAgentWavQueue) ? window.__hscVoiceAgentWavQueue.shift() : null;
    let generator;
    if (typeof queued === 'string') {
      const binary = atob(queued); const bytes = new Uint8Array(binary.length); for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      const buffer = await context.decodeAudioData(bytes.buffer); generator = context.createBufferSource(); generator.buffer = buffer; generator.connect(destination); generator.start();
    } else {
      generator = context.createOscillator(); generator.frequency.value = 220; const gain = context.createGain(); gain.gain.value = 0.001; generator.connect(gain).connect(destination); generator.start();
    }
    const track = destination.stream.getAudioTracks()[0]; const stop = track.stop.bind(track);
    destinations.push({ context, destination, track });
    Object.defineProperty(track, 'getSettings', { value: () => ({ deviceId: 'synthetic-voice-agent-mic', groupId: 'synthetic-agent-group' }) });
    Object.defineProperty(track, 'getCapabilities', { value: () => ({}) }); Object.defineProperty(track, 'label', { value: devices[0].label });
    Object.defineProperty(track, 'stop', { value: () => { stop(); try { generator.stop() } catch {} void context.close() } });
    return destination.stream;
  }});
})()`

const semanticFixtureNames = ['start', 'pause', 'resume', 'next', 'privacy']

/** A deterministic in-memory tone: it is intentionally not human speech. */
export function createNonSpeechBoundaryWav() {
  const sampleRate = 16_000
  const samples = new Int16Array(sampleRate * 2)
  for (let index = Math.floor(sampleRate * 0.35); index < Math.floor(sampleRate * 1.35); index += 1) {
    samples[index] = Math.round(Math.sin((index - sampleRate * 0.35) * 2 * Math.PI * 440 / sampleRate) * 0.18 * 32767)
  }
  const wav = Buffer.alloc(44 + samples.byteLength)
  wav.write('RIFF', 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8)
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(sampleRate, 24)
  wav.writeUInt32LE(sampleRate * 2, 28); wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36); wav.writeUInt32LE(samples.byteLength, 40)
  Buffer.from(samples.buffer).copy(wav, 44)
  return wav
}

export async function loadApprovedSemanticFixtures(directory) {
  const fixtureDir = resolve(directory)
  const manifestPath = join(fixtureDir, 'fixture-manifest.json')
  let manifest
  try { manifest = JSON.parse(await readFile(manifestPath, 'utf8')) } catch { throw new Error('Semantic ASR fixtures require a readable fixture-manifest.json.') }
  if (!manifest || typeof manifest !== 'object') throw new Error('Fixture manifest must be an object.')
  if (manifest.version !== 1 || typeof manifest.license !== 'string' || !manifest.license.trim() || manifest.speaker !== 'adult_or_synthetic_non_child' || !manifest.fixtures || typeof manifest.fixtures !== 'object') {
    throw new Error('Fixture manifest must declare version 1, a non-empty license, and speaker "adult_or_synthetic_non_child".')
  }
  const fixtures = {}; const fixtureHashes = {}
  for (const name of [...semanticFixtureNames, ...(manifest.fixtures.conversation ? ['conversation'] : [])]) {
    const item = manifest.fixtures[name]
    if (!item || typeof item.file !== 'string' || typeof item.sha256 !== 'string' || !/^[a-f0-9]{64}$/i.test(item.sha256)) throw new Error(`Fixture ${name} needs file and SHA-256.`)
    const path = resolve(fixtureDir, item.file)
    if (relative(fixtureDir, path).startsWith('..')) throw new Error(`Fixture ${name} escapes its fixture directory.`)
    const wav = await readFile(path)
    const digest = createHash('sha256').update(wav).digest('hex')
    if (digest !== item.sha256.toLowerCase()) throw new Error(`Fixture ${name} SHA-256 does not match its manifest.`)
    if (wav.subarray(0, 4).toString('ascii') !== 'RIFF' || wav.subarray(8, 12).toString('ascii') !== 'WAVE') throw new Error(`Fixture ${name} is not a WAV file.`)
    fixtures[name] = wav.toString('base64'); fixtureHashes[name] = digest
  }
  return { fixtures, metadata: { license: manifest.license.trim(), speaker: manifest.speaker, fixture_hashes: fixtureHashes } }
}

const SCREENSHOT_TIMEOUT_MS = ELECTRON_RENDERER_STARTUP_TIMEOUT_MS
async function screenshot(cdp, path) {
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

function runCommand(command, args) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] }); let stdout = '', stderr = ''
    child.stdout.on('data', (chunk) => { stdout += String(chunk) }); child.stderr.on('data', (chunk) => { stderr += String(chunk) })
    child.once('error', reject); child.once('exit', (code) => code === 0 ? resolvePromise(stdout) : reject(new Error(`${command} exited ${code}: ${stderr.slice(-500)}`)))
  })
}

async function stopElectron(child, cdp) {
  cdp?.close()
  if (!child?.pid) return
  if (process.platform === 'win32') {
    await stopWindowsProcessTree(child, eventually, async (command, args) => ({ stdout: await runCommand(command, args) }))
  } else if (child.exitCode === null) {
    try { process.kill(-child.pid, 'SIGTERM') } catch { child.kill('SIGTERM') }
    if (!await eventually(() => child.exitCode !== null, 'owned Electron exit', 7_000).catch(() => false)) {
    try { process.kill(-child.pid, 'SIGKILL') } catch { child.kill('SIGKILL') }
    await eventually(() => child.exitCode !== null, 'owned Electron forced exit', 3_000)
    }
  }
  releaseChildProcessHandles(child)
}

async function completeInstallation(cdp, smtpPort, smtpSink) {
  const text = await eventually(async () => {
    const current = await cdp.evaluate('document.body.innerText')
    if (!current.includes('先完成家长与日报配置')) throw new Error('installation screen is not visible')
    return current
  }, 'installation setup')
  if (!text.includes('家长 PIN')) throw new Error('installation PIN step missing')
  const setField = (label, value) => cdp.evaluate(`(() => {
    const node = document.querySelector('[aria-label=${JSON.stringify(label)}]');
    if (!(node instanceof HTMLInputElement || node instanceof HTMLSelectElement)) throw new Error('missing installation field: ${label}');
    const descriptor = Object.getOwnPropertyDescriptor(node instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value');
    descriptor.set.call(node, ${JSON.stringify(value)}); node.dispatchEvent(new Event('input', { bubbles: true })); node.dispatchEvent(new Event('change', { bubbles: true }));
  })()`)
  const click = (label) => cdp.evaluate(`(() => {
    const button = Array.from(document.querySelectorAll('button')).find((item) => !item.disabled && item.textContent?.includes(${JSON.stringify(label)}));
    if (!(button instanceof HTMLButtonElement)) throw new Error('installation action missing: ${label}'); button.click();
  })()`)
  const visible = (label) => eventually(async () => {
    if (!(await cdp.evaluate('document.body.innerText')).includes(label)) throw new Error(`waiting for ${label}`)
    return true
  }, `installation ${label}`)
  await setField('初始化家长PIN', '2468'); await setField('再次输入家长PIN', '2468'); await click('保存 PIN')
  await visible('配置日报 SMTP')
  await setField('初始化SMTP主机', '127.0.0.1'); await setField('初始化SMTP端口', String(smtpPort)); await setField('初始化SMTP加密', 'none')
  await setField('初始化SMTP用户名', smtpSink.username); await setField('初始化SMTP密码', smtpSink.password)
  await setField('初始化SMTP发件地址', 'report@local.test'); await click('保存 SMTP')
  await visible('设置每日报告收件人')
  await setField('初始化收件邮箱', 'setup-parent@local.test'); await click('保存收件人')
  await visible('发送固定测试邮件'); await click('发送并验证')
  await eventually(async () => {
    if (smtpSink.messages.length !== 2 || smtpSink.authentications() !== 2) throw new Error('both installation verification messages not delivered')
    return true
  }, 'installation authenticated SMTP delivery')
  await visible('今天，我们从哪里开始？')
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (args.get('--dry-run')) {
    process.stdout.write(JSON.stringify({ mode: 'dry_run', semantic_mode: 'requires --fixture-dir with approved adult/synthetic WAV manifest', boundary_mode: '--asr-boundary-only uses an in-memory non-speech tone; real local ASR and app path', natural_voice_tested: false }) + '\n')
    return
  }
  if (args.get('--fixture-dir') && args.get('--asr-boundary-only')) throw new Error('Choose either --fixture-dir semantic coverage or --asr-boundary-only.')
  if (args.get('--model-keychain-service') && !args.get('--fixture-dir')) throw new Error('Cloud text acceptance requires approved synthetic speech fixtures.')
  const realtimeService = args.get('--qwen-realtime-keychain-service')?.trim() ?? ''
  const realtimeEndpoint = args.get('--qwen-realtime-endpoint')?.trim() ?? ''
  const dropPublicToolEvent = Boolean(args.get('--drop-public-tool-event'))
  const dropTaskToolEvent = Boolean(args.get('--drop-task-tool-event'))
  if (dropPublicToolEvent && dropTaskToolEvent) throw new Error('Choose one tool-delivery fault scenario.')
  if ((dropPublicToolEvent || dropTaskToolEvent) && !realtimeService) throw new Error('Tool-delivery fault injection requires real realtime acceptance.')
  if (Boolean(realtimeService) !== Boolean(realtimeEndpoint) || (realtimeService && (!args.get('--fixture-dir') || !args.get('--model-keychain-service')))) {
    throw new Error('Realtime voice acceptance requires an endpoint, a Keychain service, cloud text settings and approved synthetic speech fixtures.')
  }
  const electronBin = resolve(args.get('--electron-bin')); const pythonBin = resolve(args.get('--python-bin')); const modelDir = resolve(args.get('--model-dir')); const outputDir = resolve(args.get('--output-dir'))
  for (const path of [electronBin, pythonBin]) if (!existsSync(path)) throw new Error(`Required executable is missing: ${path}`)
  if (!existsSync(modelDir)) throw new Error(`Approved model directory is missing: ${modelDir}`)
  if (!existsSync(join(root, 'out', 'main', 'index.js'))) throw new Error('Run npm run build before Electron acceptance.')
  await mkdir(outputDir, { recursive: true })
  const profileDir = await mkdtemp(join(tmpdir(), 'hsc-electron-voice-agent-'))
  const trialId = args.get('--trial-profile')
  if (trialId && !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(trialId)) throw new Error('Invalid isolated trial profile identity')
  const trialRoot = trialId ? join(dirname(profileDir), 'hsc-trial-profiles', trialId) : null
  if (trialRoot && existsSync(trialRoot)) throw new Error('Refusing to reuse or remove an existing trial profile')
  const semanticFixtures = args.get('--fixture-dir') ? await loadApprovedSemanticFixtures(args.get('--fixture-dir')) : null
  const modelKeychainService = args.get('--model-keychain-service')?.trim() ?? ''
  const boundaryOnly = !semanticFixtures
  const boundaryWav = createNonSpeechBoundaryWav().toString('base64')
  const record = { record_type: 'electron_voice_agent_vertical_e2e_v3', semantic_commands_tested: !boundaryOnly, synthetic_adult_audio: Boolean(semanticFixtures), non_speech_boundary_audio: boundaryOnly, cloud_text_requested: Boolean(modelKeychainService), qwen_realtime_requested: Boolean(realtimeService), raw_audio_stored: false, natural_voice_tested: false, natural_voice_blocker: 'No approved natural-TTS credential was configured.', media_boundary: semanticFixtures ? 'Approved, manifest-hashed adult or synthetic non-child WAV is injected only at getUserMedia; ASR, Agent, tools, UI and SQLite are real.' : 'An in-memory non-speech tone is injected only at getUserMedia; it validates local ASR rejection/no-action behavior and makes no semantic-command claim.', fixture_manifest: semanticFixtures?.metadata ?? null, checks: {} }
  record.fault_scenario = dropPublicToolEvent ? 'drop_one_public_tool_delivery_event' : dropTaskToolEvent ? 'drop_one_task_tool_delivery_event' : null
  const port = await freePort(); let child; let cdp; let smtp; let setup
  try {
    const username = 'synthetic-voice-parent', password = `hsc-voice-${randomUUID()}`
    smtp = { ...createInstallationSmtpSink(username, password), username, password }
    await new Promise((resolvePromise, reject) => smtp.server.once('error', reject).listen(0, '127.0.0.1', resolvePromise))
    child = spawn(electronBin, [root, `--remote-debugging-port=${port}`, ...(trialId ? [`--trial-profile=${trialId}`] : [])], { cwd: root, detached: process.platform !== 'win32', env: { ...sanitizedHarnessEnvironment(process.env), HSC_ELECTRON_USER_DATA_DIR: profileDir, HSC_LOCAL_AGENT_PYTHON: pythonBin, HSC_LOCAL_MODEL_DIR: modelDir, ...(modelKeychainService ? { HSC_MODEL_KEYCHAIN_SERVICE: modelKeychainService } : {}), ...(realtimeService ? { HSC_QWEN_REALTIME_KEYCHAIN_SERVICE: realtimeService, HSC_QWEN_REALTIME_ENDPOINT: realtimeEndpoint } : {}) }, stdio: ['ignore', 'pipe', 'pipe'] })
    // Drain child output so startup logging cannot fill an unread pipe and
    // deadlock the local service. Structured diagnostics remain the authority.
    child.stdout.on('data', () => undefined)
    child.stderr.on('data', () => undefined)
    const page = await eventually(async () => {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); const target = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl)
      if (!target) throw new Error('Electron renderer target not published'); return target
    }, 'Electron CDP target', ELECTRON_RENDERER_STARTUP_TIMEOUT_MS)
    cdp = new Cdp(page.webSocketDebuggerUrl); await cdp.open(); await cdp.send('Page.enable'); await cdp.send('Runtime.enable'); await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: mediaShim })
    await eventually(async () => {
      const runtime = await cdp.evaluate('window.desktop?.getRuntimeConfig()')
      if (!runtime || runtime.status !== 'ready' || !String(runtime.localApiUrl).startsWith('http://127.0.0.1:')) {
        throw new Error(`Electron local runtime is not ready (${runtime?.status ?? 'bridge_pending'})`)
      }
      return true
    }, 'loopback desktop runtime', 70_000)
    await completeInstallation(cdp, smtp.server.address().port, smtp)
    record.checks.installation_gate = { visible_setup_completed: true, authenticated_loopback_messages: smtp.messages.length, external_delivery: false }
    setup = await cdp.evaluate(`(async () => {
      const runtime = await window.desktop.getRuntimeConfig(); const headers = { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken };
      const api = async (path, body) => { const response = await fetch(runtime.localApiUrl + path, { method: 'POST', headers, body: JSON.stringify(body) }); if (!response.ok) throw new Error(path + ':' + response.status); return response.json() };
      const parent = await api('/v1/parent/unlock', { pin: '2468' });
      const privacy = await fetch(runtime.localApiUrl + '/v1/parent/privacy', { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken, 'X-HSC-Parent': parent.parent_token }, body: JSON.stringify({ cloud_model_text_allowed: ${Boolean(modelKeychainService)}, cloud_audio_allowed: ${Boolean(realtimeService)}, raw_media_storage_allowed: false, model_improvement_allowed: false }) });
      if (!privacy.ok) throw new Error('/v1/parent/privacy:' + privacy.status);
      const session = await api('/v1/sessions', { child_id: 'child-primary-4', client_action_id: 'voice-agent-session-001' }); const id = session.session_id;
      await api('/v1/sessions/' + id + '/transition', { expected_state: 'check_in', target_state: 'classroom_recall', reason: 'synthetic acceptance setup', client_action_id: 'voice-agent-transition-001' });
      await api('/v1/sessions/' + id + '/recalls/none', { client_action_id: 'voice-agent-recall-none-001' });
      await api('/v1/sessions/' + id + '/transition', { expected_state: 'classroom_recall', target_state: 'homework_capture', reason: 'synthetic acceptance setup', client_action_id: 'voice-agent-transition-002' });
      await api('/v1/sessions/' + id + '/homework', { subject: '数学', title: '合成验收练习', expected_minutes: 20, client_action_id: 'voice-agent-homework-001' });
      await api('/v1/sessions/' + id + '/plan/generate', { client_action_id: 'voice-agent-plan-001' });
      await api('/v1/sessions/' + id + '/transition', { expected_state: 'plan_review', target_state: 'study', reason: 'synthetic acceptance setup', client_action_id: 'voice-agent-transition-003' });
      const wake = await window.desktop.saveWakeWordSettings({ childId: 'child-primary-4', wakeWord: '小知', inactivitySeconds: 30, enabled: true, updatedAt: new Date().toISOString() });
      await window.desktop.saveVoiceOnboarding({ childId: 'child-primary-4', version: 1, step: 'completed', updatedAt: new Date().toISOString() });
      return { session_id: id, parent_token: parent.parent_token, wake };
    })()`)
    const workspace = () => cdp.evaluate(`(async () => { const runtime = await window.desktop.getRuntimeConfig(); return (await (await fetch(runtime.localApiUrl + '/v1/sessions/${setup.session_id}', { headers: { 'X-HSC-Session': runtime.sessionToken } })).json()) })()`)
    const body = () => cdp.evaluate('document.body.innerText')
    await cdp.evaluate('window.location.reload()')
    await eventually(async () => {
      const state = await cdp.evaluate(`({ panel: Boolean(document.querySelector('[aria-label="语音学习伙伴"]')), text: document.body.innerText })`)
      if (!state.panel && !state.text.includes('继续今天的学习')) throw new Error('neither zero-click session recovery nor resume UI is visible')
      return state
    }, 'zero-click session recovery')
    const resumeState = await eventually(async () => {
      const state = await cdp.evaluate(`(() => ({ panel: Boolean(document.querySelector('[aria-label="语音学习伙伴"]')), resume: Array.from(document.querySelectorAll('button')).some((item) => !item.disabled && item.textContent?.includes('继续学习')) }))()`)
      if (!state.panel && !state.resume) throw new Error('session recovery is still loading')
      return state
    }, 'child workspace or resume action')
    if (!resumeState.panel) {
      await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.includes('继续学习')); if (!(button instanceof HTMLButtonElement)) throw new Error('child resume button missing'); button.click() })()`)
    }
    await eventually(async () => { const panel = await cdp.evaluate(`Boolean(document.querySelector('[aria-label="语音学习伙伴"]'))`); if (!panel) throw new Error('visible child command panel missing'); return panel }, 'child command panel')
    if (realtimeService) {
      await cdp.evaluate(`(() => {
        window.__hscSyntheticRealtimeTurns = []
        const original = window.fetch.bind(window)
        window.fetch = (input, init) => {
          const url = typeof input === 'string' ? input : input.url
          if (url.includes('/v1/sessions/') && url.endsWith('/agent-turn') && typeof init?.body === 'string') {
            try { window.__hscSyntheticRealtimeTurns.push(JSON.parse(init.body).transcript) } catch {}
          }
          return original(input, init)
        }
      })()`)
      const settings = await cdp.evaluate('window.desktop.getQwenRealtimeSettings()')
      if (!settings?.configured || !settings?.enabled) throw new Error('Qwen realtime was not loaded from the named Keychain service')
      const managedVoice = await cdp.evaluate(`(async () => {
        const voice = await window.desktop.getVoiceSettings(); if (!voice.configured) throw new Error('managed recovery speech is not configured');
        const runtime = await window.desktop.getRuntimeConfig(); const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 45000);
        const started = Date.now(); let firstAudioMs = null; let bytes = new Uint8Array();
        try {
          const response = await fetch(runtime.localApiUrl + '/v1/sessions/${setup.session_id}/voice/synthesize/stream', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken }, body: JSON.stringify({ text: '这是合成成人语音的备用声音验收。', client_action_id: 'synthetic-recovery-voice-probe' }), signal: controller.signal });
          if (!response.ok || response.headers.get('X-HSC-TTS-Protocol') !== 'pcm16-frame-v1') throw new Error('recovery TTS protocol:' + response.status);
          const reader = response.body.getReader();
          while (true) {
            const chunk = await reader.read(); if (chunk.done) break;
            if (firstAudioMs === null) firstAudioMs = Date.now() - started;
            if (bytes.length + chunk.value.length > 2000000) { await reader.cancel(); throw new Error('recovery TTS response limit exceeded'); }
            const joined = new Uint8Array(bytes.length + chunk.value.length); joined.set(bytes); joined.set(chunk.value, bytes.length); bytes = joined;
          }
          let offset = 0, frames = 0, samples = 0, nonzero = 0; const view = new DataView(bytes.buffer);
          while (offset + 8 <= bytes.length) {
            const rate = view.getUint32(offset, true), length = view.getUint32(offset + 4, true); offset += 8;
            if (rate !== 24000 || !length || length % 2 || offset + length > bytes.length) throw new Error('recovery PCM framing invalid');
            for (let i = offset; i < offset + length; i += 2) { samples++; if (view.getInt16(i, true)) nonzero++; }
            frames++; offset += length;
          }
          if (offset !== bytes.length || !frames || !nonzero) throw new Error('recovery PCM empty or silent');
          return { provider: voice.provider, voice_id: voice.voiceId, frames, samples, first_audio_ms: firstAudioMs, non_silent_pcm: true, physical_speaker_tested: false };
        } finally { clearTimeout(timeout); }
      })()`)
      record.checks.managed_recovery_voice = managedVoice
      await eventually(async () => {
        const state = await cdp.evaluate(`({ calls: window.__hscVoiceAgentMediaCalls.length, fallback: document.body.innerText.includes('已回退到分段语音') })`)
        if (state.fallback) throw new Error('realtime mode fell back before synthetic speech')
        if (state.calls < 1) throw new Error('realtime mode has not opened the synthetic microphone')
        return state
      }, 'realtime microphone open')
      await cdp.evaluate(`window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { message: '到时间了，我们看一下下一步。' } }))`)
      await eventually(async () => {
        const state = await cdp.evaluate(`(() => ({ conversation: document.querySelector('.wake-word-status span')?.textContent ?? '', playback: document.querySelector('.voice-fallback-note')?.textContent ?? '' }))()`)
        if (!state.conversation.includes('连续对话中')) throw new Error('proactive reminder has not activated continuous conversation')
        if (state.playback.includes('正在尝试') || state.playback.includes('正在准备声音')) throw new Error('proactive reminder is still preparing playback')
        return state
      }, 'proactive reminder settled into conversation')
      await sleep(5_000)
      if (dropTaskToolEvent) await cdp.evaluate('window.__hscDropPublicToolEvent = true')
      const injected = await cdp.evaluate(`window.__hscVoiceAgentInjectWav(${JSON.stringify(semanticFixtures.fixtures.start)})`)
      if (injected < 1) throw new Error('synthetic adult speech was not injected into the realtime microphone')
      await eventually(async () => {
        const state = await workspace(); const text = await body()
        if (text.includes('已回退到分段语音')) throw new Error('realtime mode fell back during the speech turn')
        if (state.plan_items?.[0]?.status !== 'active') throw new Error('Qwen realtime transcript did not start the real plan item')
        return state
      }, 'Qwen realtime to Agent Runtime task persistence', 60_000)
      if (dropTaskToolEvent) {
        const dropped = await cdp.evaluate('window.__hscDroppedPublicToolEvents ?? 0')
        if (dropped !== 1) throw new Error('The task tool-delivery fault was not actually injected')
        record.checks.task_tool_delivery_recovery = { dropped_events: dropped, fault_applied: true,
          real_task_started: true, duplicate_execution_tested: false, normal_task_tool_delivery_proven: false }
      }
      const beforePrivacy = await cdp.evaluate(`(async () => { const runtime = await window.desktop.getRuntimeConfig(); const headers = { 'X-HSC-Session': runtime.sessionToken, 'X-HSC-Parent': ${JSON.stringify(setup.parent_token)} }; const response = await fetch(runtime.localApiUrl + '/v1/parent/children/child-primary-4/model-calls', { headers }); return response.json() })()`)
      if (!beforePrivacy.items?.some((call) => call.provider === 'deepseek' && call.status === 'completed')) throw new Error('realtime transcript did not reach the cloud semantic planner')
      // A local-only task result is deliberately not returned to Qwen. Verify
      // cloud audio with a separate public conversation, never by weakening
      // protected learning/memory source boundaries to satisfy this gate.
      if (!semanticFixtures.fixtures.conversation) throw new Error('Realtime audio acceptance requires a hashed public synthetic conversation fixture')
      await eventually(async () => {
        const summary = await cdp.evaluate('window.desktop.getDiagnosticSummary()')
        const events = (await readFile(summary.logPath, 'utf8')).trim().split('\n').map(line => JSON.parse(line))
        if (!events.some(event => ['voice.realtime.local_reply_boundary', 'voice.realtime.reply_delivery'].includes(event.event))) throw new Error('task reply has not finished its runtime dispatch')
        const ready = await cdp.evaluate(`window.__hscVoiceAgentDestinations.some(entry => entry.track.readyState === 'live' && entry.track.enabled)`)
        if (!ready) throw new Error('local protected playback still owns the microphone gate')
        return true
      }, 'microphone released after protected local reply', 45_000)
      if (dropPublicToolEvent) await cdp.evaluate('window.__hscDropPublicToolEvent = true')
      const publicTurnStartedAt = await cdp.evaluate('Date.now()')
      const conversationInjected = await cdp.evaluate(`window.__hscVoiceAgentInjectWav(${JSON.stringify(semanticFixtures.fixtures.conversation)})`)
      if (!conversationInjected) throw new Error('public synthetic conversation was not injected into an enabled microphone')
      const realtimeEvidence = await eventually(async () => {
        const summary = await cdp.evaluate('window.desktop.getDiagnosticSummary()')
        const events = (await readFile(summary.logPath, 'utf8')).trim().split('\n').map(line => JSON.parse(line))
        const dispatched = events.find(event => event.event === 'voice.realtime.turn_dispatched' && event.details?.route === (dropTaskToolEvent ? 'runtime_recovery' : 'realtime_tool'))
        const firstAudio = events.find(event => event.event === 'voice.realtime.reply_first_audio' && ['realtime_tool', 'runtime_recovery'].includes(event.details?.route))
        const delivery = events.find(event => event.event === 'voice.realtime.reply_delivery' && event.details?.route === firstAudio?.details?.route && event.details?.delivered === true)
        if (!dispatched) throw new Error('the model has not actually dispatched its runtime tool')
        if (!firstAudio || !delivery) throw new Error('no authorized realtime reply playback receipt')
        const firstAudioAt = Date.parse(firstAudio.timestamp)
        const protocol = await cdp.evaluate('window.__hscRealtimeProtocol ?? []')
        const turnProtocol = protocol.filter(item => item.time >= publicTurnStartedAt && item.time <= firstAudioAt)
        const speechEnd = turnProtocol.find(item => item.type === 'input_audio_buffer.speech_stopped')
        const committed = turnProtocol.find(item => item.type === 'conversation.item.input_audio_transcription.completed')
        if (!speechEnd || !committed || !Number.isFinite(firstAudioAt)) throw new Error('first-audio latency lacks actual speech-end and committed-transcript timestamps')
        return { runtime_tool_route: !dropTaskToolEvent, task_dispatch_route: dispatched.details.route, reply_route: firstAudio.details.route, protocol_playback_receipt: firstAudio.details.route === 'realtime_tool', managed_tts_playback_receipt: firstAudio.details.route === 'runtime_recovery', first_audio_ms: firstAudio.details.first_audio_ms,
          agent_runtime_ms: firstAudio.details.agent_runtime_ms,
          provider_speech_end_to_first_audio_ms: firstAudioAt - speechEnd.time,
          transcript_commit_to_first_audio_ms: firstAudioAt - committed.time,
          latency_boundary: 'actual_provider_VAD_and_ASR_events_to_authorized_playback_not_physical_acoustics', speaker_acoustics_tested: false }
      }, 'realtime tool route and authorized playback completion', 100_000)
      if (dropPublicToolEvent) {
        const dropped = await cdp.evaluate('window.__hscDroppedPublicToolEvents ?? 0')
        if (dropped > 1 || realtimeEvidence.reply_route !== 'runtime_recovery' || !realtimeEvidence.managed_tts_playback_receipt) throw new Error('Missing tool delivery did not produce a real managed recovery reply')
        record.checks.tool_delivery_recovery = { dropped_events: dropped, fault_applied: dropped === 1,
          observed_failure: dropped === 1 ? 'injected_tool_event_loss' : 'provider_omitted_runtime_tool',
          actual_runtime_reply: true, managed_playback_completed: true, normal_tool_delivery_proven: false }
      }
      await eventually(async () => {
        const ready = await cdp.evaluate(`({ microphone: window.__hscVoiceAgentDestinations.some(entry => entry.track.readyState === 'live' && entry.track.enabled), conversation: document.querySelector('.wake-word-status span')?.textContent ?? '' })`)
        if (!ready.microphone || !ready.conversation.includes('连续对话中')) throw new Error('cloud reply did not return to continuous listening')
        return ready
      }, 'continuous listening after cloud reply', 15_000)
      const followupStart = await cdp.evaluate('window.__hscSyntheticRealtimeTurns.length')
      const followupInjected = await cdp.evaluate(`window.__hscVoiceAgentInjectWav(${JSON.stringify(semanticFixtures.fixtures.pause)})`)
      if (!followupInjected) throw new Error('no-wake followup was not injected into an enabled microphone')
      const continuousFollowup = await eventually(async () => {
        const state = await workspace()
        const turns = await cdp.evaluate(`window.__hscSyntheticRealtimeTurns.slice(${followupStart})`)
        if (!turns.length || turns.some(turn => turn.includes('小知'))) throw new Error('no-wake followup did not reach Agent Runtime')
        if (state.session?.state !== 'break' || state.plan_items?.[0]?.status !== 'paused') throw new Error('continuous followup did not durably pause the active task')
        return { repeated_wake_word_required: false, runtime_followup_received: true, task_paused: true }
      }, 'no-wake continuous followup and durable tool action', 60_000)
      if (trialRoot) {
        const runtime = await cdp.evaluate('window.desktop.getRuntimeConfig()')
        const summary = await cdp.evaluate('window.desktop.getDiagnosticSummary()')
        if (runtime.dataScope !== 'trial' || resolve(summary.rootPath) !== resolve(trialRoot, 'family-data', 'diagnostics')) throw new Error('trial runtime storage escaped its named profile')
        const marker = JSON.parse(await readFile(join(trialRoot, 'trial-profile.json'), 'utf8'))
        if (marker.profileId !== trialId) throw new Error('trial profile identity was not durable')
        let rejected = false
        try { await cdp.evaluate('window.desktop.setAutoLaunch(true)') } catch { rejected = true }
        if (!rejected) throw new Error('trial runtime allowed formal automatic startup')
        record.checks.trial_isolation = { data_scope: 'trial', profile_marker_persisted: true,
          separate_storage: true, formal_auto_launch_rejected: true }
      }
      await cdp.evaluate(`window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))`)
      await eventually(async () => { const state = await workspace(); if (state.sensor_release?.active_leases !== 0) throw new Error('realtime microphone lease remained active after privacy pause'); return state }, 'realtime lease release')
      const restarted = await cdp.evaluate('window.desktop.restartLocalService()')
      if (restarted.status !== 'ready') throw new Error('local service did not recover after the realtime task')
      await cdp.evaluate('window.location.reload()')
      const recovered = await eventually(async () => {
        const state = await cdp.evaluate(`(() => ({ panel: Boolean(document.querySelector('[aria-label="语音学习伙伴"]')), resume: Array.from(document.querySelectorAll('button')).some((item) => !item.disabled && item.textContent?.includes('继续学习')) }))()`)
        if (!state.panel && !state.resume) throw new Error('child workspace has not recovered after local service restart')
        return state
      }, 'realtime child workspace recovery')
      if (!recovered.panel) await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => !item.disabled && item.textContent?.includes('继续学习')); if (!(button instanceof HTMLButtonElement)) throw new Error('resume action missing'); button.click() })()`)
      await eventually(async () => {
        const state = await workspace()
        if (state.session?.state !== 'break' || state.plan_items?.[0]?.status !== 'paused') throw new Error('continuous followup task did not survive local service restart')
        if (state.sensor_release?.active_leases !== 0) throw new Error('privacy-released microphone lease revived after restart')
        return state
      }, 'realtime task and privacy recovery')
      const visual = await screenshot(cdp, join(outputDir, 'electron-qwen-realtime-agent.png'))
      record.checks.realtime_agent_tool = { ...realtimeEvidence, continuous_followup: continuousFollowup, synthetic_speech_injected: true, qwen_realtime_configured: true, active_task_persisted: true, deepseek_semantic_completed: true, privacy_released_microphone: true, survived_local_service_restart: true, no_segmented_fallback: true, visible_evidence: visual.captured }
      record.status = visual.captured ? 'passed' : 'visual_evidence_missing'
      await writeFile(join(outputDir, 'electron-voice-agent-evidence.json'), JSON.stringify(record, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
      process.stdout.write(JSON.stringify({ status: record.status, outputDir, checks: record.checks }) + '\n')
      if (!visual.captured) process.exitCode = 2
      return
    }
    await eventually(async () => { const standby = await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .hands-free-control button'); return button instanceof HTMLButtonElement && button.textContent?.includes('正在听唤醒词 · 小知') })()`); if (!standby) throw new Error('saved wake word did not auto-start local standby'); return standby }, 'wake-word standby')
    const persistedWake = await cdp.evaluate(`window.desktop.getWakeWordSettings('child-primary-4')`)
    if (persistedWake?.wakeWord !== '小知' || persistedWake?.inactivitySeconds !== 30) throw new Error('wake word did not persist through renderer reload')
    const queue = async (wav) => cdp.evaluate(`window.__hscVoiceAgentWavQueue = [${JSON.stringify(wav)}]`)
    const startRecording = () => cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .local-input-control:not(.hands-free-control) button'); if (!(button instanceof HTMLButtonElement)) throw new Error('manual voice control missing'); button.click() })()`)
    const stopRecording = () => cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .local-input-control:not(.hands-free-control) button'); if (!(button instanceof HTMLButtonElement) || !button.textContent?.includes('结束并识别')) throw new Error('manual voice stop control missing'); button.click() })()`)
    const prepareManualCapture = async (label) => {
      const handsFreeActive = await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .hands-free-control button'); return button instanceof HTMLButtonElement && /正在听|正在处理|取消打开麦克风/.test(button.textContent ?? '') })()`)
      if (handsFreeActive) await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.click()`)
      await eventually(async () => { const enabled = await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .local-input-control:not(.hands-free-control) button'); return button instanceof HTMLButtonElement && !button.disabled })()`); if (!enabled) throw new Error('manual microphone control is still processing the prior turn'); return enabled }, `${label} microphone ready`)
    }
    const speak = async (name, label) => {
      await queue(semanticFixtures.fixtures[name]); await prepareManualCapture(label); await startRecording()
      await eventually(async () => {
        const state = await cdp.evaluate(`(() => ({ button: document.querySelector('.agent-voice-controls > .local-input-control:not(.hands-free-control) button')?.textContent?.trim() ?? '', error: document.querySelector('.agent-voice-controls > .local-input-control:not(.hands-free-control) span')?.textContent ?? '', mediaCalls: window.__hscVoiceAgentMediaCalls.length, queuedAudio: window.__hscVoiceAgentWavQueue?.length ?? 0 }))()`)
        if (!state.button.includes('结束并识别')) throw new Error(`microphone did not visibly open: ${JSON.stringify(state)}`)
        return state
      }, `${label} open`)
      await sleep(3_200); await stopRecording()
    }
    const typeAndEnter = async (text) => {
      await cdp.evaluate(`(() => {
        const input = document.querySelector('input[aria-label="对学习伙伴说"]');
        if (!(input instanceof HTMLInputElement)) throw new Error('agent text fallback input missing');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, ${JSON.stringify(text)});
        input.dispatchEvent(new Event('input', { bubbles: true }));
      })()`)
      await eventually(async () => { const value = await cdp.evaluate(`document.querySelector('input[aria-label="对学习伙伴说"]')?.value`); if (value !== text) throw new Error('CDP text was not accepted by the visible input'); return value }, 'visible supplemental input')
      await eventually(async () => { const ready = await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => /^(加入队列|已排队 \\d+ 句)$/.test(item.textContent?.trim() ?? '')); return button instanceof HTMLButtonElement && !button.disabled })()`); if (!ready) throw new Error('visible ordered-input queue did not enable'); return ready }, 'ordered-input queue')
      await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => /^(加入队列|已排队 \\d+ 句)$/.test(item.textContent?.trim() ?? '')); if (!(button instanceof HTMLButtonElement)) throw new Error('visible ordered-input queue missing'); button.click() })()`)
    }

    if (boundaryOnly) {
      await eventually(async () => {
        const standby = await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .hands-free-control button'); const avatar = document.querySelector('.agent-command-panel .companion-avatar'); return button instanceof HTMLButtonElement && button.textContent?.includes('正在听唤醒词 · 小知') && avatar?.getAttribute('aria-label') === '学习伙伴：随时等你' })()`)
        if (!standby) throw new Error('wake standby and avatar did not enter the waiting state together')
        return standby
      }, 'wake standby state')
      await cdp.evaluate(`window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { message: '到时间了，我们看一下下一步。' } }))`)
      await eventually(async () => { const active = await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .hands-free-control button'); const avatar = document.querySelector('.agent-command-panel .companion-avatar'); return button instanceof HTMLButtonElement && button.textContent?.includes('连续对话') && avatar?.getAttribute('aria-label') === '学习伙伴：在认真听' })()`); if (!active) throw new Error('proactive reminder did not enter conversation mode'); return active }, 'proactive conversation activation')
      const listeningVisual = await screenshot(cdp, join(outputDir, 'electron-voice-agent-listening.png'))
      await eventually(async () => { const standby = await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.textContent?.includes('正在听唤醒词 · 小知')`); if (!standby) throw new Error('conversation silence did not return to wake standby'); return standby }, 'conversation inactivity fallback', 36_000)
      await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.click()`)
      await eventually(async () => { const stopped = await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.textContent?.trim() === '启动唤醒监听'`); if (!stopped) throw new Error('wake standby did not release before boundary restart'); return stopped }, 'non-speech boundary release')
      const boundaryMediaBefore = await cdp.evaluate(`window.__hscVoiceAgentMediaCalls.length`)
      await queue(boundaryWav)
      await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.click()`)
      await eventually(async () => { const opened = await cdp.evaluate(`(() => ({ listening: document.querySelector('.agent-voice-controls .hands-free-control button')?.textContent?.includes('正在听唤醒词 · 小知'), calls: window.__hscVoiceAgentMediaCalls.length }))()`); if (!opened.listening || opened.calls <= boundaryMediaBefore) throw new Error('non-speech boundary did not open a fresh wake capture'); return opened }, 'non-speech boundary open')
      await sleep(4_500)
      await eventually(async () => { const settled = await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.textContent?.includes('正在听唤醒词 · 小知')`); if (!settled) throw new Error('non-speech boundary capture did not settle'); return settled }, 'non-speech boundary settle')
      await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.click()`)
      await eventually(async () => { const enabled = await cdp.evaluate(`(() => { const button = document.querySelector('.agent-voice-controls .local-input-control:not(.hands-free-control) button'); return button instanceof HTMLButtonElement && !button.disabled })()`); if (!enabled) throw new Error('boundary capture did not release its microphone owner'); return enabled }, 'non-speech boundary release owner')
      const afterBoundary = await workspace()
      if (afterBoundary.session?.state !== 'study' || afterBoundary.plan_items?.some((item) => ['active', 'paused', 'completed'].includes(item.status))) throw new Error('non-speech ASR boundary unexpectedly changed a task state')
      const localOnly = await cdp.evaluate(`(async () => { const runtime = await window.desktop.getRuntimeConfig(); const headers = { 'X-HSC-Session': runtime.sessionToken, 'X-HSC-Parent': ${JSON.stringify(setup.parent_token)} }; const health = await (await fetch(runtime.localApiUrl + '/v1/parent/system-health', { headers })).json(); const calls = await (await fetch(runtime.localApiUrl + '/v1/parent/children/child-primary-4/model-calls', { headers })).json(); return { health, calls } })()`)
      if (localOnly.health.raw_media_files_stored !== 0 || localOnly.calls.items.length !== 0 || afterBoundary.sensor_release.active_leases !== 0 || afterBoundary.sensor_release.released_leases < 2) throw new Error('voice runtime boundary violated local-only media or lease constraints')
      record.checks.asr_non_speech_boundary = { wake_capture_opened: true, capture_settled: true, semantic_command_claimed: false, task_unchanged: true, raw_media_files_stored: 0, cloud_model_calls: 0, active_sensor_leases: 0, released_sensor_leases: afterBoundary.sensor_release.released_leases }

      const mediaBeforePrivacy = await cdp.evaluate(`window.__hscVoiceAgentMediaCalls.length`)
      await cdp.evaluate(`window.dispatchEvent(new CustomEvent('hsc-privacy-pause'))`)
      await eventually(async () => { const ready = await cdp.evaluate(`document.querySelector('.agent-voice-controls .hands-free-control button')?.textContent?.trim() === '启动唤醒监听'`); if (!ready) throw new Error('privacy pause did not stop wake capture'); return ready }, 'wake privacy stop')
      await cdp.evaluate(`window.dispatchEvent(new CustomEvent('hsc-agent-intervention', { detail: { message: '这条提醒不能重开已暂停的麦克风。' } }))`)
      await sleep(800)
      const mediaAfterPrivacy = await cdp.evaluate(`window.__hscVoiceAgentMediaCalls.length`)
      if (mediaAfterPrivacy !== mediaBeforePrivacy) throw new Error('proactive reminder reopened microphone after privacy pause')
      record.checks.hands_free_runtime_state = { wake_word_persisted: true, capture_opened: true, wake_standby_visible: true, proactive_entered_conversation: true, inactivity_returned_to_standby: true, privacy_pause_blocked_reopen: true, visual_evidence: listeningVisual.captured ? 'electron-voice-agent-listening.png' : null }
      const visual = await screenshot(cdp, join(outputDir, 'electron-voice-agent-boundary.png'))
      if (visual.captured) record.screenshot = 'electron-voice-agent-boundary.png'
      record.visual_evidence = visual
      record.status = visual.captured ? 'passed' : 'visual_evidence_missing'
      await writeFile(join(outputDir, 'electron-voice-agent-evidence.json'), JSON.stringify(record, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
      process.stdout.write(JSON.stringify({ status: record.status, outputDir, semantic_commands_tested: false, checks: record.checks }) + '\n')
      if (record.status === 'visual_evidence_missing') process.exitCode = 2
      return
    }

    await cdp.evaluate(`(() => {
      const button = document.querySelector('.agent-voice-controls .hands-free-control button')
      if (!(button instanceof HTMLButtonElement) || !button.textContent?.includes('正在听')) throw new Error('wake standby stop control missing before semantic fixtures')
      button.click()
    })()`)
    await eventually(async () => {
      const enabled = await cdp.evaluate(`(() => {
        const button = document.querySelector('.agent-voice-controls .local-input-control:not(.hands-free-control) button')
        return button instanceof HTMLButtonElement && !button.disabled
      })()`)
      if (!enabled) throw new Error('wake standby has not released the microphone')
      return enabled
    }, 'semantic fixture microphone handoff')

    // Delay only the real start-tool request to create an observable processing
    // interval. The recorder is correctly locked then; the visible text fallback
    // accepts two supplements and the product keeps both in original order.
    await cdp.evaluate(`(() => { const original = window.fetch.bind(window); window.fetch = async (input, init) => { const url = String(input); if (/\\/plan-items\\/[^/]+\\/start$/.test(url)) await new Promise((resolve) => setTimeout(resolve, 2500)); return original(input, init) } })()`)
    await cdp.evaluate(`(() => { const original = window.fetch.bind(window); window.__hscSyntheticAsrTranscripts = []; window.fetch = async (input, init) => { const response = await original(input, init); if (String(input).endsWith('/asr')) response.clone().json().then((value) => window.__hscSyntheticAsrTranscripts.push(value.transcript ?? '')).catch(() => {}); return response } })()`)
    await speak('start', 'start task')
    await eventually(async () => { const processing = await cdp.evaluate(`Boolean(Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.trim() === '加入队列'))`); if (!processing) throw new Error('agent tool was not processing'); return processing }, 'agent processing')
    await typeAndEnter('不要处理这句')
    await eventually(async () => { const text = await body(); if (!text.includes('已按顺序排队1句')) throw new Error('first supplemental input was not accepted in order'); return text }, 'first supplemental input')
    await typeAndEnter('今天计划')
    await eventually(async () => { const current = await workspace(); if (current.plan_items?.[0]?.status !== 'active') throw new Error('real start tool did not persist active task'); const text = await body(); if (!text.includes('今天共有')) throw new Error('ordered queued supplements did not reach the final visible reply'); return current }, 'start tool and ordered supplements')
    record.checks.start_and_ordered_processing_inputs = { asr_transcripted: true, real_tool_persisted: true, supplements_preserved_in_order: true, final_visible: true, supplemental_input_boundary: 'visible text fallback while the manual microphone control is intentionally locked during the real tool call' }

    await speak('pause', 'pause task')
    await eventually(async () => { const current = await workspace(); if (current.session?.state !== 'break' || current.plan_items?.[0]?.status !== 'paused') throw new Error('real break tool did not pause task'); return current }, 'pause persistence')
    await speak('resume', 'resume task')
    await eventually(async () => { const current = await workspace(); if (current.session?.state !== 'study' || current.plan_items?.[0]?.status !== 'active') throw new Error('real resume tool did not restore task'); return current }, 'resume persistence')
    await speak('next', 'next-step question')
    await eventually(async () => { const text = await body(); if (!text.includes('今天共有')) throw new Error('next-step answer was not visibly rendered'); return text }, 'next-step visible answer')
    record.checks.asr_agent_tools = { start: 'active', pause: 'break_and_paused', resume: 'study_and_active', next_step_visible: true }

    await cdp.evaluate(`(() => { const button = document.querySelector('button[title="停止伙伴播报"]'); if (!(button instanceof HTMLButtonElement)) throw new Error('visible speech interrupt control missing'); button.click() })()`)
    await eventually(async () => { const text = await body(); if (!text.includes('已停止播报；回复文字仍保留。')) throw new Error('speech interruption was not visibly labelled'); return text }, 'visible speech interruption')
    record.checks.interrupt_and_tts_fallback = { interrupt_visible: true, natural_tts_configured: false, text_retained: true, natural_voice_claimed: false }

    await speak('privacy', 'privacy pause')
    await eventually(async () => { const current = await workspace(); if (current.session?.observation_status !== 'paused') throw new Error('privacy pause did not persist'); return current }, 'privacy pause persistence')
    const beforeRestart = await cdp.evaluate(`(async () => { const runtime = await window.desktop.getRuntimeConfig(); const headers = { 'X-HSC-Session': runtime.sessionToken, 'X-HSC-Parent': ${JSON.stringify(setup.parent_token)} }; const checked = async (path) => { const response = await fetch(runtime.localApiUrl + path, { headers }); const payload = await response.json(); if (!response.ok) throw new Error(path + ':' + response.status + ':' + (payload.message || payload.detail || 'request failed')); return payload }; const health = await checked('/v1/parent/system-health'); const calls = await checked('/v1/parent/children/child-primary-4/model-calls'); const boot = await checked('/v1/bootstrap'); return { health, calls, boot } })()`)
    if (beforeRestart.health.raw_media_files_stored !== 0) throw new Error(`privacy boundary failed: raw=${beforeRestart.health.raw_media_files_stored}`)
    if (modelKeychainService && !beforeRestart.calls.items.some((call) => call.provider === 'deepseek' && call.status === 'completed')) throw new Error('cloud text mode did not record a completed DeepSeek call')
    if (!modelKeychainService && beforeRestart.calls.items.length !== 0) throw new Error('local-only semantic mode unexpectedly called a cloud model')
    const finalWorkspace = await workspace()
    if (finalWorkspace.sensor_release.active_leases !== 0 || finalWorkspace.sensor_release.released_leases < 5) throw new Error('microphone leases were not all released')
    record.checks.privacy_and_model_routing = { observation_paused: true, raw_media_files_stored: 0, cloud_model_calls: beforeRestart.calls.items.length, completed_deepseek_calls: beforeRestart.calls.items.filter((call) => call.provider === 'deepseek' && call.status === 'completed').length, cloud_model_configured: beforeRestart.boot.model.configured === true, active_sensor_leases: 0, released_sensor_leases: finalWorkspace.sensor_release.released_leases }

    const restarted = await cdp.evaluate('window.desktop.restartLocalService()')
    if (restarted.status !== 'ready') throw new Error('local agent restart failed')
    await cdp.evaluate('window.location.reload()')
    await eventually(async () => {
      const recovered = await cdp.evaluate(`({ panel: Boolean(document.querySelector('[aria-label="语音学习伙伴"]')), resumable: document.body.innerText.includes('继续今天的学习') })`)
      if (!recovered.panel && !recovered.resumable) throw new Error('session did not recover after restart')
      return recovered
    }, 'restart home')
    const restartState = await eventually(async () => {
      const state = await cdp.evaluate(`(() => ({ panel: Boolean(document.querySelector('[aria-label="语音学习伙伴"]')), resume: Array.from(document.querySelectorAll('button')).some((item) => !item.disabled && item.textContent?.includes('继续学习')) }))()`)
      if (!state.panel && !state.resume) throw new Error('restart recovery is still loading')
      return state
    }, 'restarted child workspace or resume action')
    if (!restartState.panel) {
      await cdp.evaluate(`(() => { const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.includes('继续学习')); if (!(button instanceof HTMLButtonElement)) throw new Error('restart resume button missing'); button.click() })()`)
    }
    const recoveredWorkspace = await eventually(async () => { const current = await workspace(); if (current.session?.state !== 'study' || current.session?.observation_status !== 'paused' || !['active', 'paused'].includes(current.plan_items?.[0]?.status)) throw new Error(`persisted tool result did not survive restart: state=${current.session?.state}, observation=${current.session?.observation_status}, task=${current.plan_items?.[0]?.status}`); return current }, 'restart durable workspace')
    await cdp.evaluate(`(() => document.querySelector('.agent-command-panel')?.scrollIntoView({ block: 'center' }))()`); await sleep(150)
    const visual = await screenshot(cdp, join(outputDir, 'electron-voice-agent-synthetic.png'))
    record.checks.restart_recovery = { workspace_persisted: true, privacy_pause_persisted: true, task_status_after_safe_restart: recoveredWorkspace.plan_items[0].status, task_persisted: true }
    if (visual.captured) record.screenshot = 'electron-voice-agent-synthetic.png'
    record.visual_evidence = visual
    record.status = visual.captured ? 'passed' : 'visual_evidence_missing'
    await writeFile(join(outputDir, 'electron-voice-agent-evidence.json'), JSON.stringify(record, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
    process.stdout.write(JSON.stringify({ status: record.status, outputDir, checks: record.checks }) + '\n')
    if (record.status === 'visual_evidence_missing') process.exitCode = 2
  } catch (error) {
    record.error = error instanceof Error ? error.message : String(error)
    try {
      const runtime = await cdp?.evaluate('window.desktop?.getRuntimeConfig()')
      record.failure_runtime = { status: runtime?.status, error: runtime?.error }
      const diagnostics = await cdp?.evaluate('window.desktop?.getDiagnosticSummary?.()')
      record.failure_diagnostic_summary = diagnostics
      if (diagnostics?.logPath) {
        const events = (await readFile(diagnostics.logPath, 'utf8')).trim().split('\n').map(line => JSON.parse(line))
        record.failure_realtime_events = events.filter(event => event.event?.startsWith('voice.realtime.'))
          .map(({ timestamp, event, level, details }) => ({ timestamp, event, level, details })).slice(-50)
      }
    } catch {}
    try { record.failure_ui_text = String(await cdp?.evaluate('document.body.innerText')).slice(0, 8_000) } catch {}
    if (semanticFixtures) try { record.synthetic_asr_transcripts = await cdp?.evaluate('window.__hscSyntheticAsrTranscripts ?? []') } catch {}
    if (realtimeService) try { record.synthetic_realtime_turns = await cdp?.evaluate('window.__hscSyntheticRealtimeTurns ?? []') } catch {}
    if (realtimeService) try { record.failure_protocol_events = await cdp?.evaluate('window.__hscRealtimeProtocol ?? []') } catch {}
    if (setup?.parent_token) try {
      record.failure_model_calls = await cdp?.evaluate(`(async () => {
        const runtime = await window.desktop.getRuntimeConfig()
        const response = await fetch(runtime.localApiUrl + '/v1/parent/children/child-primary-4/model-calls', {
          headers: { 'X-HSC-Session': runtime.sessionToken, 'X-HSC-Parent': ${JSON.stringify(setup.parent_token)} }
        })
        if (!response.ok) return { status: response.status }
        const payload = await response.json()
        return (payload.items ?? []).map(({ purpose, provider, model, status, error_code, latency_ms }) => ({ purpose, provider, model, status, error_code, latency_ms }))
      })()`)
    } catch {}
    await writeFile(join(outputDir, 'electron-voice-agent-evidence.json'), JSON.stringify(record, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 }).catch(() => undefined)
    throw error
  } finally {
    // Closing the inspected Browser target can terminate the Node process before
    // its rejected promise reaches the exit handler on some Electron builds.
    // Terminate only the spawned Electron child so failure records reliably exit
    // non-zero and no result can be mistaken for passing evidence.
    await stopElectron(child, cdp)
    if (smtp) await new Promise((resolvePromise, reject) => smtp.server.close((error) => error ? reject(error) : resolvePromise()))
    await rm(profileDir, { recursive: true, force: true })
    if (trialRoot) await rm(trialRoot, { recursive: true, force: true })
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => { console.error(`[electron-voice-agent-e2e] ${error instanceof Error ? error.message : String(error)}`); process.exit(1) })
}
