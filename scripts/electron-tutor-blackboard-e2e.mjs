import { spawn } from 'node:child_process'
import { copyFile, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { homedir, tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { awaitCdpResponse, releaseChildProcessHandles } from './electron-owned-process.mjs'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const electronBin = join(root, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron')
const pythonBin = join(root, '.venv', 'bin', 'python')
const outputDir = resolve(process.env.HSC_BLACKBOARD_E2E_OUTPUT ?? join(root, 'artifacts', 'tutor-blackboard-electron'))
const keychainService = process.env.HSC_MODEL_KEYCHAIN_SERVICE ?? 'hsc-deepseek-api-key'
const voiceSettingsSource = process.env.HSC_BLACKBOARD_E2E_VOICE_SETTINGS_SOURCE
  ?? join(homedir(), 'Library', 'Application Support', 'home-study-companion', 'voice-settings.json')
const asrSettingsSource = process.env.HSC_BLACKBOARD_E2E_ASR_SETTINGS_SOURCE
  ?? join(homedir(), 'Library', 'Application Support', 'home-study-companion', 'asr-settings.json')

function freePort() {
  return new Promise((resolvePromise, reject) => {
    const server = createServer()
    server.unref(); server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address(); const port = typeof address === 'object' && address ? address.port : 0
      server.close((error) => error ? reject(error) : resolvePromise(port))
    })
  })
}

async function eventually(action, description, timeoutMs = 70_000) {
  const deadline = Date.now() + timeoutMs; let lastError
  while (Date.now() < deadline) {
    try { const value = await action(); if (value) return value } catch (error) { lastError = error }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 180))
  }
  throw new Error(`${description} timed out${lastError ? `: ${lastError.message}` : ''}`)
}

class Cdp {
  constructor(url) { this.socket = new WebSocket(url); this.nextId = 0; this.pending = new Map() }
  open() {
    return new Promise((resolvePromise, reject) => {
      this.socket.onopen = resolvePromise; this.socket.onerror = reject
      this.socket.onmessage = (event) => {
        const message = JSON.parse(event.data)
        if (!message.id || !this.pending.has(message.id)) return
        const waiting = this.pending.get(message.id); this.pending.delete(message.id)
        if (message.error) waiting.reject(new Error(message.error.message)); else waiting.resolve(message.result)
      }
    })
  }
  send(method, params = {}) {
    const id = ++this.nextId; const response = awaitCdpResponse(this.pending, id, method, 30_000)
    this.socket.send(JSON.stringify({ id, method, params })); return response
  }
  async evaluate(expression) {
    const response = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description ?? response.exceptionDetails.text)
    return response.result.value
  }
  close() { this.socket.close() }
}

async function api(runtime, path, method = 'GET', body) {
  const response = await fetch(`${runtime.localApiUrl}${path}`, {
    method,
    headers: { 'X-HSC-Session': runtime.sessionToken, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(path.endsWith('/tutor') ? 190_000 : 20_000),
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload.message ?? payload.detail?.message ?? payload.detail ?? payload.code ?? payload
    throw new Error(`${method} ${path} failed (${response.status}): ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`)
  }
  return payload
}

async function probeNaturalVoice(runtime, sessionId) {
  const status = await api(runtime, '/v1/local-ai/status')
  if (!status.voice?.configured) return { configured: false, provider: 'disabled', first_frame_ms: null, first_frame_bytes: 0 }
  const started = Date.now()
  const response = await fetch(`${runtime.localApiUrl}/v1/sessions/${sessionId}/voice/synthesize/stream`, {
    method: 'POST',
    headers: { 'X-HSC-Session': runtime.sessionToken, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: '我们继续下一步。', client_action_id: 'blackboard-e2e-natural-voice-probe' }),
    signal: AbortSignal.timeout(45_000),
  })
  if (!response.ok || !response.body) throw new Error(`Natural voice probe failed (${response.status})`)
  const reader = response.body.getReader(); const first = await reader.read(); await reader.cancel()
  if (first.done || !first.value || first.value.byteLength <= 8) throw new Error('Natural voice returned no framed PCM')
  return { configured: true, provider: status.voice.provider, first_frame_ms: Date.now() - started, first_frame_bytes: first.value.byteLength }
}

function pcm16ToWav(pcm, sampleRate) {
  const header = Buffer.alloc(44)
  header.write('RIFF', 0); header.writeUInt32LE(36 + pcm.length, 4); header.write('WAVE', 8)
  header.write('fmt ', 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20)
  header.writeUInt16LE(1, 22); header.writeUInt32LE(sampleRate, 24)
  header.writeUInt32LE(sampleRate * 2, 28); header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34)
  header.write('data', 36); header.writeUInt32LE(pcm.length, 40)
  return Buffer.concat([header, pcm])
}

