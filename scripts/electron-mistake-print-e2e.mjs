// Isolated production-component acceptance. Only public synthetic records are used.
// The OS save chooser is controlled by this harness; PDF generation and the native
// print handler, renderer component, HTTP router, and SQLite service remain real.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { open, mkdir, mkdtemp, readFile, writeFile, stat, copyFile, realpath } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { build } from 'esbuild'
import { sanitizedHarnessEnvironment } from './electron-harness-env.mjs'

const root = resolve(import.meta.dirname, '..')
const output = await realpath(await mkdtemp(join(tmpdir(), 'hsc-mistake-print-')))
const report = { status: 'running', public_synthetic_data: true, os_save_chooser: 'controlled',
  real_pdf_engine: false, physical_printing: false, windows_verified: false, checks: {}, output }
await mkdir(join(output, 'profile'))
const checkpoint = () => writeFile(join(output, 'summary.json'), JSON.stringify(report, null, 2))
await checkpoint()
const backendOnly = process.argv.includes('--backend-only')
const availablePort = () => new Promise((resolvePort, reject) => {
  const server = createServer().listen(0, '127.0.0.1', () => {
    const port = server.address().port; server.close(() => resolvePort(port))
  })
  server.once('error', reject)
})
let apiPort, debugPort, apiUrl
const sessionToken = 'public-mistake-print-acceptance'
const childId = 'child-primary-4'
const environment = { ...sanitizedHarnessEnvironment(process.env), PYTHONPATH: join(root, 'services/local_agent/src') }
delete environment.ELECTRON_RUN_AS_NODE
const python = join(root, '.venv/bin/python')
const pythonSource = String.raw`
import json, socket, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.mistake_learning import MistakeLearningService
from home_study_agent.mistake_learning_http import create_router
from home_study_agent.mistake_learning_intake import MistakeLearningIntake
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService

scope = Path(sys.argv[1])
db = Database(scope / 'acceptance.db')
db.initialize()
PrivacyLifecycleService(db, scope).initialize()
repo = StudyRepository(db)
repo.seed_family()
service = MistakeLearningService(db, None)
intake = MistakeLearningIntake(db)
original_connect = socket.socket.connect
def local_only(self, address):
    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):
        raise RuntimeError('ACCEPTANCE_EXTERNAL_NETWORK_FORBIDDEN')
    return original_connect(self, address)
socket.socket.connect = local_only
fixture = scope / 'fixture.json'
if not fixture.exists():
    sid = repo.start_session('child-primary-4', 'public-print-session')['session_id']
    rows = []
    questions = [
      ('math', '数学', '计算3+4。', '八'),
      ('english', '英语', '把 apple 翻译成中文。', '梨'),
      ('long', '数学', '阅读材料，列出分数应用题的解题过程。\n' + '\n'.join('材料第%d段：全班同学把一张长方形纸平均分成十二份，观察每份的大小，并说明分母和分子的意义。请写出推理依据和计算过程。' % n for n in range(1, 32)), ''),
      ('old', '数学', '计算5×6。', '十一'),
    ]
    for key, subject, question, answer in questions:
        row = intake.create(child_id='child-primary-4', session_id=sid, question=question,
          submitted_answer=answer, subject=subject, client_action_id='public-print-' + key)
        rows.append({'key': key, 'mistake_id': row['mistake_id']})
    mid = rows[0]['mistake_id']
    for n, answer in enumerate(('八', '七')):
        service.review(mid, child_id='child-primary-4', session_id=sid,
          client_action_id='public-print-review-' + str(n), modality='voice', utterance=answer)
    analysis = {'summary':'检查基本运算。', 'wrong_causes':[{'label':'运算错误','explanation':'把加法结果算成了八。'}],
      'knowledge_points':[{'label':'整数加法','description':'数出相加后的总数。'}], 'correction_strategy':['重新计算并检查。']}
    with db.connect() as c:
        c.execute('UPDATE mistake_learning_state SET analysis_json=? WHERE mistake_id=?', (json.dumps(analysis, ensure_ascii=False), mid))
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        c.execute('UPDATE mistake_learning_card SET created_at=? WHERE mistake_id=?', (old, rows[-1]['mistake_id']))
        c.commit()
    intake.create(child_id='child-junior-3', question='其他孩子专属题，不得打印在本页。', subject='数学', client_action_id='public-print-other-child')
    fixture.write_text(json.dumps({'session_id':sid,'cards':rows}, ensure_ascii=False))
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=['null'], allow_methods=['GET'], allow_headers=['Content-Type','X-HSC-Session'])
app.include_router(create_router(service, authorize_request=lambda request: request.headers.get('X-HSC-Session') == 'public-mistake-print-acceptance'))
if len(sys.argv) > 3 and sys.argv[3] == 'snapshot':
    from fastapi.testclient import TestClient
    with db.connect() as c:
        before = '\n'.join(c.iterdump())
    with TestClient(app) as client:
        response = client.get('/v1/children/child-primary-4/mistake-learning/print', headers={'X-HSC-Session':'public-mistake-print-acceptance'})
        assert response.status_code == 200
        assert client.get('/v1/children/child-primary-4/mistake-learning/print').status_code == 403
        other = client.get('/v1/children/child-junior-3/mistake-learning/print', headers={'X-HSC-Session':'public-mistake-print-acceptance'}).json()
        assert other['total'] == 1 and '其他孩子专属题' in other['items'][0]['record']['source']['question']
    with db.connect() as c:
        assert before == '\n'.join(c.iterdump())
    (scope / 'backend-snapshot.json').write_text(json.dumps(response.json(), ensure_ascii=False))
    sys.exit(0)
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[2]), access_log=False, log_level='warning')
`
await writeFile(join(output, 'backend.py'), pythonSource)
let backend, electron, ws, serial = 0
const logs = []
const pending = new Map()
async function startBackend() {
  const log = await open(join(output, `backend-${logs.length}.log`), 'a'); logs.push(log)
  backend = spawn(python, [join(output, 'backend.py'), output, String(apiPort)], {
    cwd: root, env: environment, stdio: ['ignore', log.fd, log.fd],
  })
  await eventually(async () => {
    if (backend.exitCode !== null) throw Error('Backend exited; inspect the local acceptance log.')
    return await api().catch(() => null)
  }, 'backend ready', 90000)
}
async function eventually(fn, label, timeout = 15000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    const result = await fn()
    if (result) return result
    await new Promise(resolveDelay => setTimeout(resolveDelay, 150))
  }
  throw Error(`${label}: deadline exceeded`)
}
async function exited(process, label) {
  if (!process || process.exitCode !== null) return
  await new Promise((resolveExit, reject) => {
    const timer = setTimeout(() => reject(Error(`${label} did not stop`)), 12000)
    process.once('exit', () => { clearTimeout(timer);resolveExit() })
  })
}
async function api() {
  const response = await fetch(`${apiUrl}/v1/children/${childId}/mistake-learning/print`, {
    headers: { 'X-HSC-Session': sessionToken }, signal: AbortSignal.timeout(1500),
  })
  if (!response.ok) throw Error(`Print API: ${response.status}`)
  return response.json()
}
function cdp(method, params = {}) {
  const id = ++serial
  return new Promise((resolveReply, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(Error(`${method} timed out`)) }, 20000)
    pending.set(id, { resolve: result => { clearTimeout(timer); resolveReply(result) }, reject })
    ws.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const response = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (response.exceptionDetails) throw Error(response.exceptionDetails.exception?.description ?? 'Renderer exception')
  return response.result.value
}
async function click(text) {
  assert.equal(await evaluate(`(() => { const button = [...document.querySelectorAll('button')].find(e => e.textContent.trim() === ${JSON.stringify(text)}); if (!button || button.disabled) return false; button.click(); return true })()`), true, `click ${text}`)
}
async function changeLabel(label, value) {
  assert.equal(await evaluate(`(() => { const label = [...document.querySelectorAll('label')].find(e => e.textContent.startsWith(${JSON.stringify(label)})); const element = label?.querySelector('select,input'); if (!element) return false; const prototype = element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(prototype,'value').set.call(element,${JSON.stringify(value)}); element.dispatchEvent(new Event(element instanceof HTMLSelectElement?'change':'input',{bubbles:true})); return true })()`), true, `change ${label}`)
}
async function text() { return evaluate('document.body.innerText') }

