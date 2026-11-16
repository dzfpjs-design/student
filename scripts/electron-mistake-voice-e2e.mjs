// Isolated acceptance: a real local camera page, explicitly synthetic replies.
// Does not create observations, answers or diagnoses in the family database.
import { spawn } from 'node:child_process'
import { copyFile, mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir, homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { createServer } from 'node:net'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'
import { releaseChildProcessHandles } from './electron-owned-process.mjs'

const root = resolve(import.meta.dirname, '..')
const output = await mkdtemp(join(tmpdir(), 'hsc-mistake-voice-'))
const profile = join(output, 'profile')
await mkdir(profile)
for (const name of ['voice-settings.json', 'asr-settings.json']) {
  await copyFile(join(homedir(), 'Library/Application Support/home-study-companion', name), join(profile, name)).catch(() => {})
}
const port = await new Promise((resolvePort) => {
  const s = createServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolvePort(p)) })
})
const child = spawn(join(root, 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'), [root, `--remote-debugging-port=${port}`], {
  cwd: root, detached: true, stdio: ['ignore', 'ignore', 'ignore'], env: {
    ...sanitizedHarnessEnvironment(process.env), HSC_ELECTRON_USER_DATA_DIR: profile,
    HSC_DATA_DIR: join(output, 'data'), HSC_DATABASE_PATH: join(output, 'data', 'acceptance.db'),
    HSC_LOCAL_AGENT_PYTHON: join(root, '.venv/bin/python'), HSC_MODEL_KEYCHAIN_SERVICE: 'hsc-deepseek-api-key'
  }
})
const report = { synthetic_responses: true, actual_children_tested: false, raw_media_uploaded: false, output, checks: {} }
let ws, runtime, serial = 0
const pending = new Map()
async function eventually(fn, name, ms = 65_000) {
  const end = Date.now() + ms
  while (Date.now() < end) {
    const value = await fn().catch(() => null)
    if (value) return value
    await new Promise(r => setTimeout(r, 400))
  }
  throw new Error(`${name} timed out`)
}
function cdp(method, params = {}) {
  const id = ++serial
  return new Promise((resolveMessage, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timeout`)) }, 65_000)
    pending.set(id, { resolve: (value) => { clearTimeout(timer); resolveMessage(value) }, reject })
    ws.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? 'renderer error')
  return result.result.value
}
async function api(path, body, method = body ? 'POST' : 'GET') {
  const response = await fetch(runtime.localApiUrl + path, {
    method, headers: { 'X-HSC-Session': runtime.sessionToken, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(210_000)
  })
  const data = await response.json()
  if (!response.ok) throw new Error(`${path}: ${response.status} ${JSON.stringify(data.detail ?? { code: data.code, message: data.message }).slice(0, 300)}`)
  return data
}
const action = (name) => `mistake-e2e-${name}`
async function syntheticSpeech(sid, text) {
  const response = await fetch(`${runtime.localApiUrl}/v1/sessions/${sid}/voice/synthesize/stream`, {
    method: 'POST', headers: { 'X-HSC-Session': runtime.sessionToken, 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, client_action_id: action('synthetic-answer-audio') }), signal: AbortSignal.timeout(60_000)
  })
  if (!response.ok) throw new Error(`Voice fixture synthesis failed (${response.status})`)
  const framed = Buffer.from(await response.arrayBuffer()), chunks = []
  let offset = 0, sampleRate = null
  while (framed.length - offset >= 8) {
    const rate = framed.readUInt32LE(offset), size = framed.readUInt32LE(offset + 4)
    if (!rate || size <= 0 || size % 2 || offset + 8 + size > framed.length || (sampleRate && rate !== sampleRate)) throw new Error('Invalid voice PCM framing')
    sampleRate = rate; chunks.push(framed.subarray(offset + 8, offset + 8 + size)); offset += 8 + size
  }
  if (offset !== framed.length || !sampleRate || !chunks.length) throw new Error('Incomplete voice fixture')
  const pcm = Buffer.concat(chunks), header = Buffer.alloc(44)
  header.write('RIFF'); header.writeUInt32LE(36 + pcm.length, 4); header.write('WAVE', 8)
  header.write('fmt ', 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22)
  header.writeUInt32LE(sampleRate, 24); header.writeUInt32LE(sampleRate * 2, 28); header.writeUInt16LE(2, 32); header.writeUInt16LE(16, 34)
  header.write('data', 36); header.writeUInt32LE(pcm.length, 40)
  return Buffer.concat([header, pcm]).toString('base64')
}
try {
  const page = await eventually(async () => (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(x => x.type === 'page'), 'renderer')
  ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej })
  ws.onmessage = e => {
    const m = JSON.parse(e.data), handler = pending.get(m.id)
    if (handler) { pending.delete(m.id); if (m.error) handler.reject(new Error(m.error.message)); else handler.resolve(m.result) }
  }
  runtime = await eventually(async () => { const r = await evaluate('window.desktop?.getRuntimeConfig()'); return r?.status === 'ready' && r }, 'backend')
  const session = await api('/v1/sessions', { child_id: 'child-primary-4', client_action_id: action('session') })
  const sid = session.session_id
  await api(`/v1/sessions/${sid}/transition`, { expected_state: 'check_in', target_state: 'classroom_recall', reason: 'isolated acceptance', client_action_id: action('recall') })
  await api(`/v1/sessions/${sid}/recalls/none`, { client_action_id: action('no-recall') })
  await api(`/v1/sessions/${sid}/transition`, { expected_state: 'classroom_recall', target_state: 'homework_capture', reason: 'isolated acceptance', client_action_id: action('homework-state') })
  await api(`/v1/sessions/${sid}/homework`, { subject: '英语', title: '真实页面与合成回答连调', expected_minutes: 20, client_action_id: action('homework') })
  const plan = await api(`/v1/sessions/${sid}/plan/generate`, { client_action_id: action('plan') })
  await api(`/v1/sessions/${sid}/transition`, { expected_state: 'plan_review', target_state: 'study', reason: 'isolated acceptance', client_action_id: action('study') })
  await api(`/v1/plan-items/${plan.plan_items[0].plan_item_id}/start`, { client_action_id: action('start') })
  // Never fall back to the room camera. Only acquire the actual overhead camera.
  const capture = process.env.HSC_MISTAKE_FRAME ? { label: 'replay_of_prior_real_camera_frame',
    jpeg: (await readFile(process.env.HSC_MISTAKE_FRAME)).toString('base64') } : await evaluate(`(async () => {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const camera = devices.find(d => d.kind === 'videoinput' && /Fic760/i.test(d.label));
    if (!camera) throw new Error('Fic760_document_camera_unavailable');
    const stream = await navigator.mediaDevices.getUserMedia({audio:false, video:{deviceId:{exact:camera.deviceId}, width:{ideal:3264},height:{ideal:2448},resizeMode:{ideal:'none'},frameRate:{ideal:5,max:15}}});
    try {
      const video = document.createElement('video'); video.muted=true; video.srcObject=stream; await video.play();
      await new Promise(r=>setTimeout(r,3500));
      const canvas=document.createElement('canvas'); canvas.width=video.videoWidth; canvas.height=video.videoHeight;
      canvas.getContext('2d').drawImage(video,0,0);
      return {label:camera.label,width:canvas.width,height:canvas.height,jpeg:canvas.toDataURL('image/jpeg',.96).split(',')[1]};
    } finally {stream.getTracks().forEach(t=>t.stop())}
  })()`)
  report.checks.camera = { label: capture.label, width: capture.width, height: capture.height, live: !process.env.HSC_MISTAKE_FRAME }
  await writeFile(join(output, 'local-camera.jpg'), Buffer.from(capture.jpeg, 'base64'), { mode: 0o600 })
  const ocr = await api(`/v1/sessions/${sid}/ocr`, { image_base64: capture.jpeg, client_action_id: action('ocr') })
  const artifactId = ocr.artifact_id
  if (!artifactId) throw new Error(`OCR did not return an artifact (${Object.keys(ocr).join(',')})`)
  report.checks.ocr = { artifact_id: artifactId, status: ocr.status, pipeline: ocr.details?.local_pipeline }
  const candidates = await api(`/v1/capture-artifacts/${artifactId}/mistake-candidates/prepare`, { child_id: session.child_id, client_action_id: action('prepare') })
  // This is a live fixture selector, not a product parser or a replacement for OCR.
  const selected = candidates.items.find(x => /grape/i.test(x.question_text) && /make/i.test(x.question_text) && /like/i.test(x.question_text))
  if (!selected) throw new Error('Live page has no complete grape/make/like acceptance question; inspect local-camera.jpg and OCR candidates')
  await api(`/v1/sessions/${sid}/mistake-dialogue`, { artifact_id: artifactId, candidate_id: selected.candidate_id, client_action_id: action('begin') })
  report.checks.selected_question = selected.question_text
  const say = async (text, name) => {
    const turn = await api(`/v1/sessions/${sid}/agent-turn`, { transcript: text, client_action_id: action(name) })
    if (turn.action.type !== 'continue_mistake_dialogue') throw new Error(`Misrouted ${name}: ${turn.action.type}`)
    const result = await api(`/v1/agent-turns/${turn.agent_turn_id}/execute`, {})
    if (result.execution.teaching_error) throw new Error(`Real tutoring failed: ${result.execution.teaching_error}`)
    return result
  }
  await say('对', 'stem')
  const wav = await syntheticSpeech(sid, '我当时写的是B')
  const asr = await api(`/v1/sessions/${sid}/asr`, { wav_base64: wav, purpose: 'agent_command', client_action_id: action('asr-answer') })
  if (!asr.cloud_used || !asr.transcript) throw new Error('Configured cloud ASR did not recognize synthetic answer')
  report.checks.asr = { model: asr.model_version, cloud_used: asr.cloud_used, transcript: asr.transcript, source: 'synthetic_reference_voice_not_child_microphone' }
  await say(asr.transcript, 'answer')
  await evaluate('location.reload()')
  await eventually(() => evaluate("document.body.innerText.includes('当时的答案：B')"), 'saved answer UI recovery')
  report.checks.reload_before_save = true
  const saved = await say('是的', 'save')
  report.checks.mistake_id = saved.execution.result.mistake_id
  const taught = await say('我是看拼写选的，不知道比较读音的方法', 'diagnosis')
  const tutor = taught.execution.tutor_turn
  if (!tutor?.output_envelope?.result?.blackboard?.segments?.length) throw new Error('No actual tutor blackboard')
  report.checks.teaching = { provider: tutor.provider, model: tutor.model, segments: tutor.output_envelope.result.blackboard.segments.length }
  await evaluate(`window.dispatchEvent(new CustomEvent('hsc-tutor-presentation',{detail:{turn:${JSON.stringify(tutor)},resolve:()=>{}}}))`)
  await eventually(() => evaluate(`document.querySelectorAll('.blackboard-segment').length === ${tutor.output_envelope.result.blackboard.segments.length}`), 'complete voice blackboard', 180_000)
  const board = await evaluate("(() => {const node=document.querySelector('.tutor-blackboard');return {segments:node?.querySelectorAll('.blackboard-segment').length,content:node?.querySelectorAll('.blackboard-block:not(.is-heading)').length,text:node?.textContent}})()")
  if (!board.content || !/grape|make|like/i.test(board.text ?? '')) throw new Error('Blackboard has no content grounded in the selected question')
  report.checks.blackboard = { revealed_segments: board.segments, content_blocks: board.content, matches_question: true }
  await eventually(() => evaluate("(() => {const r=document.querySelector('.tutor-blackboard')?.getBoundingClientRect();return r && r.top >= 0 && r.top < innerHeight - 100})()"), 'automatic blackboard scrolling', 8000)
  // Capture the complete teaching surface after automatic scrolling settles.
  const bounds = await evaluate("(() => {const r=document.querySelector('.tutor-blackboard').getBoundingClientRect();return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height,scale:1}})()")
  const screenshot = await cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, clip: bounds })
  await writeFile(join(output, 'voice-blackboard.png'), Buffer.from(screenshot.data, 'base64'), { mode: 0o600 })
  await say('明白了', 'understood')
  for (let i=0; i<3; i++) {
    const focus = (await api(`/v1/sessions/${sid}/mistake-dialogue`)).dialogue
    const item = focus.state.next_activity?.item
    if (!item) throw new Error(`Missing independent question at ${focus.phase}`)
    const answer = item.prompt.includes('cake') ? 'bag 的读音不同，cake 和 name 相同' : item.prompt.includes('bike') ? 'sit 的读音不同，bike 和 five 相同' : 'hot 的读音不同，home 和 nose 相同'
    await say(answer, `attempt-${i}`)
  }
  const final = (await api(`/v1/sessions/${sid}/mistake-dialogue`)).dialogue
  if (final.phase !== 'scheduled' || final.state.next_activity.stage !== 'delayed_48h') throw new Error(`No spaced review: ${final.phase}`)
  report.checks.delayed_review = final.state.next_activity
  await evaluate('location.reload()')
  await eventually(() => evaluate("document.body.innerText.includes('后续复习')"), 'review UI restart recovery')
  report.checks.reload_after_review = true
  const status = await api('/v1/local-ai/status')
  report.checks.voice = { configured: status.voice?.configured, provider: status.voice?.provider }
  report.passed = true
} catch (error) {
  report.passed = false
  report.error = error.message
  process.exitCode = 1
} finally {
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 })
  ws?.close()
  if (child.pid && child.exitCode === null) {
    try { process.kill(-child.pid, 'SIGTERM') } catch {}
    await new Promise(r=>setTimeout(r,1000))
    if (child.exitCode === null) { try { process.kill(-child.pid, 'SIGKILL') } catch {} }
  }
  releaseChildProcessHandles(child)
  console.log(JSON.stringify({ passed: report.passed, error: report.error, report: join(output, 'report.json') }))
}