async function synthesizeVoiceFixture(runtime, sessionId, text, clientActionId) {
  const started = Date.now()
  const response = await fetch(`${runtime.localApiUrl}/v1/sessions/${sessionId}/voice/synthesize/stream`, {
    method: 'POST',
    headers: { 'X-HSC-Session': runtime.sessionToken, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, client_action_id: clientActionId }),
    signal: AbortSignal.timeout(60_000),
  })
  if (!response.ok) throw new Error(`Synthetic voice fixture failed (${response.status})`)
  const framed = Buffer.from(await response.arrayBuffer())
  const chunks = []; let offset = 0; let sampleRate = null
  while (framed.length - offset >= 8) {
    const frameRate = framed.readUInt32LE(offset); const size = framed.readUInt32LE(offset + 4)
    if (!frameRate || size <= 0 || size % 2 || framed.length - offset - 8 < size) throw new Error('Synthetic voice fixture returned invalid PCM framing')
    if (sampleRate === null) sampleRate = frameRate
    else if (sampleRate !== frameRate) throw new Error('Synthetic voice fixture returned mixed sample rates')
    chunks.push(framed.subarray(offset + 8, offset + 8 + size)); offset += 8 + size
  }
  if (offset !== framed.length || !sampleRate || chunks.length === 0) throw new Error('Synthetic voice fixture returned incomplete audio')
  const pcm = Buffer.concat(chunks)
  return {
    wav: pcm16ToWav(pcm, sampleRate), sample_rate: sampleRate,
    audio_seconds: Math.round((pcm.length / 2 / sampleRate) * 100) / 100,
    synthesis_ms: Date.now() - started,
  }
}

async function stop(child) {
  if (!child?.pid || child.exitCode !== null) return
  try { process.kill(-child.pid, 'SIGTERM') } catch { child.kill('SIGTERM') }
  await eventually(() => child.exitCode !== null, 'Electron cleanup', 8_000).catch(() => {
    try { process.kill(-child.pid, 'SIGKILL') } catch { child.kill('SIGKILL') }
  })
  releaseChildProcessHandles(child)
}

