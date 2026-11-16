import { mkdir, writeFile, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { startElectron, stopOwned, unlockParent, screenshot, root, outputDir, pythonBin, run, owned, captureOwnedFailure } from './electron-parent-report-delivery-e2e.mjs'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'

const dataDir = join(outputDir, 'data'), databasePath = join(dataDir, 'oral.db'), profile = join(outputDir, 'profile')
await mkdir(dataDir, { recursive: true })
const env = { ...parentReportDeliveryEnvironment({ root, pythonBin, dataDir, databasePath }), PYTHONDONTWRITEBYTECODE: '1',
  PYTHONPATH: join(root, 'services/local_agent/src') + ':' + join(root, 'services/local_agent/tests') }
const verifyExisting = process.argv.includes('--verify-existing')
if (!verifyExisting) await run(pythonBin, [join(root, 'scripts/seed-oral-ui.py'), databasePath], { cwd: root, env })
let app, phase = 'launch'
const checks = [], material = 'Ada visits the school garden on Tuesday. She waters a dry plant with her teacher. At the end, they move it into the shade.'
const sleep = ms => new Promise(r => setTimeout(r, ms))
async function until(fn, label, timeout = 35000) {
  const end = Date.now() + timeout
  while (Date.now() < end) { if (await fn()) return; await sleep(150) }
  throw Error(label + ' timed out')
}
const evaluate = code => app.cdp.evaluate(code)
async function text(s) { await until(() => evaluate(`document.querySelector('.curriculum-supply-review')?.textContent.includes(${JSON.stringify(s)})`), s) }
async function click(name) {
  const q = `Array.from(document.querySelectorAll('.curriculum-supply-review button')).find(b=>b.textContent.trim()===${JSON.stringify(name)}&&!b.disabled)`
  await until(() => evaluate(`!!(${q})`), 'button ' + name)
  await evaluate(`(${q}).click()`)
}
async function field(label, value) {
  await evaluate(`(()=>{const n=Array.from(document.querySelectorAll('.curriculum-supply-review label')).find(l=>{const a=l.cloneNode(true);a.querySelectorAll('input,select,textarea').forEach(n=>n.remove());return a.textContent.trim()===${JSON.stringify(label)}})?.querySelector('input,select,textarea');
    if(!n||n.disabled)throw Error('field unavailable '+${JSON.stringify(label)});const p=n instanceof HTMLSelectElement?HTMLSelectElement.prototype:n instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(p,'value').set.call(n,${JSON.stringify(value)});n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));})()`)
}
async function chooseFirst(label) {
  let value
  await until(async () => { value = await evaluate(`(()=>{const n=Array.from(document.querySelectorAll('.curriculum-supply-review label')).find(l=>l.firstChild?.textContent===${JSON.stringify(label)})?.querySelector('select');return n&&!n.disabled&&Array.from(n.options).find(o=>o.value&&!o.disabled)?.value})()`); return value }, label)
  await field(label, value)
}
async function enter(first) {
  await unlockParent(app.cdp, first)
  await evaluate(`Array.from(document.querySelectorAll('.parent-tabs button')).find(b=>b.textContent.includes('四年级'))?.click()`)
  await text('课程供给')
  await until(() => evaluate(`document.querySelector('.curriculum-supply-review')?.getAttribute('aria-busy')==='false'`), 'read curriculum')
}
async function author(family) {
  await click('编写口语原题')
  await until(() => evaluate(`!!Array.from(document.querySelectorAll('.curriculum-supply-review select')).find(n=>Array.from(n.options).some(o=>o.value==='read_aloud')&&!n.disabled)`), 'oral descriptors')
  await field('口语任务', family)
  await field('资料名称', family === 'read_aloud' ? 'Garden original passage' : 'Study-room original picture task')
  await field('资料版本', 'Synthetic oral UI v1')
  await field('完整任务要求', family === 'read_aloud' ? 'Read the complete garden story in your own natural voice.' : 'Look at this picture. Describe the two children and explain one difference between their activities.')
  if (family === 'read_aloud') await field('完整阅读材料（必填）', material)
  await field('任务点 1', family === 'read_aloud' ? 'Read the beginning, middle and ending.' : 'Describe both children and their activities.')
  await evaluate(`document.querySelector('[aria-label="口语原题编写"] input[type=checkbox]').click()`)
}
async function publish() {
  await chooseFirst('对照课标'); await click('保存当前教材')
  await until(() => evaluate(`document.querySelector('.curriculum-supply-review')?.getAttribute('aria-busy')==='false'`), 'selection saved')
  await chooseFirst('教材题目'); await chooseFirst('题型依据')
  await field('题目难度（1至5）', '1')
  await field('解题说明', '核对完整原题、原材料和参考任务点；不认证口语、发音或掌握。')
  await click('提取审核草案'); await click('独立校验')
  await text('待审核'); await field('审核备注', '已核对这份合成原题的完整内容、材料与任务点，仅作本地参考。')
  await click('审核通过'); await click('发布到题库'); await text('已计入当前有效题库。')
}
try {
  let bounds = null, narrow = null
  if (!verifyExisting) {
  app = await startElectron(profile, dataDir, databasePath); await enter(true)
  phase = 'read-aloud-author-and-loss'; await author('read_aloud')
  await evaluate(`(()=>{const original=window.fetch.bind(window);window.fetch=async(...args)=>{const r=await original(...args);if(String(args[0]).includes('/knowledge-sources')&&args[1]?.method==='POST'&&!window.__oralLost){window.__oralLost=true;await r.clone().text();throw Error('synthetic lost import response')}return r};})()`)
  await click('保存口语原题'); await text('原保存请求待核对')
  await until(() => evaluate(`!!document.querySelector('.curriculum-supply-review [role=alert]')`), 'actual response loss')
  const journal = await evaluate(`localStorage.getItem('hsc-curriculum-oral-author:child-primary-4')`)
  if (!JSON.parse(journal).pending) throw Error('pending original request missing')
  await stopOwned(app); app = null
  phase = 'restart-replay-and-publish'; app = await startElectron(profile, dataDir, databasePath); await enter(false)
  await click('编写口语原题'); await text('原保存请求待核对'); await click('重试原保存请求'); await text('完整原题已保存并读回')
  await click('编写口语原题'); await publish()
  await evaluate(`Array.from(document.querySelectorAll('.curriculum-supply-review h3')).find(n=>n.textContent.includes('题目与依据')).scrollIntoView({block:'start'})`)
  await screenshot(app.cdp, join(outputDir, '01-read-aloud-published.png'))
  if (!await evaluate(`document.querySelector('.curriculum-supply-review').textContent.includes(${JSON.stringify(material)})`)) throw Error('full material missing')
  checks.push('real UI author -> actual HTTP committed import -> lost response -> backend/app restart -> same original import -> full material validate/review/publish')
  phase = 'picture'; await click('编写口语原题'); await click('编写另一道题'); await click('编写口语原题'); await author('picture_description')
  await click('保存口语原题'); await text('完整原题已保存并读回'); await click('编写口语原题'); await publish()
  await evaluate(`document.querySelector('.curriculum-supply-review img').scrollIntoView({block:'center'})`)
  await until(() => evaluate(`Array.from(document.querySelectorAll('.curriculum-supply-review img')).every(i=>i.complete&&i.naturalWidth>100)`), 'real exact image')
  bounds = await evaluate(`(()=>{const s=document.querySelector('.curriculum-supply-review');return {width:s.clientWidth,scroll:s.scrollWidth,images:Array.from(s.querySelectorAll('img')).map(i=>({width:i.naturalWidth,height:i.naturalHeight}))}})()`)
  if (bounds.scroll > bounds.width + 1) throw Error('desktop horizontal overflow')
  await screenshot(app.cdp, join(outputDir, '02-picture-published.png'))
  await app.cdp.send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false })
  await evaluate(`document.querySelector('.curriculum-supply-review img').scrollIntoView({block:'center'})`)
  await screenshot(app.cdp, join(outputDir, '03-picture-narrow.png'))
  narrow = await evaluate(`(()=>{const s=document.querySelector('.curriculum-supply-review');return {width:s.clientWidth,scroll:s.scrollWidth}})()`)
  if (narrow.scroll > narrow.width + 1) throw Error('narrow curriculum overflow')
  checks.push('actual exact shipped picture rendered; complete new question and taskpoints reviewed/published; desktop/narrow panel bounds checked')
  await stopOwned(app); app = null
  phase = 'published-restart-withdraw'; app = await startElectron(profile, dataDir, databasePath); await enter(false); await text('已计入当前有效题库。')
  await field('审核备注', '撤回这份合成图片原题，不删除既有原回答。'); await click('撤回题目'); await text('已撤回')
  await stopOwned(app); app = null
  } else {
    const failure = JSON.parse(await readFile(join(outputDir, 'failure.json'), 'utf8'))
    if (failure.phase !== 'published-restart-withdraw' || !failure.error.includes('no such column: diagnostic_item_id')) throw Error('only the known post-UI verification error is resumable')
    for (const name of ['01-read-aloud-published.png', '02-picture-published.png', '03-picture-narrow.png']) await readFile(join(outputDir, name))
    checks.push('resumed only the final read-only DB check after the original UI run completed; original failed query/log/screenshots retained, no user operation rerun')
  }
  const data = await run(pythonBin, ['-c', `import sqlite3,json; c=sqlite3.connect(${JSON.stringify(databasePath)}); c.row_factory=sqlite3.Row
sources=[dict(r) for r in c.execute("select source_id,title,child_id from knowledge_source")]
packages=[dict(r) for r in c.execute("select state,payload_json from curriculum_supply_package")]
assert len(sources)==2 and len(packages)==2
assert all(r['child_id']=='child-primary-4' for r in sources)
assert sorted(p['state'] for p in packages)==['published','withdrawn']
assert c.execute('select count(*) from diagnostic_attempt').fetchone()[0]==0
assert c.execute('select count(*) from model_call').fetchone()[0]==0
assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
print(json.dumps({'sources':len(sources),'packages':len(packages),'states':sorted(p['state'] for p in packages),'attempts':0,'model_calls':0,'integrity':'ok'}))`], { cwd: root, env })
  checks.push('same database reopened; published records retained; explicit UI withdrawal; no attempt/model calls; no duplicate source after lost response')
  await writeFile(join(outputDir, 'result.json'), JSON.stringify({ status: 'passed', checks, desktop: bounds, narrow, database: JSON.parse(data.stdout),
    synthetic_only: true, actual_electron: true, resumed_read_only_verification: verifyExisting,
    hardware_audio: 'not_tested', official_model: 'not_called' }, null, 2))
  console.log(JSON.stringify({ status: 'passed', checks: checks.length, outputDir }))
} catch (error) {
  if (app) { await screenshot(app.cdp, join(outputDir, 'failure.png')).catch(()=>{}); await writeFile(join(outputDir,'failure-ui.txt'), String(await evaluate(`document.querySelector('.curriculum-supply-review')?.textContent`).catch(()=>''))) }
  await writeFile(join(outputDir, 'failure.json'), JSON.stringify({ phase, error: String(error) }))
  throw error
} finally {
  for (const a of owned.values()) await stopOwned(a)
}
