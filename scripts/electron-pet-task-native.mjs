#!/usr/bin/env node
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join, resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS } from './electron-owned-process.mjs'
import { stopOwned, screenshot, root, outputDir, pythonBin, owned, diagnosticSecrets, run,
  captureOwnedFailure, redactHarnessDiagnostic } from './electron-parent-report-delivery-e2e.mjs'

const args = new Map(process.argv.slice(2).reduce((all, v, i, a) => i % 2 ? all : [...all, [v, a[i + 1]]], []))
if (args.get('--run-approved') !== 'yes') throw new Error('Requires explicit --run-approved yes after backend freeze and main build')
const skill = args.get('--skill') ?? 'reading', mode = args.get('--mode') ?? 'fresh'
const controlledVoice = args.get('--controlled-voice') === 'yes'
const activityNative = args.get('--activity-native') === 'yes'
const recoveryNative = args.get('--recovery-native') === 'yes'
if (recoveryNative && (!controlledVoice || activityNative || mode !== 'fresh')) throw new Error('Recovery requires a separate controlled fresh fixture')
if (activityNative && (!controlledVoice || mode !== 'fresh')) throw new Error('Activity journey requires explicit controlled synthetic voice')
if (!['reading', 'listening'].includes(skill) || !['fresh', 'awaiting'].includes(mode)) throw new Error('Invalid case')
const electronBin = resolve(args.get('--electron-bin'))
const reused = args.get('--fixture-dir')
const recoveryReadOnly = args.get('--recovery-readonly') === 'yes'
if (recoveryReadOnly && (!reused || !recoveryNative)) throw new Error('Recovery screenshots require the retained recovery fixture')
const receiptAuditOnly = args.get('--receipt-audit-only') === 'yes'
const continueStarted = args.get('--continue-started') === 'yes'
if (continueStarted && (!reused || receiptAuditOnly)) throw new Error('Continue requires a retained started fixture, outside receipt-only audit')
if (receiptAuditOnly && !reused) throw new Error('Receipt audit requires the retained fixture; never seed a replacement')
if (reused && !basename(reused).startsWith('hsc-pet-task-native-')) throw new Error('Only owned PET fixtures may be resumed')
const temporaryRoot = reused ? resolve(reused) : await mkdtemp(join(tmpdir(), 'hsc-pet-task-native-'))
const dataDir = join(temporaryRoot, 'data'), profile = join(temporaryRoot, 'profile'), databasePath = join(dataDir, 'pet-task.db')
const seedPath = join(temporaryRoot, 'seed.json'), checkpointPath = join(temporaryRoot, 'checkpoint.json')
await mkdir(outputDir, { recursive: true }); await mkdir(profile, { recursive: true })
const environment = parentReportDeliveryEnvironment({ root, pythonBin, dataDir, databasePath, profile })
const delay = ms => new Promise(done => setTimeout(done, ms))
const save = (path, value) => writeFile(path, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 })
async function until(read, label, milliseconds = 45000) {
  const deadline = Date.now() + milliseconds
  let last
  while (Date.now() < deadline) { try { const value = await read(); if (value) return value } catch (error) { last = error } await delay(100) }
  throw new Error(`${label}: timed out${last ? ` (${last.message})` : ''}`)
}
class Cdp {
  constructor(url) { this.socket = new WebSocket(url); this.pending = new Map(); this.nextId = 0; this.events = [] }
  async open() {
    await new Promise((yes, no) => { this.socket.addEventListener('open', yes, { once: true }); this.socket.addEventListener('error', no, { once: true }) })
    this.socket.addEventListener('message', event => {
      const m = JSON.parse(String(event.data)), item = this.pending.get(m.id)
      if (!item) { if (m.method) this.events.push(m); return }
      this.pending.delete(m.id); m.error ? item.reject(new Error(m.error.message)) : item.resolve(m.result)
    })
  }
  send(method, params = {}) { const id = ++this.nextId, response = awaitCdpResponse(this.pending, id, method, CDP_COMMAND_TIMEOUT_MS); this.socket.send(JSON.stringify({ id, method, params })); return response }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
    return result.result.value
  }
  close() { this.socket.close() }
}

