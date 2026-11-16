/*
 * Visible, isolated Electron/CDP acceptance for resource-level KG review and
 * the child mistake -> approved guidance boundary.
 * It creates only fixed synthetic data under mkdtemp; no family DB, media,
 * transcript corpus, credentials, or shared desktop instance is used.
 */
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { mkdtemp, mkdir, rm, symlink, writeFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { join, delimiter } from 'node:path'
import { spawn } from 'node:child_process'
import { tmpdir } from 'node:os'
import { awaitCdpResponse, releaseChildProcessHandles, stopWindowsProcessTree, windowsProcessTree, windowsRemainingPids } from './electron-owned-process.mjs'

const root = process.cwd()
const dependencyRoot = process.env.HSC_E2E_DEPENDENCY_ROOT || root
const windows = process.platform === 'win32'
const python = process.env.HSC_E2E_PYTHON || (windows
  ? join(root, '.venv', 'Scripts', 'python.exe')
  : join(root, '.venv', 'bin', 'python'))
const requireDeps = createRequire(join(dependencyRoot, 'package.json'))
const { WebSocket } = requireDeps('ws')
const token = 'e2e-resource-review-session'
const owned = new Set()
let linkedNodeModules = false

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
async function freePort() {
  return await new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', reject).listen(0, '127.0.0.1', () => {
      const port = server.address().port
      server.close((error) => error ? reject(error) : resolve(port))
    })
  })
}
function start(command, args, options = {}) {
  const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'], ...options })
  owned.add(child)
  return child
}
async function stop(child) {
  if (!child || child.exitCode !== null) return
  if (windows) {
    // Electron owns renderer/GPU descendants.  Killing just the launch process
    // can leave an old renderer answering CDP on a reused port, which would
    // turn the restart assertion below into a false positive.
    await stopWindowsProcessTree(child, waitFor)
    releaseChildProcessHandles(child)
    owned.delete(child)
    return
  }
  child.kill('SIGTERM')
  await Promise.race([new Promise((resolve) => child.once('exit', resolve)), sleep(5_000)])
  if (child.exitCode === null) child.kill('SIGKILL')
  owned.delete(child)
}
async function closeElectronForRestart(cdp, child) {
  if (!child || child.exitCode !== null) return { forced_cleanup: false, cim_survivors: [] }
  if (!windows) {
    // DevTools can close its socket before acknowledging Browser.close on
    // macOS as well. Do not let a missing CDP reply strand the E2E process.
    await Promise.race([cdp.command('Browser.close').catch(() => undefined), sleep(3_000)])
    await stop(child)
    return { forced_cleanup: false, cim_survivors: [] }
  }
  const tracked = await windowsProcessTree(child.pid)
  if (!tracked.includes(child.pid)) tracked.push(child.pid)
  try {
    // DevTools may disconnect before replying to Browser.close; a normal
    // close request must therefore be bounded independently of process exit.
    await Promise.race([cdp.command('Browser.close'), sleep(3_000)])
    await waitFor(() => child.exitCode !== null, 'graceful Electron main-process shutdown', 12_000)
    const survivors = await windowsRemainingPids(tracked)
    if (survivors.length) throw new Error(`Electron descendants survived graceful close: ${survivors.join(',')}`)
    releaseChildProcessHandles(child)
    owned.delete(child)
    return { forced_cleanup: false, cim_survivors: [], tracked_process_count: tracked.length }
  } catch {
    // A forced kill is recovery-only evidence, never a normal app restart.
    await stopWindowsProcessTree(child, waitFor)
    releaseChildProcessHandles(child)
    owned.delete(child)
    return { forced_cleanup: true, cim_survivors: [], tracked_process_count: tracked.length }
  }
}
async function waitFor(check, label, timeout = 25_000) {
  const deadline = Date.now() + timeout
  let last
  while (Date.now() < deadline) {
    try { const value = await check(); if (value) return value } catch (error) { last = error }
    await sleep(160)
  }
  throw new Error(`${label} timed out${last ? `: ${last.message}` : ''}`)
}
async function json(url, init = {}) {
  const response = await fetch(url, { ...init, headers: { 'X-HSC-Session': token, ...(init.headers || {}) } })
  if (!response.ok) throw new Error(`${init.method || 'GET'} ${url} -> ${response.status}: ${await response.text()}`)
  return response.json()
}
async function seed(database) {
  const script = String.raw`
from pathlib import Path
from uuid import uuid4
from home_study_agent.database import Database
from home_study_agent.knowledge_graph.catalog_bootstrap import CatalogGraphBootstrapper
from home_study_agent.knowledge_graph.service import KnowledgeGraphService
from home_study_agent.repository import utc_now
db=Database(Path(r'''${database}''')); db.initialize(); CatalogGraphBootstrapper(db).run(rebuild_local_candidates=False); graph=KnowledgeGraphService(db)
with db.connect() as c: node=c.execute("SELECT node_id FROM kg_node WHERE canonical_key='starter:J3.MATH.QUADRATIC.DISCRIMINANT'").fetchone()[0]
now=utc_now(); corpus=str(uuid4()); resource=str(uuid4()); segment=str(uuid4())
with db.connect() as c:
 c.execute("INSERT OR IGNORE INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child-junior-3','小辰','初三','江苏南京','active',?,?)",(now,now))
 c.execute("INSERT INTO kg_corpus(corpus_id,title,source_kind,transcript_root,video_root,metadata_db_path,source_ref,license_boundary,review_status,version_label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(corpus,'Electron 本机验收','fixture','fixture',None,None,'fixture','user_local','approved','v1',now,now))
 c.execute("INSERT INTO kg_resource(resource_id,corpus_id,external_id,title,grade_scope,subject_scope,semester_scope,transcript_relative_path,video_relative_path,transcript_sha256,video_sha256,video_fingerprint,metadata_json,source_ref,license_boundary,confidence,review_status,version_label,ingested_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(resource,corpus,'e2e-lesson','本机课程：求根公式','初三','数学',None,'fixture.txt',None,'fixture-sha',None,None,'{}','fixture-source','user_local',1,'approved','v1',now,now))
 text='求根公式用于一元二次方程。先识别系数，再计算判别式。联系电话 19900000001 不应在预览中显示。'
 c.execute("INSERT INTO kg_segment(segment_id,resource_id,ordinal,start_char,end_char,text,text_sha256,source_ref,confidence,review_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(segment,resource,0,0,len(text),text,'segment-sha','fixture-source',1,'approved',now))
 c.execute("INSERT INTO kg_segment_occurrence(resource_id,segment_id,ordinal,start_char,end_char) VALUES(?,?,?,?,?)",(resource,segment,0,0,len(text)))
 c.execute("INSERT INTO kg_segment_fts(segment_id,text,title,subject_scope,grade_scope) VALUES(?,?,?,?,?)",(segment,text,'本机课程：求根公式','数学','初三'))
 c.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('e2e-study','child-junior-3','active','study',?,?,?)",(now,now,'e2e-study-action'))
 c.execute("INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id) VALUES('e2e-plan','child-junior-3','e2e-study',date('now'),'active',20,'fixture',1,?,?)",(now,'e2e-plan-action'))
 c.execute("INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,completion_standard,cognitive_load,task_type) VALUES('e2e-plan-item','e2e-plan','数学','判别式巩固',20,1,'pending',1,'school_homework','完成并检查','medium','written')")
 c.commit()
graph.map_segment(resource,segment,node,'explains',confidence=.95,source_ref='fixture-mapper')
print(node)
`
  const child = start(python, ['-c', script], { cwd: root, env: { ...process.env, PYTHONPATH: join(root, 'services/local_agent/src') } })
  let output = ''; child.stdout.on('data', (chunk) => { output += chunk })
  let error = ''; child.stderr.on('data', (chunk) => { error += chunk })
  const code = await new Promise((resolve) => child.once('exit', resolve)); owned.delete(child)
  if (code !== 0) throw new Error(`fixture seed failed: ${error}`)
  return output.trim()
}
async function connectCdp(port) {
  const targets = await waitFor(async () => {
    const response = await fetch(`http://127.0.0.1:${port}/json/list`)
    if (!response.ok) return null
    return (await response.json()).find((target) => target.type === 'page')
  }, 'Electron CDP page')
  const socket = new WebSocket(targets.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.once('open', resolve); socket.once('error', reject) })
  let sequence = 0; const pending = new Map()
  socket.on('message', (raw) => { const body = JSON.parse(String(raw)); const item = pending.get(body.id); if (item) { pending.delete(body.id); body.error ? item.reject(new Error(body.error.message)) : item.resolve(body.result) } })
  const command = (method, params = {}) => {
    const id = ++sequence
    const response = awaitCdpResponse(pending, id, method)
    socket.send(JSON.stringify({ id, method, params }))
    return response
  }
  return { socket, command }
}
async function visible(cdp, label) {
  try {
    await waitFor(async () => {
      const result = await cdp.command('Runtime.evaluate', { expression: `document.body.innerText.includes(${JSON.stringify(label)})`, returnByValue: true })
      return result.result.value
    }, `visible UI: ${label}`)
  } catch (error) {
    const snapshot = await cdp.command('Runtime.evaluate', { expression: 'document.body.innerText.slice(0,600)', returnByValue: true })
    throw new Error(`${error.message}; visible page text: ${snapshot.result.value || '(empty)'}`)
  }
}
async function clickButton(cdp, label) {
  await waitFor(async () => (await cdp.command('Runtime.evaluate', { expression: `Array.from(document.querySelectorAll('button')).some((b)=>!b.disabled && (b.textContent?.trim()===${JSON.stringify(label)} || b.textContent?.includes(${JSON.stringify(label)})))`, returnByValue: true })).result.value, `button: ${label}`)
  await cdp.command('Runtime.evaluate', { expression: `(() => { const button=Array.from(document.querySelectorAll('button')).find((b)=>!b.disabled && (b.textContent?.trim()===${JSON.stringify(label)} || b.textContent?.includes(${JSON.stringify(label)}))); button?.scrollIntoView({block:'center'}); button?.click() })()`, awaitPromise: true })
}
async function ensureChildStudyWorkspace(cdp) {
  const alreadyOpen = await waitFor(async () => {
    const result = await cdp.command('Runtime.evaluate', {
      expression: `({
        workspace: document.body.innerText.includes('能力挑战'),
        resumable: Array.from(document.querySelectorAll('button')).some((button) => !button.disabled && button.textContent?.includes('继续学习'))
      })`,
      returnByValue: true,
    })
    return result.result.value.workspace || result.result.value.resumable ? result.result.value : false
  }, 'automatic child session recovery')
  if (!alreadyOpen.workspace) await clickButton(cdp, '继续学习')
  await visible(cdp, '能力挑战')
}
async function clickTitle(cdp, title) {
  await waitFor(async () => (await cdp.command('Runtime.evaluate', { expression: `Boolean(document.querySelector(${JSON.stringify(`[title="${title}"]`)}))`, returnByValue: true })).result.value, `title control: ${title}`)
  await cdp.command('Runtime.evaluate', { expression: `document.querySelector(${JSON.stringify(`[title="${title}"]`)})?.click()`, awaitPromise: true })
}
async function setTextarea(cdp, label, value) {
  const selector = '.diagnostic-panel textarea'
  await waitFor(async () => (await cdp.command('Runtime.evaluate', { expression: `Boolean(document.querySelector(${JSON.stringify(selector)}))`, returnByValue: true })).result.value, `textarea: ${label}`)
  await cdp.command('Runtime.evaluate', { expression: `(() => { const input=document.querySelector(${JSON.stringify(selector)}); const set=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set; set.call(input,${JSON.stringify(value)}); input.dispatchEvent(new Event('input',{bubbles:true})) })()`, awaitPromise: true })
}
async function submitPin(cdp, buttonLabel) {
  await waitFor(async () => (await cdp.command('Runtime.evaluate', { expression: 'Boolean(document.querySelector(\'input[aria-label="家长 PIN"]\'))', returnByValue: true })).result.value, 'parent PIN input')
  await cdp.command('Runtime.evaluate', { expression: `(() => { const input=document.querySelector('input[aria-label="家长 PIN"]'); const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; set.call(input,'2468'); input.dispatchEvent(new Event('input',{bubbles:true})) })()`, awaitPromise: true })
  await clickButton(cdp, buttonLabel)
}
async function startAgent(dataDir, database, port) {
  const child = start(python, ['-m', 'home_study_agent', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: root, env: { ...process.env, PYTHONPATH: join(root, 'services/local_agent/src'), HSC_DATA_DIR: dataDir, HSC_DATABASE_PATH: database, HSC_SESSION_TOKEN: token }
  })
  let stderr = ''
  child.stderr.on('data', (chunk) => { stderr += chunk; if (stderr.length > 12_000) stderr = stderr.slice(-12_000) })
  child.e2eStderr = () => stderr
  await waitFor(async () => {
    if (child.exitCode !== null) throw new Error(`local Agent exited: ${stderr || 'no stderr'}`)
    const headers = { 'X-HSC-Session': token }
    const health = await fetch(`http://127.0.0.1:${port}/v1/health`, { headers })
    if (!health.ok) return false
    if (child.exitCode !== null) throw new Error(`local Agent exited after health: ${stderr || 'no stderr'}`)
    const bootstrap = await fetch(`http://127.0.0.1:${port}/v1/bootstrap`, { headers })
    if (child.exitCode !== null) throw new Error(`local Agent exited during bootstrap: ${stderr || 'no stderr'}`)
    return bootstrap.ok
  }, 'local agent health and bootstrap readiness', 70_000)
  return child
}
async function buildElectron() {
  // Running the package's JavaScript entrypoint avoids cmd.exe quote parsing
  // for Windows paths (and does not need Node's deprecated shell option).
  const cli = join(dependencyRoot, 'node_modules', 'electron-vite', 'bin', 'electron-vite.js')
  const child = start(process.execPath, [cli, 'build'], { cwd: root, env: { ...process.env } })
  let stderr = ''; child.stderr.on('data', (chunk) => { stderr += chunk })
  const code = await new Promise((resolve) => child.once('exit', resolve)); owned.delete(child)
  if (code !== 0) throw new Error(`Electron production build failed: ${stderr.slice(-2000)}`)
}
async function startElectron(profile, agentPort, cdpPort) {
  const command = process.env.HSC_E2E_ELECTRON_BINARY || (windows
    ? join(dependencyRoot, 'node_modules', 'electron', 'dist', 'Electron.exe')
    : join(dependencyRoot, 'node_modules', 'electron', 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron'))
  const child = start(command, [root, `--remote-debugging-port=${cdpPort}`], {
    cwd: root,
    env: { ...process.env, HSC_ELECTRON_USER_DATA_DIR: profile, HSC_LOCAL_API_URL: `http://127.0.0.1:${agentPort}`, HSC_SESSION_TOKEN: token, ELECTRON_ENABLE_LOGGING: '1' }
  })
  let stderr = ''; child.stderr.on('data', (chunk) => { stderr += chunk; if (stderr.length > 12_000) stderr = stderr.slice(-12_000) })
  child.e2eStderr = () => stderr
  await waitFor(async () => child.exitCode === null || Promise.reject(new Error(stderr || 'Electron exited')), 'Electron startup')
  return child
}

const started = Date.now(); const reportDir = await mkdtemp(join(tmpdir(), 'hsc-resource-review-e2e-')); const profile = join(reportDir, 'profile'); const dataDir = join(reportDir, 'data'); const database = join(dataDir, 'home-study.db')
let agent; let electron; let cdp; let restartCleanup = { forced_cleanup: false, cim_survivors: [] }
try {
  await mkdir(profile); await mkdir(dataDir)
  if (!existsSync(join(root, 'node_modules'))) { await symlink(join(dependencyRoot, 'node_modules'), join(root, 'node_modules')); linkedNodeModules = true }
  const nodeId = await seed(database); const agentPort = await freePort(); const firstCdpPort = await freePort()
  await buildElectron()
  agent = await startAgent(dataDir, database, agentPort)
  electron = await startElectron(profile, agentPort, firstCdpPort); cdp = await connectCdp(firstCdpPort)
  // A real child action creates a host-scored mistake first.  With only a
  // pending resource mapping, the child gets feedback but no KG material UI.
  await ensureChildStudyWorkspace(cdp)
  await clickButton(cdp, '来一道题')
  await setTextarea(cdp, '输入挑战题答案', '四个根')
  await clickButton(cdp, '提交答案'); await visible(cdp, '判别式为16-16=0')
  const beforeApproval = (await cdp.command('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true })).result.value
  if (beforeApproval.includes('根据这次错题，下一步这样学')) throw new Error('pending mapping leaked into child guidance')
  await clickTitle(cdp, '返回首页')
  await clickButton(cdp, '家长中心'); await visible(cdp, '家长 PIN')
  await submitPin(cdp, '设置并进入'); await visible(cdp, '根的判别式')
  await clickButton(cdp, '查看本机证据'); await visible(cdp, '片段 1')
  const preview = (await cdp.command('Runtime.evaluate', { expression: 'document.body.innerText', returnByValue: true })).result.value
  if (preview.includes('19900000001')) throw new Error('redaction failure: phone number appeared in visible evidence')
  await cdp.command('Page.captureScreenshot', { format: 'png' }).then((shot) => writeFile(join(reportDir, 'evidence-preview.png'), Buffer.from(shot.data, 'base64')))
  await clickButton(cdp, '接受'); await visible(cdp, '已接受')
  await waitFor(async () => (await json(`http://127.0.0.1:${agentPort}/v1/children/child-junior-3/knowledge-graph/learning-path?node_id=${nodeId}`)).materials?.length === 1, 'approved child material')
  await cdp.command('Page.captureScreenshot', { format: 'png' }).then((shot) => writeFile(join(reportDir, 'approved-review.png'), Buffer.from(shot.data, 'base64')))
  // End the parent session before the process restart.  The restart must prove
  // both the durable approval and a new child-facing renderer, not keep using
  // the parent dashboard that happened to be open at process termination.
  await clickTitle(cdp, '返回孩子首页'); await visible(cdp, '继续今天的学习')
  restartCleanup = await closeElectronForRestart(cdp, electron); electron = null; cdp.socket.close(); cdp = null
  try {
    await fetch(`http://127.0.0.1:${agentPort}/v1/runtime/shutdown`, {
      method: 'POST', headers: { 'X-HSC-Session': token }, signal: AbortSignal.timeout(5_000)
    })
  } catch {
    // The service may close its socket before responding; teardown continues.
  } finally {
    await stop(agent); agent = null
  }
  agent = await startAgent(dataDir, database, agentPort)
  // This is intentionally checked again immediately before a distinct
  // Electron/DevTools instance: a closed DevTools port or a child that exits
  // between readiness and launch must not masquerade as a UI timeout.
  await waitFor(async () => agent.exitCode === null && (await fetch(`http://127.0.0.1:${agentPort}/v1/bootstrap`, { headers: { 'X-HSC-Session': token } })).ok, 'restart agent bootstrap before Electron', 70_000)
  const secondCdpPort = await freePort()
  electron = await startElectron(profile, agentPort, secondCdpPort); cdp = await connectCdp(secondCdpPort)
  await ensureChildStudyWorkspace(cdp)
  await clickButton(cdp, '来一道题')
  await setTextarea(cdp, '输入挑战题答案', '四个根')
  await clickButton(cdp, '提交答案'); await visible(cdp, '根据这次错题，下一步这样学')
  await visible(cdp, '本机课程：求根公式')
  await visible(cdp, '方程 x²-4x+4=0')
  const childGuidance = (await cdp.command('Runtime.evaluate', { expression: 'document.querySelector("[aria-label=\\"已批准学习建议\\"]")?.innerText || ""', returnByValue: true })).result.value
  if (childGuidance.includes('19900000001')) throw new Error('child guidance leaked an unredacted local segment')
  await cdp.command('Page.captureScreenshot', { format: 'png' }).then((shot) => writeFile(join(reportDir, 'approved-child-guidance.png'), Buffer.from(shot.data, 'base64')))
  await clickTitle(cdp, '返回首页')
  await clickButton(cdp, '家长中心'); await visible(cdp, '家长 PIN')
  await submitPin(cdp, '解锁'); await visible(cdp, '最近审核')
  await clickButton(cdp, '撤销此决定'); await visible(cdp, '根的判别式')
  await waitFor(async () => (await cdp.command('Runtime.evaluate', { expression: 'document.querySelector("[aria-label=\\"最近资源级审核\\"]") === null', returnByValue: true })).result.value, 'durable history removed after undo')
  await waitFor(async () => {
    const response = await fetch(`http://127.0.0.1:${agentPort}/v1/children/child-junior-3/knowledge-graph/learning-path?node_id=${nodeId}`, { headers: { 'X-HSC-Session': token } })
    return response.status === 422
  }, 'withdrawn child learning-path denied')
  const materials = await json(`http://127.0.0.1:${agentPort}/v1/children/child-junior-3/knowledge-graph/materials/search?query=%E6%B1%82%E6%A0%B9%E5%85%AC%E5%BC%8F`)
  if (materials.items.length) throw new Error('withdrawn material was still visible to child search')
  await cdp.command('Page.captureScreenshot', { format: 'png' }).then((shot) => writeFile(join(reportDir, 'withdrawn-review.png'), Buffer.from(shot.data, 'base64')))
  const report = { status: 'passed', synthetic_fixture: true, evidence_opened: true, phone_redacted: true, child_guidance_preview_redacted: true, pending_mapping_hidden_from_child_guidance: true, approved_before_restart: true, child_guidance_after_parent_approval_and_restart: true, history_after_agent_and_app_restart: true, restart_cleanup: restartCleanup, withdrawn_card_reappeared: true, withdrawn_history_removed: true, withdrawn_child_learning_path_status: 422, withdrawn_child_search_count: 0, elapsed_ms: Date.now() - started }
  await writeFile(join(reportDir, 'report.json'), JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ report: join(reportDir, 'report.json'), screenshots: [join(reportDir, 'evidence-preview.png'), join(reportDir, 'approved-review.png'), join(reportDir, 'approved-child-guidance.png'), join(reportDir, 'withdrawn-review.png')], ...report }))
} catch (error) {
  const agentStderr = agent?.e2eStderr?.() ?? ''
  const electronStderr = electron?.e2eStderr?.() ?? ''
  const diagnostics = { agent_stderr: 'agent.stderr.log', electron_stderr: 'electron.stderr.log' }
  await Promise.all([
    writeFile(join(reportDir, diagnostics.agent_stderr), agentStderr),
    writeFile(join(reportDir, diagnostics.electron_stderr), electronStderr)
  ]).catch(() => undefined)
  await writeFile(join(reportDir, 'report.json'), JSON.stringify({ status: 'failed', synthetic_fixture: true, error: error instanceof Error ? error.message : String(error), diagnostics }, null, 2)).catch(() => undefined)
  console.error(`resource review Electron acceptance failed; report: ${join(reportDir, 'report.json')}`)
  throw error
} finally {
  if (cdp) cdp.socket.close()
  await Promise.all([...owned].map(stop))
  if (linkedNodeModules) await rm(join(root, 'node_modules'), { force: true })
}
