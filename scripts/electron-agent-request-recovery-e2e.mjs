import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { startElectron, stopOwned, screenshot, root, outputDir, owned } from './electron-parent-report-delivery-e2e.mjs'

await mkdir(outputDir, { recursive: true })
const fixture = await mkdtemp('/private/tmp/hsc-request-recovery-native-')
const mode = process.env.HSC_RECEIPT_TEST_MODE || 'delivery'
const activeTask = process.env.HSC_RECEIPT_TEST_ACTIVE_TASK === '1'
if (!['delivery', 'before-execution', 'after-execution'].includes(mode)) throw Error('Invalid receipt test mode')
const data = join(fixture, 'data'), profile = join(fixture, 'profile'), db = join(data, 'home-study.db')
await mkdir(data); await mkdir(profile)
const html = await readFile(join(root, 'out/renderer/index.html'), 'utf8')
const asset = html.match(/\.\/assets\/index-[^" ]+\.js/)?.[0]
if (!asset) throw Error('Built renderer asset missing')
const files = ['services/local_agent/src/home_study_agent/app.py', 'services/local_agent/src/home_study_agent/agent_request_receipt.py',
  'src/renderer/src/api.ts', 'src/renderer/src/SessionWorkspace.tsx', 'src/renderer/src/agentRequestJournal.ts',
  'src/renderer/src/agentRequestRecovery.ts', 'src/renderer/src/agentExecutionJournal.ts',
  'out/main/index.js', 'out/renderer/index.html', 'out/renderer/' + asset.slice(2)]
const hashes = async () => Object.fromEntries(await Promise.all(files.map(async p => [p, createHash('sha256').update(await readFile(join(root, p))).digest('hex')])))
const before = await hashes()
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
async function eventually(fn, label) {
  const end = Date.now() + 45000
  while (Date.now() < end) { if (await fn()) return; await sleep(150) }
  throw Error(label)
}
const noMedia = `Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>{throw new DOMException('Synthetic test; physical media disabled','NotAllowedError')}});`
const savedTurns = () => {
  const result = spawnSync('/usr/bin/sqlite3', ['-readonly', '-cmd', '.timeout 5000', db,
    "SELECT json_object('id',agent_turn_id,'key',client_action_id,'status',tool_status) FROM agent_turn ORDER BY agent_turn_id"], { encoding: 'utf8' })
  if (result.status !== 0) throw Error('Cannot read synthetic fixture: ' + result.stderr.slice(0, 500))
  return result.stdout.trim().split('\n').filter(Boolean).map(JSON.parse)
}
const savedTasks = () => {
  const result = spawnSync('/usr/bin/sqlite3', ['-readonly', '-cmd', '.timeout 5000', db,
    "SELECT json_object('id',p.plan_item_id,'status',p.status,'source_type',p.source_type,'events',json((SELECT json_group_array(json_object('type',event_type,'key',client_action_id)) FROM plan_item_event WHERE plan_item_id=p.plan_item_id))) FROM plan_item p ORDER BY p.plan_item_id"], { encoding: 'utf8' })
  if (result.status !== 0) throw Error('Cannot read synthetic task evidence: ' + result.stderr.slice(0, 500))
  return result.stdout.trim().split('\n').filter(Boolean).map(JSON.parse)
}
let app
const enter = async text => {
  await app.cdp.evaluate(`(()=>{const i=document.querySelector('input[aria-label="对学习伙伴说"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,${JSON.stringify(text)});i.dispatchEvent(new Event('input',{bubbles:true}))})()`)
  await sleep(100)
  await app.cdp.evaluate(`document.querySelector('input[aria-label="对学习伙伴说"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))`)
}
const childSurface = async () => eventually(() => app.cdp.evaluate(`Boolean(document.querySelector('input[aria-label="对学习伙伴说"]'))||(()=>{[...document.querySelectorAll('button')].find(b=>b.textContent.includes('继续学习'))?.click();return false})()`), 'Child surface not ready')
try {
  app = await startElectron(profile, data, db)
  await app.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: noMedia })
  const session = await app.cdp.evaluate(`(async()=>{
    const r=await window.desktop.getRuntimeConfig(),headers={'Content-Type':'application/json','X-HSC-Session':r.sessionToken};
    const post=async(p,b)=>{const x=await fetch(r.localApiUrl+p,{method:'POST',headers,body:JSON.stringify(b)});if(!x.ok)throw Error(p+':'+x.status);return x.json()};
    const s=await post('/v1/sessions',{child_id:'child-primary-4',client_action_id:'receipt-native-session'}),id=s.session_id;
    const t=async(a,b,n)=>post('/v1/sessions/'+id+'/transition',{expected_state:a,target_state:b,reason:'synthetic test',client_action_id:'receipt-native-'+n});
    await t('check_in','classroom_recall','t1');
    await post('/v1/sessions/'+id+'/recalls/none',{client_action_id:'receipt-native-none'});
    await t('classroom_recall','homework_capture','t2');
    await post('/v1/sessions/'+id+'/homework',{subject:'数学',title:'合成验收练习',expected_minutes:20,client_action_id:'receipt-native-hw'});
    const plan=await post('/v1/sessions/'+id+'/plan/generate',{client_action_id:'receipt-native-plan'});
    await t('plan_review','study','t3');
    if(${activeTask}){
      const task=plan.plan_items.find(x=>x.source_type==='school_homework');if(!task)throw Error('No fixture school task');
      await post('/v1/plan-items/'+task.plan_item_id+'/start',{client_action_id:'receipt-native-start'});
    }
    await window.desktop.saveWakeWordSettings({childId:'child-primary-4',wakeWord:'小知',inactivitySeconds:60,enabled:false,updatedAt:new Date().toISOString()});
    await window.desktop.saveVoiceOnboarding({childId:'child-primary-4',version:1,step:'deferred',updatedAt:new Date().toISOString()});return id;
  })()`)
  await app.cdp.evaluate('window.location.reload()')
  await childSurface()
  await app.cdp.evaluate(`(()=>{const original=window.fetch.bind(window);window.__delivery=[];window.fetch=async(input,init)=>{
    if(/\\/agent-turn$/.test(String(input))&&init?.method==='POST'){
      const response=await original(input,init),turn=response.ok?await response.clone().json():{};
      window.__delivery.push({key:JSON.parse(init.body).client_action_id,status:response.status,capability:turn.runtime_capability,actionType:turn.action?.type,turnId:turn.agent_turn_id});
      if(${JSON.stringify(mode)}==='delivery'&&window.__delivery.length===1&&response.ok)throw new TypeError('Synthetic lost reply after real backend commit');return response;
    }
    if(window.__delivery.length&&init?.method&&['POST','PUT'].includes(init.method)){
      const url=String(input),mode=${JSON.stringify(mode)};
      const turnId=window.__delivery.at(-1)?.turnId;
      const ownExecution=turnId&&new URL(url).pathname==='/v1/agent-turns/'+turnId+'/execute';
      const ownOutcome=turnId&&new URL(url).pathname==='/v1/agent-turns/'+turnId+'/outcome';
      if(mode==='before-execution'&&ownExecution){
        window.__executionBoundary={url,sent:false};return new Promise(()=>{});
      }
      if(mode==='after-execution'&&(ownExecution||ownOutcome)){
        const response=await original(input,init);if(!response.ok)throw Error('Execution did not commit');
        window.__executionBoundary={url,sent:true,status:response.status};return new Promise(()=>{});
      }
    }
    return original(input,init);
  }})()`)
  // Exercise the real existing input handler, not ASR or a claimed human voice.
  await enter('暂停学习')
  if (mode === 'delivery') await eventually(() => app.cdp.evaluate(`document.body.innerText.includes('没有收到完整回执')`), 'Lost reply guidance missing')
  else await eventually(() => app.cdp.evaluate('Boolean(window.__executionBoundary)'), 'Execution interruption boundary missing')
  const turns = savedTurns()
  const proof = await app.cdp.evaluate('window.__delivery')
  if (activeTask && proof[0]?.capability !== 'task.pause') throw Error('Semantic fixture did not dispatch a real task pause')
  if (turns.length !== 1 || proof.length !== 1 || turns[0].key !== proof[0].key || proof[0].status !== 200) throw Error('Original commit missing or repeated')
  const executionBoundary = await app.cdp.evaluate('window.__executionBoundary||null')
  const tasksBeforeStop = activeTask ? savedTasks() : null
  if (activeTask) {
    const task = tasksBeforeStop.find(x => x.source_type === 'school_homework')
    const expected = mode === 'after-execution' ? 'paused' : 'active'
    if (task?.status !== expected || task.events.filter(x => x.type === 'paused').length !== Number(mode === 'after-execution')) {
      throw Error('Interrupted task did not have the expected durable operation state')
    }
  }
  const journalPrefix = mode === 'delivery' ? 'hsc-agent-request-v1:' : 'hsc-agent-execution-v1:'
  const journal = await app.cdp.evaluate(`Object.keys(localStorage).filter(k=>k.startsWith(${JSON.stringify(journalPrefix)})).map(k=>JSON.parse(localStorage.getItem(k)))`)
  if (journal.length !== 1 || journal[0].actionId !== turns[0].key || JSON.stringify(journal).includes('暂停学习')) throw Error('Delivery identity not retained privately')
  await screenshot(app.cdp, join(outputDir, 'reply-lost.png'))
  await stopOwned(app); app = null
  app = await startElectron(profile, data, db)
  await app.cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: noMedia })
  await childSurface()
  const tasksAfterRestart = activeTask ? savedTasks() : null
  if (activeTask && mode === 'after-execution' && JSON.stringify(tasksAfterRestart) !== JSON.stringify(tasksBeforeStop)) throw Error('Restart changed a completed task pause')
  const restoredText = mode === 'delivery' ? '没有收到完整回执' : '还有操作没有核对完成回执'
  await eventually(() => app.cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(restoredText)})`), 'Restart did not restore pending guidance')
  await screenshot(app.cdp, join(outputDir, 'pending-restored.png'))
  await app.cdp.evaluate(`(()=>{const original=window.fetch.bind(window);window.__recovery=[];window.fetch=async(input,init)=>{
    if(/\\/agent-turn$|\\/agent-request-receipts\\/|\\/(execute|outcome|transition)$/.test(String(input)))window.__recovery.push({url:String(input),method:init?.method||'GET'});
    return original(input,init);
  }})()`)
  await enter('核对刚才请求')
  const receiptText = mode !== 'after-execution' ? '原请求的理解结果已保存'
    : proof[0].actionType === 'none' ? '上次只完成了对话回复，没有执行学习或设备操作'
    : typeof proof[0].actionType !== 'string' || !proof[0].actionType.trim() ? '这本身不能证明学习或设备操作已完成'
    : '上次请求已记录为完成'
  await eventually(() => app.cdp.evaluate(`document.body.innerText.includes(${JSON.stringify(receiptText)})`), 'Receipt result missing')
  const recovery = await app.cdp.evaluate('window.__recovery')
  if (recovery.length !== 1 || recovery[0].method !== 'GET' || !recovery[0].url.endsWith('/agent-request-receipts/' + turns[0].key)) throw Error('Recovery repeated a mutation')
  if (JSON.stringify(savedTurns()) !== JSON.stringify(turns)) throw Error('Read-only receipt changed original turns')
  const tasksAfterCheck = activeTask ? savedTasks() : null
  if (activeTask && JSON.stringify(tasksAfterCheck) !== JSON.stringify(tasksAfterRestart)) throw Error('Receipt lookup mutated a task')
  if (await app.cdp.evaluate(`Object.keys(localStorage).some(k=>k.startsWith('hsc-agent-request-v1:')||k.startsWith('hsc-agent-execution-v1:'))`)) throw Error('Checked notice not acknowledged')
  await screenshot(app.cdp, join(outputDir, 'receipt-checked.png'))
  await stopOwned(app); app = null
  const after = await hashes()
  if (JSON.stringify(before) !== JSON.stringify(after)) throw Error('Runtime inputs changed')
  await writeFile(join(outputDir, 'result.json'), JSON.stringify({ passed: true, fixture, session, mode, activeTask, executionBoundary,
    tasksBeforeStop, tasksAfterRestart, tasksAfterCheck,
    boundary: 'Actual macOS Electron/local backend and same-profile full restart. Synthetic hidden-input handler with controlled request/response interruption. No ASR, physical media, paid model, Windows or child outcome claim. Read confirms recorded status only; no old operation replay or automatic generic execution continuation.',
    original_post: proof, recovery, turns, journal, before, after, owned_processes: owned.size }, null, 2))
  console.log('Real lost reply, journal restart and read-only original receipt verified; no replay.')
} catch (error) {
  if (app?.cdp) await screenshot(app.cdp, join(outputDir, 'failure.png')).catch(() => {})
  await writeFile(join(outputDir, 'failure.txt'), String(error.stack))
  throw error
} finally { for (const item of [...owned.values()]) await stopOwned(item) }