// Real fetch responses are observed, never substituted. Only microphone input
// is an oscillator-generated stream; no recognized answer is manufactured.
const instrumentation = `(() => {
  const p = window.__petProof = { requests: [], updates: [], speech: [], mediaOpens: 0, held: [] };
  const raw = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (!['localhost', '127.0.0.1', '[::1]', '::1'].includes(url.hostname)) throw new Error('NATIVE_EXTERNAL_REQUEST_FORBIDDEN');
    let body; try { body = JSON.parse(init?.body ?? '{}') } catch { body = {} }
    const entry = { path: url.pathname, method: init?.method ?? 'GET',
      capture_reference: body.capture_reference, client_action_id: body.client_action_id,
      pending_evaluation_turn_id: body.pending_evaluation_turn_id, local_recovery_only: body.local_recovery_only,
      voice_input_id: body.voice_input_id, final_response: body.final_response, receipt: body.result };
    p.requests.push(entry);
    if (url.pathname.includes('/voice/synthesize') || url.pathname.endsWith('/voice/playback')) p.speech.push(body.text ?? '');
    const response = await raw(input, init); entry.status = response.status;
    if (['/execute','/agent-turn','/asr','/activity-focus','/execution-status','/control-admission','/outcome'].some(end=>url.pathname.endsWith(end)) || url.pathname.includes('/diagnostic-prompt') || url.pathname.includes('/pet-media/prompt/')) {
      entry.result = await response.clone().json().catch(() => null);
    }
    if (url.pathname.endsWith('/execution-status') && sessionStorage.getItem('hsc-native-hold-evaluation')==='yes') {
      await new Promise(resolve=>p.held.push(resolve));
    }
    return response;
  };
  window.addEventListener('hsc-diagnostic-prompt-updated', event => p.updates.push(event.detail));
  if (window.speechSynthesis) {
    const speak = speechSynthesis.speak.bind(speechSynthesis);
    speechSynthesis.speak = value => { p.speech.push(value.text); return speak(value) };
  }
  const sources = [];
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: async constraints => {
    if (constraints.video) throw new DOMException('No camera in synthetic acceptance', 'NotAllowedError');
    const context = new AudioContext(), destination = context.createMediaStreamDestination();
    const source = context.createOscillator(), gain = context.createGain();
    source.frequency.value = 440; gain.gain.value = 0; source.connect(gain); gain.connect(destination); source.start();
    sources.push({context, gain}); p.mediaOpens++;
    return destination.stream;
  }});
  window.__petCaptureBurst = async () => {
    if (!sources.length) throw new Error('Actual PcmRecorder has not opened synthetic input');
    for (const {context, gain} of sources) {
      await context.resume(); const t = context.currentTime;
      gain.gain.setValueAtTime(0, t); gain.gain.setValueAtTime(0.12, t + 0.4);
      gain.gain.setValueAtTime(0, t + 1.8);
    }
  };
})()`