async function main() {
  await mkdir(outputDir, { recursive: true })
  const temporaryRoot = await mkdtemp(join(tmpdir(), 'hsc-blackboard-e2e-'))
  const profileDir = join(temporaryRoot, 'profile'); const dataDir = join(temporaryRoot, 'data')
  const cdpPort = await freePort(); let child; let cdp; let logs = ''
  try {
    await mkdir(profileDir, { recursive: true })
    await copyFile(resolve(voiceSettingsSource), join(profileDir, 'voice-settings.json')).catch(() => undefined)
    const asrConfigured = await copyFile(resolve(asrSettingsSource), join(profileDir, 'asr-settings.json')).then(() => true).catch(() => false)
    child = spawn(electronBin, [root, `--remote-debugging-port=${cdpPort}`], {
      cwd: root, detached: true, stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...sanitizedHarnessEnvironment(process.env),
        HSC_ELECTRON_USER_DATA_DIR: profileDir,
        HSC_DATA_DIR: dataDir,
        HSC_DATABASE_PATH: join(dataDir, 'family-agent.db'),
        HSC_LOCAL_AGENT_PYTHON: pythonBin,
        HSC_MODEL_KEYCHAIN_SERVICE: keychainService,
      },
    })
    child.stdout.on('data', (chunk) => { logs += String(chunk) }); child.stderr.on('data', (chunk) => { logs += String(chunk) })
    const page = await eventually(async () => {
      const response = await fetch(`http://127.0.0.1:${cdpPort}/json/list`); const pages = await response.json()
      return pages.find((item) => item.type === 'page' && item.webSocketDebuggerUrl)
    }, 'Electron renderer')
    cdp = new Cdp(page.webSocketDebuggerUrl); await cdp.open(); await cdp.send('Page.enable'); await cdp.send('Runtime.enable')
    const runtime = await eventually(async () => {
      const value = await cdp.evaluate('window.desktop?.getRuntimeConfig()')
      return value?.status === 'ready' ? value : null
    }, 'local Agent runtime')

    const action = (name) => `blackboard-e2e-${name}`
    const parent = await api(runtime, '/v1/parent/setup', 'POST', { pin: '4862' })
    if (asrConfigured) {
      const calibrationResponse = await fetch(`${runtime.localApiUrl}/v1/parent/children/child-junior-3/voice-calibration`, {
        method: 'PUT',
        headers: { 'X-HSC-Session': runtime.sessionToken, 'X-HSC-Parent': parent.parent_token, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          profile: { sampleCount: 48_000, calibrationCount: 3, energy: 0.12, zeroCrossingRate: 0.08, spectralBands: [0.2, 0.3, 0.3, 0.2], environmentSpread: 0.1 },
          vocabulary: ['判别式', '一元二次方程', 'PET'],
          microphone: { system_id_hash: 'a'.repeat(64), label: 'Synthetic adult acceptance microphone', capabilities: { transport: 'browser-media-device', echo_cancellation_requested: true, noise_suppression_requested: true, auto_gain_control_requested: true, sample_rate_hz: 16_000, channel_count: 1 } },
          client_action_id: action('voice-calibration'),
        }),
        signal: AbortSignal.timeout(20_000),
      })
      if (!calibrationResponse.ok) throw new Error(`Voice calibration setup failed (${calibrationResponse.status})`)
    }
    const session = await api(runtime, '/v1/sessions', 'POST', { child_id: 'child-junior-3', client_action_id: action('session') })
    const sessionId = session.session_id
    await cdp.evaluate(`(async () => {
      const updatedAt = new Date().toISOString()
      await window.desktop.saveWakeWordSettings({ childId: 'child-junior-3', wakeWord: '小知', inactivitySeconds: 60, enabled: true, updatedAt })
      await window.desktop.saveVoiceOnboarding({ childId: 'child-junior-3', version: 1, step: 'completed', updatedAt })
    })()`)
    const spokenQuestion = '请讲解为什么一元二次方程的判别式大于零时有两个不等实根，并给我一个检查问题。'
    let recognizedQuestion = spokenQuestion
    let syntheticVoiceInput = null
    let cloudAsr = { configured: asrConfigured, tested: false, cloud_used: false, vocabulary_applied: false, transcript_chars: 0 }
    if (asrConfigured) {
      const calibration = await api(runtime, `/v1/sessions/${sessionId}/voice-calibration`)
      const fixture = await synthesizeVoiceFixture(runtime, sessionId, spokenQuestion, action('synthetic-child-voice'))
      const asrStarted = Date.now()
      const asr = await api(runtime, `/v1/sessions/${sessionId}/asr`, 'POST', { wav_base64: fixture.wav.toString('base64'), purpose: 'agent_command', client_action_id: action('cloud-asr') })
      if (!asr.cloud_used || !String(asr.model_version).startsWith('cloud:qwen:')) {
        const auditResponse = await fetch(`${runtime.localApiUrl}/v1/parent/children/child-junior-3/model-calls`, { headers: { 'X-HSC-Session': runtime.sessionToken, 'X-HSC-Parent': parent.parent_token }, signal: AbortSignal.timeout(10_000) })
        const audit = await auditResponse.json().catch(() => ({ items: [] }))
        const speechCall = audit.items?.find((item) => item.purpose === 'speech_recognition')
        throw new Error(`Cloud ASR did not process the approved adult fixture (cloud_used=${Boolean(asr.cloud_used)}, model_version=${String(asr.model_version)}, error=${speechCall?.error_code ?? 'not-recorded'})`)
      }
      if (!calibration.vocabulary_applied || calibration.vocabulary_status !== 'cloud_asr_context_enabled') throw new Error('Child vocabulary was not enabled for cloud ASR context')
      recognizedQuestion = String(asr.transcript).trim()
      if (!recognizedQuestion.includes('判别式')) throw new Error(`Cloud ASR lost the key learning concept (${recognizedQuestion})`)
      syntheticVoiceInput = { sample_rate: fixture.sample_rate, audio_seconds: fixture.audio_seconds, synthesis_ms: fixture.synthesis_ms }
      cloudAsr = { configured: true, tested: true, cloud_used: true, vocabulary_applied: true, transcript_chars: recognizedQuestion.length, recognition_ms: Date.now() - asrStarted }
    }
    await api(runtime, `/v1/sessions/${sessionId}/transition`, 'POST', { expected_state: 'check_in', target_state: 'classroom_recall', reason: 'synthetic blackboard acceptance', client_action_id: action('recall') })
    await api(runtime, `/v1/sessions/${sessionId}/recalls/none`, 'POST', { client_action_id: action('no-recall') })
    await api(runtime, `/v1/sessions/${sessionId}/transition`, 'POST', { expected_state: 'classroom_recall', target_state: 'homework_capture', reason: 'synthetic blackboard acceptance', client_action_id: action('homework-state') })
    await api(runtime, `/v1/sessions/${sessionId}/homework`, 'POST', { subject: '数学', title: '判别式合成验收', expected_minutes: 20, client_action_id: action('homework') })
    const plan = await api(runtime, `/v1/sessions/${sessionId}/plan/generate`, 'POST', { client_action_id: action('plan') })
    await api(runtime, `/v1/sessions/${sessionId}/transition`, 'POST', { expected_state: 'plan_review', target_state: 'study', reason: 'synthetic blackboard acceptance', client_action_id: action('study') })
    await api(runtime, `/v1/plan-items/${plan.plan_items[0].plan_item_id}/start`, 'POST', { client_action_id: action('start') })
    const naturalVoice = await probeNaturalVoice(runtime, sessionId)
    await cdp.evaluate('location.reload()')
    await eventually(() => cdp.evaluate("document.body.innerText.includes('哪里卡住了？')"), 'study Tutor surface')

    const question = recognizedQuestion
    const questionAgentTurn = await api(runtime, `/v1/sessions/${sessionId}/agent-turn`, 'POST', {
      transcript: question, client_action_id: action('agent-question'),
    })
    const acceptedLearningSources = new Set(['agent_runtime_deterministic', 'agent_runtime_planner'])
    if (questionAgentTurn.intent !== 'learning.explain'
      || questionAgentTurn.action?.capability !== 'learning.explain'
      || !acceptedLearningSources.has(questionAgentTurn.action?.source)) {
      throw new Error(`Agent did not route the explicit learning question through its durable learning runtime (${questionAgentTurn.intent}/${questionAgentTurn.action?.capability}/${questionAgentTurn.action?.source})`)
    }
    const objectiveBefore = await api(runtime, `/v1/sessions/${sessionId}/agent-runtime`)
    if (!objectiveBefore.active_objective?.objective_id || objectiveBefore.active_objective.objective_type !== 'learning') {
      throw new Error('Agent did not create a durable learning objective before tutoring')
    }
    const turn = await api(runtime, `/v1/sessions/${sessionId}/tutor`, 'POST', {
      subject: questionAgentTurn.action.subject ?? '数学',
      question: questionAgentTurn.action.question ?? question,
      hint_level: questionAgentTurn.action.hint_level ?? 6,
      client_action_id: action('tutor'), confirm_unknown_outcome_retry: false,
    })
    if (turn.output_envelope?.result?.blackboard?.version !== 'tutor_blackboard_v1') throw new Error('Tutor did not return a structured blackboard')
    if (!['model_structured', 'model_structured_repaired'].includes(turn.output_envelope.result.blackboard.source)) throw new Error(`Tutor blackboard degraded instead of using the model structure: ${turn.output_envelope.result.blackboard.degraded_reason ?? 'unknown'}`)
    await api(runtime, `/v1/agent-turns/${questionAgentTurn.agent_turn_id}/outcome`, 'PUT', {
      final_response: turn.answer, tool_status: 'completed',
    })
    await cdp.evaluate(`window.dispatchEvent(new CustomEvent('hsc-runtime-tutor-presentation',{detail:{turn:${JSON.stringify(turn)},speechPolicy:{localOnly:true},resolve:()=>{}}}))`)
    const live = await eventually(async () => {
      const state = await cdp.evaluate(`(() => { const board=document.querySelector('.tutor-blackboard'); return board ? {segments:board.querySelectorAll('.blackboard-segment').length,blocks:board.querySelectorAll('.blackboard-block').length,title:board.querySelector('header strong')?.textContent??''} : null })()`)
      return state?.blocks > 0 ? state : null
    }, 'audible-frame blackboard projection', 25_000)
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 900))
    live.in_viewport = await cdp.evaluate(`(() => { const rect=document.querySelector('.tutor-blackboard')?.getBoundingClientRect(); return Boolean(rect && rect.bottom > 0 && rect.top < innerHeight) })()`)
    if (!live.in_viewport) throw new Error('Live blackboard did not enter the child viewport automatically')
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
    await writeFile(join(outputDir, '01-live-synchronized-blackboard.png'), Buffer.from(shot.data, 'base64'), { mode: 0o600 })
    const playbackBeforeReload = await api(runtime, `/v1/sessions/${sessionId}/tutor-playback`)
    if (playbackBeforeReload.playback?.turn_id !== turn.turn_id || live.segments < 1) {
      throw new Error('Live blackboard progress was not durably bound to the tutor turn')
    }

    await cdp.evaluate('location.reload()')
    const restored = await eventually(async () => {
      const state = await cdp.evaluate(`(() => { const board=document.querySelector('.tutor-blackboard'); return board ? {segments:board.querySelectorAll('.blackboard-segment').length,blocks:board.querySelectorAll('.blackboard-block').length,title:board.querySelector('header strong')?.textContent??''} : null })()`)
      return state && state.segments >= live.segments
        && state.segments <= turn.output_envelope.result.blackboard.segments.length ? state : null
    }, 'persisted blackboard restoration')
    const playbackAfterReload = await api(runtime, `/v1/sessions/${sessionId}/tutor-playback`)
    if (playbackAfterReload.playback?.turn_id !== turn.turn_id
      || playbackAfterReload.playback?.playback_id !== playbackBeforeReload.playback?.playback_id) {
      throw new Error('Reload restored a different tutor playback instead of the persisted explanation')
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 900))
    restored.in_viewport = await cdp.evaluate(`(() => { const rect=document.querySelector('.tutor-blackboard')?.getBoundingClientRect(); return Boolean(rect && rect.bottom > 0 && rect.top < innerHeight) })()`)
    if (!restored.in_viewport) throw new Error('Restored blackboard did not enter the child viewport automatically')
    const restoredShot = await cdp.send('Page.captureScreenshot', { format: 'png' })
    await writeFile(join(outputDir, '02-restored-blackboard.png'), Buffer.from(restoredShot.data, 'base64'), { mode: 0o600 })

    const acknowledgement = await api(runtime, `/v1/sessions/${sessionId}/agent-turn`, 'POST', {
      transcript: '我懂了', client_action_id: action('agent-acknowledgement'),
    })
    if (acknowledgement.intent !== 'learning.request_response'
      || acknowledgement.action?.type !== 'none'
      || !acknowledgement.response_text.includes('自己的话')) {
      throw new Error('Agent incorrectly treated a self-reported acknowledgement as learning evidence')
    }
    await api(runtime, `/v1/agent-turns/${acknowledgement.agent_turn_id}/outcome`, 'PUT', {
      final_response: acknowledgement.response_text, tool_status: 'completed',
    })
    const independentResponse = '因为判别式大于零，根号里是正数，加上它和减去它会得到两个不同的实数根。'
    const evaluationTurn = await api(runtime, `/v1/sessions/${sessionId}/agent-turn`, 'POST', {
      transcript: independentResponse, client_action_id: action('agent-independent-response'),
    })
    if (evaluationTurn.intent !== 'learning.evaluate_response'
      || evaluationTurn.action?.capability !== 'learning.evaluate_response') {
      throw new Error(`Agent lost the active learning objective before evaluating the child response (${evaluationTurn.intent})`)
    }
    const evaluationExecution = await api(runtime, `/v1/agent-turns/${evaluationTurn.agent_turn_id}/execute`, 'POST')
    const outcomeLevel = evaluationExecution.execution?.result?.outcome_level
    if (!['understood_now', 'independent', 'transferred', 'retained'].includes(outcomeLevel)) {
      throw new Error(`Learning evaluator did not persist a supported positive evidence level (${String(outcomeLevel)})`)
    }
    const objectiveAfter = await api(runtime, `/v1/sessions/${sessionId}/agent-runtime`)
    if (objectiveAfter.active_objective?.objective_id !== objectiveBefore.active_objective.objective_id) {
      throw new Error('Agent created a new objective instead of continuing the same learning goal')
    }

    await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1024, height: 768, deviceScaleFactor: 1, mobile: false })
    await cdp.evaluate(`document.querySelector('.tutor-blackboard')?.scrollIntoView({block:'center'})`)
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 250))
    const tablet = await cdp.evaluate(`(() => { const board=document.querySelector('.tutor-blackboard')?.getBoundingClientRect(); return {viewport:[innerWidth,innerHeight],document_overflow:document.documentElement.scrollWidth>innerWidth,board_overflow:Boolean(board&&(board.left<0||board.right>innerWidth)),board_visible:Boolean(board&&board.bottom>0&&board.top<innerHeight)} })()`)
    if (tablet.document_overflow || tablet.board_overflow || !tablet.board_visible) throw new Error('Tablet-width blackboard layout overflowed or left the viewport')
    const tabletShot = await cdp.send('Page.captureScreenshot', { format: 'png' })
    await writeFile(join(outputDir, '03-tablet-width-blackboard.png'), Buffer.from(tabletShot.data, 'base64'), { mode: 0o600 })
    const report = {
      schema: 'electron_tutor_blackboard_e2e_v1', passed: true, synthetic_only: true,
      real_cloud_model: true, model_provider: turn.provider, model: turn.model,
      source: turn.output_envelope.result.blackboard.source,
      degraded_reason: turn.output_envelope.result.blackboard.degraded_reason ?? null,
      blackboard_repair_warnings: turn.output_envelope.result.blackboard.repair_warnings ?? [],
      voice_boundary: naturalVoice.configured
        ? 'Configured natural voice returned framed PCM; the blackboard starts from the same renderer first-frame callback.'
        : 'Electron playback path is real; this isolated profile used the disclosed system-voice fallback.',
      natural_voice: naturalVoice,
      cloud_asr: cloudAsr,
      synthetic_voice_input: syntheticVoiceInput,
      agent_learning_loop: {
        question_intent: questionAgentTurn.intent,
        objective_id: objectiveBefore.active_objective.objective_id,
        acknowledgement_intent: acknowledgement.intent,
        evaluation_intent: evaluationTurn.intent,
        outcome_level: outcomeLevel,
        objective_phase_after_evaluation: objectiveAfter.active_objective?.phase ?? null,
        persisted_after_renderer_reload: true,
      },
      live, restored, tablet, persisted_segments: turn.output_envelope.result.blackboard.segments.length,
      raw_family_media_uploaded: false, question_contains_family_data: false,
      screenshots: ['01-live-synchronized-blackboard.png', '02-restored-blackboard.png', '03-tablet-width-blackboard.png'],
    }
    await writeFile(join(outputDir, 'report.json'), `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 })
    console.log(JSON.stringify({ report: join(outputDir, 'report.json'), ...report }))
  } catch (error) {
    await writeFile(join(outputDir, 'failure.log'), `${error.stack ?? error}\n\n${logs.slice(-12_000)}`, { mode: 0o600 }).catch(() => undefined)
    throw error
  } finally {
    cdp?.close(); await stop(child); await rm(temporaryRoot, { recursive: true, force: true })
  }
}

main().catch((error) => { console.error(`[electron-tutor-blackboard-e2e] ${error.message}`); process.exit(1) })