try {
  if (backendOnly) {
    report.real_pdf_engine = false
    const snapshots = []
    for (let round = 0; round < 2; round++) {
      const log = await open(join(output, `backend-only-${round}.log`), 'a');logs.push(log)
      const job = spawn(python, [join(output, 'backend.py'), output, '0', 'snapshot'], { cwd: root, env: environment, stdio: ['ignore', log.fd, log.fd] })
      const code = await new Promise(resolveExit => job.once('exit', resolveExit))
      assert.equal(code, 0, `backend ASGI round ${round}; inspect local log`)
      snapshots.push(JSON.parse(await readFile(join(output, 'backend-snapshot.json'), 'utf8')))
    }
    assert.equal(snapshots[0].total, 4)
    const reviewed = snapshots[0].items.find(item => item.record.source.question === '计算3+4。')
    assert.equal(reviewed.review_count, 2)
    assert.equal(reviewed.mastery.status, 'retest_pending')
    assert.equal(reviewed.record.analysis.wrong_causes[0].label, '运算错误')
    assert.deepEqual(snapshots[1].items, snapshots[0].items)
    report.checks = { backend_asgi_auth_and_child_isolation: true, read_only_snapshot: true, saved_review_count: true, process_restart: true }
    report.status = 'backend_only_passed'
  } else {
  apiPort = await availablePort();debugPort = await availablePort();apiUrl = `http://127.0.0.1:${apiPort}`
  await startBackend()
  const before = await api()
  assert.equal(before.total, 4)
  const reviewed = before.items.find(item => item.record.source.question === '计算3+4。')
  assert.equal(reviewed.review_count, 2)
  assert.equal(reviewed.mastery.status, 'retest_pending')
  assert.equal(reviewed.record.analysis.wrong_causes[0].label, '运算错误')
  report.checks.persisted_source = true
  const renderer = `import React from 'react'; import {createRoot} from 'react-dom/client'; import {MistakeBookPrint} from ${JSON.stringify(join(root, 'src/renderer/src/MistakeBookPrint.tsx'))}; import ${JSON.stringify(join(root, 'src/renderer/src/styles.css'))}; import ${JSON.stringify(join(root, 'src/renderer/src/cloudMistakeLearning.css'))}; createRoot(document.getElementById('root')).render(<MistakeBookPrint child={{child_id:'child-primary-4',nickname:'公开打印同学',grade:'四年级',curriculum_region:'公开测试',status:'active'}} onClose={()=>{}}/>);`
  await build({ stdin: { contents: renderer, loader: 'tsx', resolveDir: root }, bundle: true, platform: 'browser', format: 'iife', jsx: 'automatic', outfile: join(output, 'renderer.js'), logLevel: 'silent' })
  await build({ entryPoints: [join(root, 'src/preload/index.ts')], bundle: true, platform: 'node', format: 'cjs', external: ['electron'], outfile: join(output, 'preload.cjs'), logLevel: 'silent' })
  await writeFile(join(output, 'index.html'), '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>合成错题本打印验收</title><link rel="stylesheet" href="renderer.css"><body><div id="root"></div><script src="renderer.js"></script></body></html>')
  const main = `import {app,BrowserWindow,ipcMain,dialog} from 'electron'; import {existsSync,readFileSync,writeFileSync} from 'node:fs'; import {createMistakeBookPrintHandler} from ${JSON.stringify(join(root, 'src/main/mistake-book-print.ts'))};
    app.setPath('userData',${JSON.stringify(join(output, 'profile'))}); app.commandLine.appendSwitch('remote-debugging-port',${JSON.stringify(String(debugPort))});
    await app.whenReady(); let owner=new BrowserWindow({show:true,width:1280,height:1000,webPreferences:{preload:${JSON.stringify(join(output, 'preload.cjs'))},sandbox:true,contextIsolation:true,nodeIntegration:false}});
    ipcMain.handle('runtime:get-config',()=>({status:'ready',localApiUrl:${JSON.stringify(apiUrl)},sessionToken:${JSON.stringify(sessionToken)}}));
    const handler=createMistakeBookPrintHandler(()=>owner); ipcMain.handle('mistake-book:print',handler);
    dialog.showSaveDialog=async()=>{const mode=existsSync(${JSON.stringify(join(output, 'cancel-chooser'))});return mode?{canceled:true}:{canceled:false,filePath:${JSON.stringify(join(output, 'notebook.pdf'))}}};
    owner.webContents.on('render-process-gone',()=>{writeFileSync(${JSON.stringify(join(output, 'renderer-crashed'))},'crashed');app.quit()});
    setInterval(()=>{if(existsSync(${JSON.stringify(join(output, 'stop-electron'))}))app.quit()},150).unref();
    await owner.loadFile(${JSON.stringify(join(output, 'index.html'))}); app.on('window-all-closed',()=>app.quit());`
  await build({ stdin: { contents: main, loader: 'ts', resolveDir: root }, bundle: true, platform: 'node', format: 'esm', external: ['electron'], outfile: join(output, 'main.mjs'), logLevel: 'silent' })
  const electronLog = await open(join(output, 'electron.log'), 'a'); logs.push(electronLog)
  electron = spawn(join(root, 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'), [join(output, 'main.mjs')], { cwd: root, env: environment, stdio: ['ignore', electronLog.fd, electronLog.fd] })
  const page = await eventually(async () => {
    if (electron.exitCode !== null) throw Error('Electron exited; inspect the local acceptance log.')
    return fetch(`http://127.0.0.1:${debugPort}/json/list`).then(r=>r.json()).then(pages=>pages.find(page=>page.type==='page')).catch(()=>null)
  }, 'Electron renderer', 25000)
  ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise(resolveOpen => ws.addEventListener('open', resolveOpen, { once: true }))
  ws.addEventListener('message', event => {
    const data = JSON.parse(event.data), waiter = pending.get(data.id)
    if (!waiter) return
    pending.delete(data.id)
    if (data.error) waiter.reject(Error(data.error.message)); else waiter.resolve(data.result)
  })
  await eventually(async () => (await text()).includes('计算3+4'), 'print rows')
  // Semantic controls below exercise the production component, never a mock page.
  await changeLabel('学科', '数学')
  await changeLabel('日期', 'today')
  await eventually(async () => (await text()).includes('匹配 2 题'), 'subject/date filtering')
  await changeLabel('最少复习次数', '2')
  await eventually(async () => (await text()).includes('匹配 1 题'), 'review count filtering')
  await changeLabel('掌握', 'retest_pending')
  await click('取消当前选择')
  assert.equal(await evaluate(`Array.from(document.querySelectorAll('button')).find(e=>e.textContent.trim()==='生成打印预览').disabled`), true)
  await click('全选筛选结果')
  await eventually(async () => (await text()).includes('已选 1 题'), 'filtered selection')
  await click('生成打印预览')
  await eventually(() => evaluate(`Boolean(document.querySelector('iframe[title="错题本打印预览"]'))`), 'preview iframe')
  const previewHtml = await evaluate(`document.querySelector('iframe[title="错题本打印预览"]').srcdoc`)
  assert(previewHtml.includes('计算3+4'))
  assert(previewHtml.includes('运算错误'))
  assert(previewHtml.includes('待复测'))
  assert(!previewHtml.includes('apple'))
  assert(!previewHtml.includes('其他孩子专属'))
  report.checks.filter_and_preview = true
  await click('保存 PDF')
  await eventually(async () => (await text()).includes('已保存'), 'native PDF saved', 40000)
  assert((await stat(join(output, 'notebook.pdf'))).size > 2000)
  report.real_pdf_engine = true
  report.checks.native_pdf_saved = true
  await writeFile(join(output, 'cancel-chooser'), 'cancel next save')
  const previousPdf = await readFile(join(output, 'notebook.pdf'))
  await click('保存 PDF')
  await eventually(async () => (await text()).includes('取消'), 'save cancellation')
  assert.deepEqual(await readFile(join(output, 'notebook.pdf')), previousPdf)
  report.checks.cancel_preserves_pdf = true
  await evaluate(`document.querySelector('iframe[title="错题本打印预览"]').scrollIntoView({block:'start'})`)
  const screenshot = await cdp('Page.captureScreenshot', { format: 'png' })
  await writeFile(join(output, 'preview.png'), Buffer.from(screenshot.data, 'base64'))
  await copyFile(join(output, 'notebook.pdf'), join(output, 'filtered-notebook.pdf'))
  await click('重置全部筛选')
  assert.equal(await evaluate(`Boolean(document.querySelector('iframe[title="错题本打印预览"]'))`), false)
  report.checks.edit_invalidates_preview = true
  const after = await api()
  assert.deepEqual(after.items, before.items)
  report.checks.print_is_read_only = true
  backend.kill('SIGTERM'); await exited(backend, 'backend'); backend = null
  await startBackend()
  const resumed = await api()
  assert.deepEqual(resumed.items, before.items)
  await cdp('Page.reload', { ignoreCache: true })
  await eventually(async () => (await text()).includes('计算3+4'), 'renderer reload')
  report.checks.backend_restart_and_renderer_reload = true
  report.status = 'passed'
  }
} catch (error) {
  report.status = 'failed'; report.error = error instanceof Error ? error.message : String(error)
  process.exitCode = 1
} finally {
  ws?.close()
  for (const waiter of pending.values()) waiter.reject(Error('acceptance closed'))
  pending.clear()
  if (electron?.exitCode === null) { await writeFile(join(output, 'stop-electron'), 'stop'); await exited(electron, 'Electron').catch(error => { report.cleanup_error = error.message;process.exitCode = 1 }) }
  if (backend?.exitCode === null) { backend.kill('SIGTERM');await exited(backend, 'backend').catch(error => { report.cleanup_error = error.message;process.exitCode = 1 }) }
  for (const log of logs) await log.close()
  await checkpoint()
  console.log(JSON.stringify({ status: report.status, checks: report.checks, error: report.error, cleanup_error: report.cleanup_error, output }))
}