async function launch() {
  const server = createServer(); await new Promise(done => server.listen(0, '127.0.0.1', done))
  const port = server.address().port; await new Promise(done => server.close(done))
  let backend = null
  const launchEnvironment = { ...environment }
  if (controlledVoice) {
    const socket = createServer(); await new Promise(done => socket.listen(0, '127.0.0.1', done))
    const httpPort = socket.address().port; await new Promise(done => socket.close(done))
    launchEnvironment.HSC_LOCAL_API_URL = `http://127.0.0.1:${httpPort}`
    launchEnvironment.HSC_SESSION_TOKEN = crypto.randomUUID()
    diagnosticSecrets.add(launchEnvironment.HSC_SESSION_TOKEN)
    let backendLogs = ''
    const process = spawn(pythonBin, [join(root, 'scripts/serve-pet-activity-native.py'), '--data-dir', dataDir, '--port', String(httpPort)],
      { cwd: root, detached: true, stdio: ['ignore', 'pipe', 'pipe'], env: launchEnvironment })
    backend = { child: process, cdp: null, phase: 'controlled-synthetic-host', logs: () => backendLogs }
    owned.set(process, backend)
    process.stdout.on('data', b => { backendLogs += b }); process.stderr.on('data', b => { backendLogs += b })
    console.log(JSON.stringify({ phase: 'synthetic-host-starting', pid: process.pid, port: httpPort }))
    await until(async () => {
      const response = await fetch(`${launchEnvironment.HSC_LOCAL_API_URL}/v1/health`, { headers: { 'X-HSC-Session': launchEnvironment.HSC_SESSION_TOKEN } })
      if (!response.ok) throw new Error(`Health HTTP ${response.status}`)
      return true
    }, 'actual synthetic HTTP host')
    console.log(JSON.stringify({ phase: 'synthetic-host-ready', pid: process.pid }))
  }
  let logs = ''
  const child = spawn(electronBin, [root, `--remote-debugging-port=${port}`, '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
    { cwd: root, detached: true, stdio: ['ignore', 'pipe', 'pipe'], env: launchEnvironment })
  const app = { child, backend, cdp: null, cdpPort: port, phase: 'cdp-page', runtime: null, logs: () => logs }
  owned.set(child, app)
  child.stdout.on('data', b => { logs += b }); child.stderr.on('data', b => { logs += b })
  child.once('error', error => { app.spawnError = error.message })
  const target = await until(async () => (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(row => row.type === 'page' && row.webSocketDebuggerUrl), 'owned Electron page')
  app.cdp = new Cdp(target.webSocketDebuggerUrl); await app.cdp.open()
  await app.cdp.send('Page.enable'); await app.cdp.send('Runtime.enable')
  await app.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: instrumentation })
  await until(async () => {
    const runtime = await app.cdp.evaluate('window.desktop?.getRuntimeConfig()')
    if (runtime?.sessionToken) diagnosticSecrets.add(runtime.sessionToken)
    app.runtime = { status: runtime?.status, localApiUrl: runtime?.localApiUrl }
    return runtime?.status === 'ready'
  }, 'isolated actual backend')
  await app.cdp.evaluate(`(async () => {
    const now = new Date().toISOString();
    await desktop.saveWakeWordSettings({childId:'child-primary-4',wakeWord:'小知',enabled:${!recoveryNative},inactivitySeconds:60,updatedAt:now});
    await desktop.saveVoiceOnboarding({childId:'child-primary-4',version:1,step:'completed',updatedAt:now});
  })()`)
  await app.cdp.send('Page.reload', { ignoreCache: true })
  await until(() => app.cdp.evaluate("!!document.querySelector('.session-shell .hands-free-control')"), 'actual SessionWorkspace')
  app.phase = 'ready'
  console.log(JSON.stringify({ phase: 'electron-workspace-ready', pid: child.pid }))
  return app
}
async function say(app, text) {
  await until(() => app.cdp.evaluate(`(() => {
    const node = document.querySelector('.session-shell .hands-free-control'); if (!node) return false;
    let fiber = node[Object.keys(node).find(key => key.startsWith('__reactFiber$'))];
    for (; fiber; fiber = fiber.return) {
      if (typeof fiber.memoizedProps?.onTranscript === 'function' && fiber.memoizedProps.sessionId) {
        window.__petVoiceDone = false; window.__petVoiceError = null;
        Promise.resolve(fiber.memoizedProps.onTranscript(${JSON.stringify(text)})).then(() => { window.__petVoiceDone = true }, error => { window.__petVoiceError = error.message; window.__petVoiceDone = true });
        return true;
      }
    } return false;
  })()`), 'current recognized-voice callback')
  await until(() => app.cdp.evaluate('window.__petVoiceDone'), 'actual voice command completion')
  const error = await app.cdp.evaluate('window.__petVoiceError')
  if (error) throw new Error(error)
}
async function api(app, path, body, parent = false) {
  return app.cdp.evaluate(`(async () => {
    const config = await desktop.getRuntimeConfig();
    const headers = {'Content-Type':'application/json','X-HSC-Session':config.sessionToken};
    if (${parent}) {
      const unlocked = await fetch(config.localApiUrl+'/v1/parent/unlock', {method:'POST',headers,body:JSON.stringify({pin:'2468'})});
      if (!unlocked.ok) throw new Error('Parent unlock failed'); headers['X-HSC-Parent'] = (await unlocked.json()).parent_token;
    }
    const response = await fetch(config.localApiUrl + ${JSON.stringify(path)}, {method:${body === undefined ? "'GET'" : "'POST'"},headers,
      ${body === undefined ? '' : `body:JSON.stringify(${JSON.stringify(body)}),`} cache:'no-store'});
    const value = await response.json(); if (!response.ok) throw new Error('HTTP '+response.status+' '+JSON.stringify(value)); return value;
  })()`)
}
async function facts() {
  const value = await run(pythonBin, [join(root, recoveryNative ? 'scripts/seed-pending-recovery-native.py' : 'scripts/seed-pet-task-native.py'), '--data-dir', dataDir, '--facts'], { cwd: root, env: environment })
  return JSON.parse(value.stdout)
}
async function stopApp(app) {
  await stopOwned(app)
  if (app.backend) await stopNativeOwned(app.backend)
}
async function stopNativeOwned(item) {
  if (item.phase !== 'controlled-synthetic-host') return stopOwned(item)
  const child = item.child
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM')
  await until(() => child.exitCode !== null || child.signalCode !== null, 'owned synthetic host exit', 10000)
  try { process.kill(child.pid, 0); throw new Error('Owned host PID still alive') }
  catch (error) { if (error.code !== 'ESRCH') throw error }
  child.stdout?.destroy(); child.stderr?.destroy(); child.unref()
  owned.delete(child)
}
async function captureSaid(app, text, capability) {
  console.log(JSON.stringify({ phase: 'capture-command', expected_capability: capability }))
  await save(join(temporaryRoot, 'synthetic-recognition.json'), { transcript: text, local_recovery_only: capability === 'local-recovery-safety' })
  const count = await app.cdp.evaluate('window.__petProof.requests.length')
  await until(() => app.cdp.evaluate('window.__petProof.mediaOpens>0'), 'actual recorder opened')
  await app.cdp.evaluate('window.__petCaptureBurst()')
  const asr = await until(() => app.cdp.evaluate(`window.__petProof.requests.slice(${count}).find(row=>row.path.endsWith('/asr')&&row.status===200&&row.result?.transcript===${JSON.stringify(text)})`), 'actual ASR persistence')
  await save(join(outputDir, `capture-${count}.json`), asr)
  const executed = await until(() => app.cdp.evaluate(capability === 'local-recovery-safety'
    ? `window.__petProof.requests.slice(${count}).find(row=>row.path.endsWith('/outcome')&&row.status===200&&row.result?.action?.local_recovery_control)`
    : capability === 'companion_activity_reply'
    ? `window.__petProof.requests.slice(${count}).find(row=>row.path.endsWith('/agent-turn')&&row.status===200&&row.result?.intent==='companion_activity_reply'&&row.result?.tool_status==='completed')`
    : `window.__petProof.requests.slice(${count}).find(row=>row.path.endsWith('/execute')&&row.status===200&&row.result?.execution?.capability===${JSON.stringify(capability)})`), `actual ${capability}`)
  if (capability === 'companion_activity_reply') {
    await until(() => app.cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(executed.result.response_text)})`), 'persisted reply delivered in actual UI')
    const writes = await app.cdp.evaluate(`window.__petProof.requests.slice(${count}).filter(row => row.method === 'POST'
      && row.path.includes('/agent-turns/' + ${JSON.stringify(executed.result.agent_turn_id)} + '/'))`)
    check(writes.length === 0, 'Direct completed activity reply was executed or completed again')
  }
  console.log(JSON.stringify({ phase: 'captured-command-executed', capability }))
  return { asr, executed }
}
function check(condition, message) { if (!condition) throw new Error(message) }
async function shot(app, name, width) {
  await app.cdp.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false })
  await screenshot(app.cdp, join(outputDir, name))
}
let app
try {
  const hashes = {}
  for (const path of ['out/main/index.js', 'out/renderer/index.html', 'scripts/seed-pet-task-native.py', 'scripts/electron-pet-task-native.mjs']) {
    hashes[path] = createHash('sha256').update(await readFile(join(root, path))).digest('hex')
  }
  await save(join(outputDir, 'input-hashes.json'), hashes)
  if (!reused) {
    const seeded = await run(pythonBin, recoveryNative
      ? [join(root, 'scripts/seed-pending-recovery-native.py'), '--data-dir', dataDir]
      : [join(root, 'scripts/seed-pet-task-native.py'), '--data-dir', dataDir, '--skill', skill, '--mode', mode], { cwd: root, env: environment })
    await writeFile(join(outputDir, 'seed.log'), redactHarnessDiagnostic(seeded.stderr, [...diagnosticSecrets]))
    await writeFile(join(outputDir, 'seed-stdout.log'), redactHarnessDiagnostic(seeded.stdout, [...diagnosticSecrets]))
    await save(seedPath, JSON.parse(seeded.stdout))
  }
  const seed = JSON.parse(await readFile(seedPath, 'utf8'))
  check(seed.skill === skill && seed.mode === mode, 'Retained case must not change skill or mode')
  let checkpoint = await readFile(checkpointPath, 'utf8').then(JSON.parse).catch(error => { if (error.code === 'ENOENT') return null; throw error })
  const retained = await facts()
  if (!checkpoint && retained.started_events.length) throw new Error('Uncheckpointed executed task: inspect retained HTTP/DB evidence, do not reissue')
  app = await launch()
  const before = await facts()
  await save(join(outputDir, 'facts-prelaunch.json'), retained)
  await save(join(outputDir, 'facts-postlaunch.json'), before)
  const diagPath = `/v1/sessions/${seed.session_id}/diagnostic-prompt`
  if (recoveryReadOnly) {
    const journalKey = `hsc-pending-evaluation-v1:${seed.session_id}`
    check(await app.cdp.evaluate(`localStorage.getItem(${JSON.stringify(journalKey)})==='{synthetic-corrupt-pending'`), 'Original journal missing')
    await until(()=>app.cdp.evaluate("document.body.innerText.includes('原评价恢复记录需要家长核对')"), 'visible recovery guidance')
    const checks = []
    for (const width of [1100,390]) {
      await shot(app, `recovery-readonly-${width}.png`, width)
      const measured = await app.cdp.evaluate(`(() => {const panel=document.querySelector('.agent-command-panel');
        const notice=panel.querySelector('.agent-tool-recovery');return {width:innerWidth,noticeWidth:notice?.getBoundingClientRect().width,
          objective:!!panel.querySelector('.agent-objective'),text:panel.innerText,overflow:document.documentElement.scrollWidth-innerWidth}})()`)
      check(measured.noticeWidth>180 && measured.overflow<=1 && !measured.objective, 'Recovery layout still has narrow notice, overflow or stale objective')
      check(!measured.text.includes('你可以直接对我说“准备好了”'), 'Recovery still invites task execution')
      checks.push(measured)
    }
    const final = await facts(), proof = await app.cdp.evaluate('window.__petProof')
    check(JSON.stringify(final.learning_hashes)===JSON.stringify(before.learning_hashes), 'Read-only display changed learning facts')
    check(!proof.requests.some(row=>['/agent-turn','/execute','/outcome','/asr'].some(end=>row.path.endsWith(end))), 'Read-only screenshot issued a command')
    await save(join(outputDir,'readonly-result.json'),{status:'passed',retained_fixture:temporaryRoot,checks,proof,
      scope:'Same recovery database/profile; no speech recognition, new evaluation, safety action or original journey replay'})
  } else if (recoveryNative) {
    const journalKey = `hsc-pending-evaluation-v1:${seed.session_id}`
    const pending = { version: 1, sessionId: seed.session_id, childId: seed.child_id,
      turnId: seed.evaluation_turn_id, status: 'unknown', silent: false }
    await app.cdp.evaluate(`localStorage.setItem(${JSON.stringify(journalKey)},${JSON.stringify(JSON.stringify(pending))});sessionStorage.setItem('hsc-native-hold-evaluation','yes')`)
    await app.cdp.send('Page.reload', { ignoreCache: true })
    await until(() => app.cdp.evaluate("!!document.querySelector('.session-shell .hands-free-control')"), 'pending actual workspace')
    const query = await captureSaid(app, '刚才的评价现在保存了吗？', 'conversation.respond')
    check(query.asr.capture_reference.pending_evaluation_turn_id === seed.evaluation_turn_id, 'Actual onset did not freeze original pending evaluation')
    check(query.executed.result.turn.action.pending_evaluation_query?.turn_id === seed.evaluation_turn_id, 'Natural query did not preserve original identity')
    const pendingProof = await app.cdp.evaluate('window.__petProof')
    check(!pendingProof.requests.some(row=>row.path===`/v1/agent-turns/${seed.evaluation_turn_id}/execute`), 'Original evaluation was executed again')
    await save(join(outputDir, 'pending-query.json'), { query, proof: pendingProof })
    await app.cdp.evaluate("sessionStorage.removeItem('hsc-native-hold-evaluation');window.__petProof.held.splice(0).forEach(resolve=>resolve())")
    await until(() => app.cdp.evaluate(`localStorage.getItem(${JSON.stringify(journalKey)})===null`), 'actual completed evaluation clears only its journal')
    await app.cdp.evaluate(`localStorage.setItem(${JSON.stringify(journalKey)},'{synthetic-corrupt-pending');`)
    await app.cdp.send('Page.reload', { ignoreCache: true })
    await until(() => app.cdp.evaluate("!!document.querySelector('.session-shell .hands-free-control')"), 'corrupt journal recovery workspace')
    const recoveryBefore = await facts()
    const rescue = await captureSaid(app, '我现在有危险，请帮我找身边的大人。', 'local-recovery-safety')
    check(JSON.stringify(rescue.asr.capture_reference)===JSON.stringify({child_id:seed.child_id,local_recovery_only:true}), 'Recovery capture contains old learning/activity references')
    check(rescue.asr.result.cloud_used===false && rescue.asr.result.capture_binding?.local_recovery_only===true
      && rescue.asr.result.capture_binding.raw_audio_available===false, 'Recovery recognition was not strictly local and raw-free')
    const proof = await app.cdp.evaluate('window.__petProof')
    const turn = rescue.executed.result, id = turn.agent_turn_id
    const admission = proof.requests.findIndex(row=>row.path.endsWith(`/agent-turns/${id}/control-admission`)&&row.status===200)
    const outcome = proof.requests.findIndex(row=>row.path.endsWith(`/agent-turns/${id}/outcome`)&&row.status===200)
    check(admission>=0 && outcome>admission, 'Bridge did not admit before outcome')
    const original = proof.requests.find(row=>row.path.endsWith('/agent-turn')&&row.result?.agent_turn_id===id)
    check(original?.local_recovery_only===true && !original.pending_evaluation_turn_id, 'Agent recovery mode lost')
    check(rescue.executed.final_response===original.result.response_text, 'Canonical safety response changed')
    check(JSON.stringify(rescue.executed.receipt.client_bridge_receipt.output)===JSON.stringify({parent_contact_prompted:true}), 'Bridge falsely claimed contact')
    await until(()=>app.cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(original.result.response_text)})`), 'canonical safety prompt visible')
    const after = await facts()
    check(JSON.stringify(after.learning_hashes)===JSON.stringify(recoveryBefore.learning_hashes), 'Recovery changed learning records')
    check(after.model_calls===recoveryBefore.model_calls, 'Local recovery invoked model')
    check(after.safety.some(row=>row.agent_turn_id===id)&&after.recovery_turns.some(row=>row.agent_turn_id===id&&row.tool_status==='completed'&&row.objective_id===null), 'Safety/runtime receipts not persisted')
    check(after.other_child_fingerprint===recoveryBefore.other_child_fingerprint, 'Other child changed')
    await save(join(outputDir, 'recovery-before-restart.json'), { rescue, proof, recoveryBefore, after })
    await shot(app, '01-local-safety-1100.png', 1100); await shot(app, '02-local-safety-390.png', 390)
    await stopApp(app); app = await launch()
    check(await app.cdp.evaluate(`localStorage.getItem(${JSON.stringify(journalKey)})==='{synthetic-corrupt-pending'`), 'Restart repaired/deleted corrupt journal')
    const restarted = await facts()
    check(JSON.stringify(restarted.learning_hashes)===JSON.stringify(after.learning_hashes), 'Restart changed original evaluation/learning')
    check(JSON.stringify(restarted.safety)===JSON.stringify(after.safety), 'Restart duplicated safety event')
    await shot(app, '03-recovery-restarted-390.png', 390)
    await save(join(outputDir, 'result.json'), { status:'passed', retained_fixture:temporaryRoot,
      checks:['actual_pending_query_original_GET_not_execute','onset_local_recovery_no_old_refs','local_admission_canonical_bridge_receipt',
        'unchanged_learning_and_other_child','same_database_restart_journal_and_safety'], before, after, restarted,
      boundaries:{actual_electron:true,actual_http:true,synthetic_pcm_recognizer:true,controlled_semantic_transport:true,
        physical_microphone:false,paid_cloud:false,windows:false,real_user:false} })
    console.log(JSON.stringify({status:'passed',journey:'pending-local-recovery',outputDir,retained_fixture:temporaryRoot}))
  } else if (receiptAuditOnly) {
    check(checkpoint, 'Receipt audit requires the original execution checkpoint')
    const read = await api(app, `${diagPath}?prompt_id=${encodeURIComponent(checkpoint.prompt.prompt_id)}`)
    check(read.status === 'waiting_answer' && read.prompt.prompt_id === checkpoint.prompt.prompt_id, 'Restart lost the original prompt')
    const repeat = await api(app, `/v1/agent-turns/${checkpoint.turn_id}/execute`, {})
    check(JSON.stringify(repeat.execution) === JSON.stringify(checkpoint.execution), 'Original turn receipt changed')
    const media = await api(app, `/v1/sessions/${seed.session_id}/pet-media/prompt/${encodeURIComponent(checkpoint.prompt.prompt_id)}?item_version=${encodeURIComponent(checkpoint.prompt.item_version)}`)
    check(media.status === 'missing_media' && media.listening.length === 0, 'Original audio unexpectedly became available')
    const other = await api(app, '/v1/parent/children/child-junior-3/pet-prompts', undefined, true)
    check(other.child_id === 'child-junior-3' && other.prompts.length === 0, 'Other child received original evidence')
    await shot(app, '01-receipt-restart-1100.png', 1100)
    await shot(app, '02-receipt-restart-390.png', 390)
    const final = await facts(), proof = await app.cdp.evaluate('window.__petProof')
    check(JSON.stringify(final.started_events) === JSON.stringify(retained.started_events), 'Receipt replay changed start events')
    check(final.started_events.length === 1 && final.started_events[0].count === 1, 'Original task was not uniquely started')
    check(JSON.stringify(final.plans) === JSON.stringify(retained.plans), 'Receipt replay changed original timer/state')
    check(final.prompts.length === 1 && final.prompts[0].prompt_id === checkpoint.prompt.prompt_id, 'Receipt replay replaced prompt')
    check(final.attempts === 0 && final.completed_tasks === 0 && final.model_calls === 0 && final.media_playback_events === 0, 'Receipt audit invented execution evidence')
    const isolation = {
      unchanged_since_original_command: final.other_child_fingerprint === checkpoint.before.other_child_fingerprint,
      unchanged_during_startup: before.other_child_fingerprint === retained.other_child_fingerprint,
      unchanged_during_receipt_reads: final.other_child_fingerprint === before.other_child_fingerprint,
      original: checkpoint.before.other_child_fingerprint,
      prelaunch: retained.other_child_fingerprint,
      postlaunch: before.other_child_fingerprint,
      final: final.other_child_fingerprint,
    }
    check(!proof.requests.some(row => row.path.endsWith('/agent-turn')), 'Receipt audit issued a new command')
    if (seed.stimulus) check(!proof.speech.some(text => text.includes(seed.stimulus)), 'Transcript entered ordinary TTS')
    await save(join(outputDir, 'result.json'), { status: 'partial', skill, mode, retained_fixture: temporaryRoot,
      checks: ['original_receipt_after_restart', 'unchanged_unique_timer_and_prompt', 'other_child_prompt_not_exposed', 'no_new_command_score_or_playback'],
      known_blocker: 'listening-fresh-01 duplicate prepare clears question capture; not repaired or retested here',
      read, media, isolation, before: retained, final, proof,
      boundaries: { actual_electron: true, actual_http: true, synthetic_only: true, listening_pcm_playback: false, awaiting_voice_resume: false } })
    await writeFile(join(outputDir, 'process.log'), redactHarnessDiagnostic(app.logs(), [...diagnosticSecrets]))
    console.log(JSON.stringify({ status: 'partial', checks: 4, outputDir, retained_fixture: temporaryRoot }))
  } else {
  if (continueStarted) {
    check(checkpoint && retained.started_events.length === 1, 'Continue needs the actual original started task')
    check(retained.plans.find(row => row.plan_item_id === seed.plan_item_id)?.status === 'paused', 'Only genuinely paused tasks may continue in this case')
    await say(app, '继续原任务')
    const proof = await app.cdp.evaluate('window.__petProof')
    const event = [...proof.requests].reverse().find(row => row.path.endsWith('/execute') && row.status === 200 && row.result?.execution?.capability === 'task.resume')
    check(event, 'Actual continue voice route did not execute task.resume')
    const result = event.result.execution.result?.diagnostic_prompt
    check(result?.status === 'waiting_answer' && result.prompt?.prompt_id === checkpoint.prompt.prompt_id, 'Resume did not return the original waiting prompt')
    checkpoint = { turn_id: event.result.turn.agent_turn_id, prompt: result.prompt, execution: event.result.execution,
      before, first_proof: proof, original_mode: mode }
    await save(join(outputDir, 'resumed-command.json'), checkpoint)
  }
  if (!checkpoint) {
    if (mode === 'awaiting') {
      const pending = await api(app, diagPath)
      check(pending.status === 'awaiting_task_start' && pending.plan_item_id === seed.plan_item_id, 'Restart did not expose original awaiting task')
      check(pending.prompt.prompt_id === seed.original_prompt.prompt_id, 'Restart replaced the original prompt')
      await say(app, '重读题目')
      const text = await app.cdp.evaluate('document.body.innerText')
      check(text.includes('继续原任务'), 'Awaiting original has no readable voice recovery instruction')
      await shot(app, '01-awaiting-original-390.png', 390)
      check((await facts()).started_events.length === 0, 'Read/replay started the timer before task recovery')
    }
    if (mode === 'awaiting' && controlledVoice) await captureSaid(app, '继续原任务', 'task.resume')
    else await say(app, mode === 'awaiting' ? '继续原任务' : '开始任务')
    const proof = await app.cdp.evaluate('window.__petProof')
    const expectedCapability = mode === 'awaiting' ? 'task.resume' : 'task.start'
    const event = [...proof.requests].reverse().find(row => row.path.endsWith('/execute') && row.status === 200 && row.result?.execution?.capability === expectedCapability)
    check(event, `Actual voice route did not execute ${expectedCapability}`)
    const result = event.result.execution.result?.diagnostic_prompt
    check(result?.status === 'waiting_answer', 'Task did not return waiting_answer')
    check(result.prompt.diagnostic_item_id === seed.diagnostic_item_id && result.prompt.child_id === seed.child_id, 'Task selected a different original or child')
    if (mode === 'awaiting') check(result.prompt.prompt_id === seed.original_prompt.prompt_id, 'Recovery replaced the prepared original')
    checkpoint = { turn_id: event.result.turn.agent_turn_id, prompt: result.prompt, execution: event.result.execution,
      before, first_proof: proof, original_mode: mode }
    await save(checkpointPath, checkpoint)
  }
  if (activityNative) {
    const invited = args.get('--continue-existing-activity') === 'yes' ? null
      : await captureSaid(app, '能和我一起编个小故事吗？', 'companion.start_activity')
    const focusPath = `/v1/sessions/${seed.session_id}/voice/activity-focus`
    const opened = await until(async () => { const value = await api(app, focusPath); return value.activity_focus ? value : null }, 'actual activity focus')
    const activityBefore = await facts()
    const replied = await captureSaid(app, '我想让一位小探险家当主角。', 'companion_activity_reply')
    check(JSON.stringify(replied.asr.capture_reference.activity_reference) === JSON.stringify(opened.activity_focus), 'Actual ASR did not carry onset activity focus')
    check(replied.asr.capture_reference.question_reference?.binding.prompt_id === checkpoint.prompt.prompt_id, 'Activity erased original question reference')
    check(replied.asr.result.capture_binding?.activity_validation === 'valid', 'Backend did not validate captured activity focus')
    const current = await api(app, focusPath)
    check(current.activity_focus && current.activity_focus.version !== opened.activity_focus.version, 'Activity reply did not advance focus')
    const secondReply = await captureSaid(app, '我想让主角先去河边。', 'companion_activity_reply')
    check(JSON.stringify(secondReply.asr.capture_reference.activity_reference) === JSON.stringify(current.activity_focus), 'Second reply captured stale prior-turn focus')
    check(secondReply.asr.result.capture_binding?.activity_validation === 'valid', 'Second reply focus was not valid')
    const advanced = await facts(), priorActivity = activityBefore.activities.find(row => row.activity_id === opened.activity_focus.activity_id)
    check(advanced.activities.find(row => row.activity_id === opened.activity_focus.activity_id)?.turn_count === priorActivity.turn_count + 2, 'Two replies did not advance the original activity twice')
    for (const entry of [replied, secondReply]) check(advanced.activity_reply_receipts.some(row => row.agent_turn_id === entry.executed.result.agent_turn_id
      && row.tool_status === 'completed' && row.step_status === 'completed' && row.capability_name === 'conversation.respond'), 'Missing persisted original activity reply receipt')
    check(advanced.attempts === 0, 'Activity reply was scored against original question')
    await save(join(outputDir, 'activity-delivery-proof.json'), await app.cdp.evaluate('window.__petProof'))
    await save(join(outputDir, 'activity-two-rounds.json'), { invited, opened, replied, current, secondReply, before: activityBefore, advanced })
    await shot(app, '05-activity-focus-1100.png', 1100)
    await shot(app, '06-activity-focus-390.png', 390)
    const declined = await captureSaid(app, '这个活动先不做了', 'companion.activity_feedback')
    const ended = await api(app, focusPath)
    await save(join(outputDir, 'activity-before-restart.json'), { invited, opened, replied, current, secondReply, declined, ended, facts: await facts() })
    await app.cdp.evaluate("window.dispatchEvent(new Event('hsc-privacy-pause'))")
    await stopApp(app); app = await launch()
    const restarted = await api(app, focusPath), final = await facts()
    await save(join(outputDir, 'activity-result.json'), { status: ended.activity_focus === null && restarted.activity_focus === null ? 'passed' : 'partial',
      restarted, ended, final, retained_fixture: temporaryRoot,
      boundaries: { actual_electron: true, actual_http_asr_persistence: true, synthetic_recognizer: true,
        deterministic_model_transport: true, external_model_network: false, real_asr_accuracy: false, real_user: false } })
    check(ended.activity_focus === null && restarted.activity_focus === null, 'Activity end/restart did not release focus')
    check(final.attempts === 0, 'Activity speech became a learning attempt')
    console.log(JSON.stringify({ status: 'passed', journey: 'synthetic-activity-capture', outputDir }))
  } else {
  const text = await app.cdp.evaluate('document.body.innerText')
  check(text.includes(seed.question), 'Actual question not shown in SessionWorkspace')
  if (skill === 'listening') check(/缺对应原音|缺.*原音|原音.*未.*核对/.test(text), 'Missing original audio is not clearly blocked')
  await shot(app, '02-started-original-1100.png', 1100)
  await shot(app, '03-started-original-390.png', 390)

  // Probe actual PcmRecorder -> ASR request capture_reference using synthetic
  // signal. Recognition may reject the tone; no response/answer is invented.
  await until(() => app.cdp.evaluate('window.__petProof.mediaOpens>0'), 'synthetic microphone opened')
  await app.cdp.evaluate('window.__petCaptureBurst()')
  const capture = await until(() => app.cdp.evaluate("window.__petProof.requests.find(row=>row.path.endsWith('/asr')&&row.capture_reference?.question_reference?.kind==='diagnostic')"), 'real captured original binding')
  check(capture.capture_reference.child_id === seed.child_id
    && capture.capture_reference.question_reference.binding.prompt_id === checkpoint.prompt.prompt_id
    && capture.capture_reference.question_reference.binding.item_version === checkpoint.prompt.item_version, 'Capture bound another question')
  await app.cdp.evaluate("window.dispatchEvent(new Event('hsc-privacy-pause'))")
  const firstProof = await app.cdp.evaluate('window.__petProof')
  await save(join(outputDir, 'first-process.json'), firstProof)
  await save(join(outputDir, 'facts-before-restart.json'), await facts())
  await writeFile(join(outputDir, 'first-process.log'), redactHarnessDiagnostic(app.logs(), [...diagnosticSecrets]))
  await stopApp(app); app = await launch()
  const read = await api(app, `${diagPath}?prompt_id=${encodeURIComponent(checkpoint.prompt.prompt_id)}`)
  check(read.status === 'waiting_answer' && read.prompt.prompt_id === checkpoint.prompt.prompt_id, 'Restart lost the original waiting prompt')
  const repeat = await api(app, `/v1/agent-turns/${checkpoint.turn_id}/execute`, {})
  check(JSON.stringify(repeat.execution) === JSON.stringify(checkpoint.execution), 'Same turn did not recover its original execution receipt')
  const other = await api(app, '/v1/parent/children/child-junior-3/pet-prompts', undefined, true)
  check(other.child_id === 'child-junior-3' && other.prompts.length === 0, 'Other child received original question evidence')
  await shot(app, '04-restarted-original-1100.png', 1100)
  const final = await facts(), secondProof = await app.cdp.evaluate('window.__petProof')
  await save(join(outputDir, 'facts-final.json'), final)
  const proofs = [checkpoint.first_proof, firstProof, secondProof]
  const requests = proofs.flatMap(p => p.requests), speech = proofs.flatMap(p => p.speech)
  check(!requests.some(row => row.path.endsWith('/diagnostic-prompt/prepare')), 'Renderer unexpectedly prepared a random/new question')
  if (skill === 'listening' && seed.stimulus) check(!speech.some(text => text.includes(seed.stimulus)), 'Listening transcript entered ordinary TTS')
  check(final.started_events.length === 1 && final.started_events[0].plan_item_id === seed.plan_item_id && final.started_events[0].count === 1, 'Repeated timer or wrong task start')
  check(final.prompts.length === 1 && final.prompts[0].prompt_id === checkpoint.prompt.prompt_id, 'Duplicate or replaced prompt')
  check(final.attempts === 0 && final.completed_tasks === 0 && final.model_calls === 0 && final.media_playback_events === 0, 'Missing audio/task execution fabricated score, completion, or playback')
  check(final.other_child_fingerprint === checkpoint.before.other_child_fingerprint, 'Other child data changed')
  await writeFile(join(outputDir, 'second-process.log'), redactHarnessDiagnostic(app.logs(), [...diagnosticSecrets]))
  await save(join(outputDir, 'result.json'), { status: 'passed', skill, mode, retained_fixture: temporaryRoot,
    checks: ['actual_task_original_and_capture', 'no_renderer_prepare_or_listening_transcript_tts', 'same_turn_restart_unique_timer', 'other_child_read_and_storage_isolated'],
    before: checkpoint.before, final, capture, secondProof,
    boundaries: { actual_electron: true, actual_http: true, synthetic_only: true, recognized_command_callback: true,
      microphone_asr_accuracy: false, physical_devices: false, listening_pcm_playback: false,
      qualified_listening_score: false, windows: false, real_family: false } })
  console.log(JSON.stringify({ status: 'passed', skill, mode, outputDir, retained_fixture: temporaryRoot }))
  }
  }
} catch (error) {
  await captureOwnedFailure({ outputDir, temporaryRoot, apps: [...owned.values()], error, secrets: [...diagnosticSecrets], smtpMessages: 0 })
  for (const item of owned.values()) if (item.cdp) {
    await screenshot(item.cdp, join(outputDir, 'failure.png')).catch(() => {})
    const proof = await item.cdp.evaluate('window.__petProof').catch(() => null)
    await save(join(outputDir, 'failure-proof.json'), proof)
  }
  throw error
} finally {
  for (const item of [...owned.values()].reverse()) await stopNativeOwned(item)
}
