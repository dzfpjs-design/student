import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { startElectron, stopOwned, screenshot, root, outputDir, pythonBin, owned, run } from './electron-parent-report-delivery-e2e.mjs'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'

await mkdir(outputDir,{recursive:true})
const fixture=await mkdtemp('/private/tmp/hsc-goal-check-native-')
const data=join(fixture,'data'), profile=join(fixture,'profile'), db=join(data,'goal.db')
await mkdir(data); await mkdir(join(profile,'voice-onboarding'),{recursive:true})
const children=['child-primary-4','child-junior-3'], updatedAt=new Date().toISOString()
await writeFile(join(profile,'companion-wake-words.json'),JSON.stringify(children.map((childId,i)=>({
  childId,wakeWord:i?'小伴':'小知',inactivitySeconds:60,enabled:false,updatedAt}))))
for(const childId of children) await writeFile(join(profile,'voice-onboarding',childId+'.json'),JSON.stringify({childId,version:1,step:'deferred',updatedAt}))
const seeded=await run(pythonBin,[join(root,'scripts/seed-goal-check-native.py'),db,join(outputDir,'seed.json')],{
  cwd:root,env:parentReportDeliveryEnvironment({root,pythonBin,dataDir:data,databasePath:db})})
await writeFile(join(outputDir,'seed.log'),seeded.stdout+seeded.stderr)
const seed=JSON.parse(await readFile(join(outputDir,'seed.json'),'utf8'))
const asset=(await readFile(join(root,'out/renderer/index.html'),'utf8')).match(/\.\/assets\/index-[^" ]+\.js/)?.[0]
if(!asset)throw Error('Built renderer missing')
const inputs=['services/local_agent/src/home_study_agent/repository.py','services/local_agent/src/home_study_agent/diagnostic_prompt.py',
  'services/local_agent/src/home_study_agent/agent_capability_executor.py','src/renderer/src/diagnosticVoiceFlow.ts',
  'src/renderer/src/SessionWorkspace.tsx','src/renderer/src/LocalInputs.tsx','out/renderer/'+asset.slice(2),'out/main/index.js']
async function hashes(){const result={};for(const path of inputs)result[path]=createHash('sha256').update(await readFile(join(root,path))).digest('hex');return result}
const before=await hashes(), sleep=ms=>new Promise(r=>setTimeout(r,ms))
const restartBeforeAnswer=process.argv.includes('--restart-before-answer')
let app, phase='launch'
function rows(sql){const r=spawnSync('/usr/bin/sqlite3',['-readonly','-cmd','.timeout 5000','-json',db,sql],{encoding:'utf8'});if(r.status!==0)throw Error('Synthetic DB read failed');return JSON.parse(r.stdout||'[]')}
async function until(fn,label){const deadline=Date.now()+45000;while(Date.now()<deadline){if(await fn())return;await sleep(150)}throw Error(label)}
async function start(){
  app=await startElectron(profile,data,db,{fakeMedia:true})
  await app.cdp.evaluate(`Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>{throw new DOMException('Synthetic media disabled','NotAllowedError')}})`)
  await until(()=>app.cdp.evaluate(`Boolean(document.querySelector('input[aria-label="对学习伙伴说"]'))||(()=>{[...document.querySelectorAll('button')].find(b=>b.textContent.includes('继续学习'))?.click();return false})()`),'Child workspace unavailable')
}
async function enter(text){
  await until(()=>app.cdp.evaluate(`document.querySelector('.agent-command-input button')?.textContent==='发送'`),'Previous renderer command unfinished')
  await app.cdp.evaluate(`(()=>{const i=document.querySelector('input[aria-label="对学习伙伴说"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,${JSON.stringify(text)});i.dispatchEvent(new Event('input',{bubbles:true}))})()`)
  await sleep(100)
  await app.cdp.evaluate(`document.querySelector('input[aria-label="对学习伙伴说"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))`)
}
function turnsDone(count){const turns=rows('SELECT tool_status,tool_error_code FROM agent_turn ORDER BY rowid');return turns.length===count&&turns.every(t=>t.tool_status==='completed'&&!t.tool_error_code)}
try{
  await start();phase='start-goal';await enter('开始任务')
  await until(()=>rows("SELECT prompt_id FROM diagnostic_prompt WHERE status='waiting_answer'").length===1,'Planned question not armed')
  await until(()=>turnsDone(1),'Start turn unfinished')
  const prompt=rows('SELECT * FROM diagnostic_prompt')[0]
  const currentItem=rows('SELECT knowledge_node_id,prompt FROM diagnostic_item WHERE diagnostic_item_id='+JSON.stringify(prompt.diagnostic_item_id))[0]
  if(currentItem?.knowledge_node_id!==seed.knowledge_node_id||prompt.child_id!==seed.child_id)throw Error('Wrong planned knowledge target')
  await screenshot(app.cdp,join(outputDir,'goal-question.png'))
  if(restartBeforeAnswer){
    await stopOwned(app);app=null;phase='resume-original';await start()
    if(rows("SELECT status FROM plan_item WHERE source_type='junior_exam_goal'")[0]?.status!=='paused')throw Error('Shutdown did not pause task')
    await enter('继续原任务')
    await until(()=>turnsDone(2),'Resume turn unfinished')
    if(rows("SELECT status FROM plan_item WHERE source_type='junior_exam_goal'")[0]?.status!=='active')throw Error('Original task not resumed')
    const restored=rows('SELECT prompt_id,item_version,diagnostic_item_id FROM diagnostic_prompt')
    if(restored.length!==1||['prompt_id','item_version','diagnostic_item_id'].some(key=>restored[0][key]!==prompt[key]))throw Error('Resume changed original question')
    await screenshot(app.cdp,join(outputDir,'goal-resumed.png'))
  }
  phase='answer';await enter(currentItem.prompt.includes('选项字母')?'答案是A':'答案是负九千九百九十九')
  await until(()=>rows('SELECT attempt_id FROM diagnostic_attempt').length===1,'Answer not persisted')
  await until(()=>turnsDone(restartBeforeAnswer?3:2),'Answer turn unfinished')
  const attempts=rows('SELECT attempt_id,evidence_id,submitted_answer,correctness FROM diagnostic_attempt')
  if(attempts[0].correctness!==0)throw Error('Wrong answer not retained as incorrect')
  await until(()=>app.cdp.evaluate(`document.body.innerText.includes('本次回答已保存')`),'Answer feedback absent')
  await screenshot(app.cdp,join(outputDir,'goal-answer.png'))
  phase='finish-check';await enter('这项做完了')
  await until(()=>turnsDone(restartBeforeAnswer?4:3),'Completion turn unfinished')
  if(rows("SELECT status FROM plan_item WHERE source_type='junior_exam_goal'")[0]?.status!=='completed')throw Error('Goal not completed')
  await until(()=>app.cdp.evaluate(`document.body.innerText.includes('这项已经保存，今天安排的任务都处理完了')`),'Completion feedback absent')
  await screenshot(app.cdp,join(outputDir,'goal-finished.png'))
  const saved=rows('SELECT agent_turn_id,tool_status,tool_error_code FROM agent_turn ORDER BY rowid')
  await stopOwned(app);app=null;phase='final-restart';await start()
  if(JSON.stringify(attempts)!==JSON.stringify(rows('SELECT attempt_id,evidence_id,submitted_answer,correctness FROM diagnostic_attempt'))
    ||JSON.stringify(saved)!==JSON.stringify(rows('SELECT agent_turn_id,tool_status,tool_error_code FROM agent_turn ORDER BY rowid'))
    ||rows("SELECT COUNT(*) AS n FROM plan_item_event WHERE event_type='completed'")[0].n!==1
    ||!rows('SELECT review_task_id FROM review_task').length)throw Error('Restart lost or duplicated learning state')
  await stopOwned(app);app=null
  const after=await hashes();if(JSON.stringify(before)!==JSON.stringify(after))throw Error('Runtime inputs changed')
  await writeFile(join(outputDir,'result.json'),JSON.stringify({passed:true,restartBeforeAnswer,fixture,seed,attempts,before,after,owned_processes:owned.size,
    boundary:'Actual Electron, renderer handler, local backend and SQLite. Synthetic qualified question, developer text input, no real ASR, TTS, camera, paid model, Windows or child outcome claim.'},null,2))
}catch(error){
  if(app?.cdp)await screenshot(app.cdp,join(outputDir,'failure.png')).catch(()=>{})
  await writeFile(join(outputDir,'failure.txt'),phase+'\n'+String(error.stack));process.exitCode=1
}finally{if(app)await stopOwned(app);for(const value of [...owned.values()])await stopOwned(value)}
