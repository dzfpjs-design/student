import { mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { startElectron, stopOwned, unlockParent, screenshot, root, outputDir, pythonBin, run, owned } from './electron-parent-report-delivery-e2e.mjs'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'

// This supplementary run does not repeat the previous read-aloud/picture cases.
const cases = [
  { family: 'topic_response', prompt: 'Talk about an outdoor place you would like to visit. Say where it is, what you would do there and why.',
    points: ['Name the place and its location.', 'Describe what you would do there.', 'Explain why you chose this place.'] },
  { family: 'interaction', prompt: 'Plan a class visit with me. Suggest a destination and transport, respond to my suggestion and ask me a question.',
    material: 'Partner: I would like to visit the science museum, but the bus ride takes an hour. What do you think?',
    points: ['Suggest a destination and transport.', 'Respond to the partner suggestion.', 'Ask a relevant question.'] },
  { family: 'extended_turn', prompt: 'Talk for about half a minute about a hobby you would like to try. Explain a reason and a practical first step.',
    points: ['Name a hobby you would like to try.', 'Explain why it interests you.', 'Describe a practical first step.'] },
  { family: 'daily_dialogue', prompt: 'I ask: Shall we visit the school library tomorrow? Reply, suggest a time, explain a reason and ask a question.',
    material: 'The library opens after lunch. Your friend has football practice at four.',
    points: ['Respond to the invitation.', 'Suggest a suitable time.', 'Explain your choice and ask a follow-up question.'] },
  { family: 'retell', prompt: 'Read the whole story and retell it in your own words. Explain who was involved, the problem and how they solved it.',
    material: 'Mira and Ben planted seeds beside the classroom. One morning the soil was very dry because their watering can had a hole. They asked the caretaker for a spare can and watered the seedlings. A week later, tiny green leaves appeared.',
    points: ['Name the people and the setting.', 'Explain the dry soil and damaged watering can.', 'Retell the solution and the ending in order.'] }
]
const dataDir = join(outputDir, 'data'), databasePath = join(dataDir, 'oral.db'), profile = join(outputDir, 'profile')
await mkdir(dataDir, { recursive: true })
const env = { ...parentReportDeliveryEnvironment({ root, pythonBin, dataDir, databasePath }), PYTHONDONTWRITEBYTECODE: '1',
  PYTHONPATH: join(root, 'services/local_agent/src') + ':' + join(root, 'services/local_agent/tests') }
await run(pythonBin, [join(root, 'scripts/seed-oral-ui.py'), databasePath], { cwd: root, env })
const suffix = randomUUID().slice(0, 8)
let app = null, phase = 'launch'
const checks = []
const sleep = ms => new Promise(r => setTimeout(r, ms))
const evaluate = code => app.cdp.evaluate(code)
async function until(fn, label) {
  const end = Date.now() + 35000
  while (Date.now() < end) { if (await fn()) return; await sleep(120) }
  throw Error(label + ' timed out')
}
async function text(s) { await until(() => evaluate(`document.querySelector('.curriculum-supply-review')?.innerText.includes(${JSON.stringify(s)})`), s) }
async function click(name) {
  const q = `Array.from(document.querySelectorAll('.curriculum-supply-review button')).find(b=>b.textContent.trim()===${JSON.stringify(name)}&&!b.disabled)`
  await until(() => evaluate(`!!(${q})`), 'button ' + name); await evaluate(`(${q}).click()`)
}
const fieldQuery = label => `Array.from(document.querySelectorAll('.curriculum-supply-review label')).find(l=>{const a=l.cloneNode(true);a.querySelectorAll('input,select,textarea').forEach(n=>n.remove());return a.textContent.trim()===${JSON.stringify(label)}})?.querySelector('input,select,textarea')`
async function field(label, value) {
  await evaluate(`(()=>{const n=${fieldQuery(label)};if(!n||n.disabled)throw Error('field unavailable '+${JSON.stringify(label)});
    const p=n instanceof HTMLSelectElement?HTMLSelectElement.prototype:n instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(p,'value').set.call(n,${JSON.stringify(value)});n.dispatchEvent(new Event('input',{bubbles:true}));n.dispatchEvent(new Event('change',{bubbles:true}));})()`)
}
async function chooseFirst(label) {
  let value
  await until(async () => { value = await evaluate(`(()=>{const n=${fieldQuery(label)};return n&&!n.disabled&&Array.from(n.options).find(o=>o.value&&!o.disabled)?.value})()`); return value }, label)
  await field(label, value)
}
async function enter(first) {
  await unlockParent(app.cdp, first)
  await evaluate(`Array.from(document.querySelectorAll('.parent-tabs button')).find(b=>b.textContent.includes('四年级'))?.click()`)
  await text('课程供给')
  await until(() => evaluate(`document.querySelector('.curriculum-supply-review')?.getAttribute('aria-busy')==='false'`), 'read curriculum')
}
try {
  app = await startElectron(profile, dataDir, databasePath); await enter(true)
  for (const [index, entry] of cases.entries()) {
    phase = entry.family + ':author'
    await click('编写口语原题')
    if (index) await click('编写另一道题')
    await until(() => evaluate(`!!Array.from(document.querySelectorAll('.curriculum-supply-review select')).find(n=>Array.from(n.options).some(o=>o.value===${JSON.stringify(entry.family)})&&!n.disabled)`), 'current descriptor')
    await field('口语任务', entry.family); await field('资料名称', `Original ${entry.family} ${suffix}`); await field('资料版本', `Synthetic UI ${suffix}`)
    await field('完整任务要求', entry.prompt)
    if (entry.material) await field(entry.family === 'retell' ? '完整阅读材料（必填）' : '补充材料（可选）', entry.material)
    for (const [i, point] of entry.points.entries()) {
      if (i) await evaluate(`document.querySelector('[aria-label="口语原题编写"] button[aria-label="增加任务点"]').click()`)
      await field(`任务点 ${i + 1}`, point)
    }
    await evaluate(`document.querySelector('[aria-label="口语原题编写"] input[type=checkbox]').click()`)
    await click('保存口语原题'); await text('完整原题已保存并读回'); await click('编写口语原题')
    await chooseFirst('对照课标'); await click('保存当前教材')
    await until(() => evaluate(`document.querySelector('.curriculum-supply-review')?.getAttribute('aria-busy')==='false'`), 'selection saved')
    await chooseFirst('教材题目'); await chooseFirst('题型依据'); await field('题目难度（1至5）', '1')
    await field('解题说明', '家长核对完整原题、角色材料及所有任务点；仅供内容参考，不评价发音或掌握。')
    phase = entry.family + ':review'
    await click('提取审核草案'); await click('独立校验'); await text('待审核')
    await field('审核备注', '已核对完整新原题、完整材料及全部任务点，作为家庭本机口语参考。')
    await click('审核通过'); await click('发布到题库'); await text('已计入当前有效题库。')
    const evidence = await evaluate(`(()=>{const s=document.querySelector('.curriculum-supply-review');const detail=Array.from(s.querySelectorAll('[data-work-object]')).find(n=>n.querySelector('h3')?.textContent.includes('题目与依据'));
      return {text:detail?.innerText,width:s.clientWidth,scroll:s.scrollWidth,images:detail?.querySelectorAll('img').length}})()`)
    if (!evidence.text?.includes(entry.prompt) || entry.points.some(p => !evidence.text.includes(p))
      || entry.material && !evidence.text.includes(entry.material) || evidence.images !== 0 || evidence.text.includes('knowledge_node_id')
      || evidence.scroll > evidence.width + 1) throw Error('incomplete/incorrect original presentation')
    await evaluate(`Array.from(document.querySelectorAll('.curriculum-supply-review h3')).find(n=>n.textContent.includes('题目与依据')).scrollIntoView({block:'start'})`)
    await screenshot(app.cdp, join(outputDir, `${index + 1}-${entry.family}.png`))
    checks.push({ family: entry.family, full_prompt: true, full_material: !!entry.material, points: entry.points.length,
      no_inherited_picture: true, no_json_ui: true, published_via_actual_ui: true, width: evidence.width, scroll: evidence.scroll })
    await writeFile(join(outputDir, 'progress.json'), JSON.stringify({ phase, checks }, null, 2))
  }
  phase = 'restart-current-readback'
  await stopOwned(app); app = null; app = await startElectron(profile, dataDir, databasePath); await enter(false)
  await text('已计入当前有效题库。')
  const content = await evaluate(`document.querySelector('.curriculum-supply-review').innerText`)
  if (!content.includes(cases.at(-1).material)) throw Error('restart lost the original retell material')
  for (const entry of cases) {
    await evaluate(`Array.from(document.querySelectorAll('.csr-queue button')).find(b=>b.textContent.includes(${JSON.stringify(entry.prompt)})).click()`)
    await until(() => evaluate(`Array.from(document.querySelectorAll('.curriculum-supply-review [data-work-object]')).some(n=>n.querySelector('h3')?.textContent.includes('题目与依据')&&n.innerText.includes(${JSON.stringify(entry.prompt)}))`), 'read original after restart')
  }
  await stopOwned(app); app = null
  phase = 'durable-db-originals'
  const expectedFile = join(outputDir, 'synthetic-originals.json')
  await writeFile(expectedFile, JSON.stringify(cases))
  const result = await run(pythonBin, ['-c', `import sqlite3,json; c=sqlite3.connect(${JSON.stringify(databasePath)});c.row_factory=sqlite3.Row
expected=json.load(open(${JSON.stringify(expectedFile)}))
rows=c.execute('select candidate_id,child_id,state,payload_json from curriculum_supply_package').fetchall()
assert len(rows)==5 and c.execute('select count(*) from knowledge_source').fetchone()[0]==5
for row in rows:
 p=json.loads(row['payload_json'])['pet_oral'];e=next(x for x in expected if x['family']==p['task_family'])
 assert row['child_id']=='child-primary-4' and row['state']=='published'
 assert p['prompt']==e['prompt'] and p['task_points']==e['points'] and p['stimulus_text']==e.get('material') and p['stimulus_binding'] is None
 item=c.execute('select * from diagnostic_item where diagnostic_item_id=?',('specialized-'+row['candidate_id'],)).fetchone()
 assert item['answer_type']=='speaking' and item['assessment_mode']=='pet_reference_rubric'
 assert item['prompt']==e['prompt'] and item['stimulus_text']==e.get('material') and item['stimulus_asset'] is None
 r=json.loads(item['expected_answer_json']); assert r['task_points']==e['points'] and r['reference_scope']=='oral_content_reference_only' and 'local_rule' not in r
assert c.execute('select count(*) from diagnostic_attempt').fetchone()[0]==0
assert c.execute('select count(*) from model_call').fetchone()[0]==0
assert c.execute('pragma integrity_check').fetchone()[0]=='ok'
print(json.dumps({'packages':len(rows),'sources':5,'attempts':0,'model_calls':0,'exact_published_originals':True,'reference_only':True,'integrity':'ok'}))`], { cwd: root, env })
  await writeFile(join(outputDir, 'result.json'), JSON.stringify({ status: 'passed', checks, database: JSON.parse(result.stdout),
    restart_read_all_five: true, synthetic_only: true, old_source_reimport: false, physical_audio: false, official_model: false }, null, 2))
  console.log(JSON.stringify({ status: 'passed', families: checks.length, outputDir }))
} catch (e) {
  await writeFile(join(outputDir, 'failure.json'), JSON.stringify({ phase, error: String(e), checks }, null, 2))
  if (app?.cdp) {
    await screenshot(app.cdp, join(outputDir, 'failure.png')).catch(()=>{})
    await writeFile(join(outputDir, 'failure-ui.txt'), String(await evaluate(`document.querySelector('.curriculum-supply-review')?.innerText`).catch(()=>'')))
  }
  throw e
} finally { for (const a of owned.values()) await stopOwned(a) }
