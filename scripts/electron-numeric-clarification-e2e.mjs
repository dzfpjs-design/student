import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { startElectron, stopOwned, screenshot, root, outputDir, pythonBin, owned, run } from './electron-parent-report-delivery-e2e.mjs'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'

await mkdir(outputDir, { recursive: true })
const fixture = await mkdtemp('/private/tmp/hsc-numeric-clarification-native-')
const data = join(fixture,'data'), profile = join(fixture,'profile'), db = join(data,'home-study.db')
await mkdir(data); await mkdir(profile)
const seedPath = join(outputDir,'seed.json')
const seed = await run(pythonBin,[join(root,'scripts/seed-numeric-clarification-native.py'),db,seedPath], {
  cwd:root,env:parentReportDeliveryEnvironment({root,pythonBin,dataDir:data,databasePath:db}) })
await writeFile(join(outputDir,'seed.log'),seed.stdout+seed.stderr)
const prepared = JSON.parse(await readFile(seedPath,'utf8'))
const html = await readFile(join(root,'out/renderer/index.html'),'utf8')
const asset = html.match(/\.\/assets\/index-[^" ]+\.js/)?.[0]
if (!asset) throw Error('Built renderer missing')
const inputs = ['services/local_agent/src/home_study_agent/numeric_clarification.py','services/local_agent/src/home_study_agent/diagnostic_prompt.py',
  'services/local_agent/src/home_study_agent/app.py','services/local_agent/src/home_study_agent/learning_state.py',
  'src/renderer/src/diagnosticVoiceFlow.ts','src/renderer/src/api.ts','out/renderer/'+asset.slice(2),'out/main/index.js']
const hashes = async () => Object.fromEntries(await Promise.all(inputs.map(async p=>[p,createHash('sha256').update(await readFile(join(root,p))).digest('hex')])))
const before = await hashes()
const sleep = ms => new Promise(r=>setTimeout(r,ms))
async function until(read,label) {
  const deadline=Date.now()+45000
  while(Date.now()<deadline) { if(await read()) return; await sleep(150) }
  throw Error(label)
}
function attempts() {
  const r=spawnSync('/usr/bin/sqlite3',['-readonly','-cmd','.timeout 5000',db,
    "SELECT json_object('id',attempt_id,'answer',submitted_answer,'correctness',correctness) FROM diagnostic_attempt ORDER BY rowid"],{encoding:'utf8'})
  if(r.status!==0) throw Error('Synthetic DB read failed')
  return r.stdout.trim().split('\n').filter(Boolean).map(JSON.parse)
}
function turns() {
  const r=spawnSync('/usr/bin/sqlite3',['-readonly','-cmd','.timeout 5000',db,
    "SELECT json_object('id',agent_turn_id,'status',tool_status,'error',tool_error_code,'response',response_text) FROM agent_turn ORDER BY rowid"],{encoding:'utf8'})
  if(r.status!==0) throw Error('Synthetic turn read failed')
  return r.stdout.trim().split('\n').filter(Boolean).map(JSON.parse)
}
let app
const noMedia = `Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>{throw new DOMException('Synthetic no media','NotAllowedError')}})`
async function start() {
  app=await startElectron(profile,data,db)
  await app.cdp.send('Page.addScriptToEvaluateOnNewDocument',{source:noMedia})
  await app.cdp.evaluate(`(async()=>{await window.desktop.saveWakeWordSettings({childId:'child-primary-4',wakeWord:'小知',inactivitySeconds:60,enabled:false,updatedAt:new Date().toISOString()});await window.desktop.saveVoiceOnboarding({childId:'child-primary-4',version:1,step:'deferred',updatedAt:new Date().toISOString()});location.reload()})()`)
  await until(()=>app.cdp.evaluate(`Boolean(document.querySelector('input[aria-label="对学习伙伴说"]'))||(()=>{[...document.querySelectorAll('button')].find(b=>b.textContent.includes('继续学习'))?.click();return false})()`),'Child workspace unavailable')
}
async function enter(text) {
  await app.cdp.evaluate(`(()=>{const i=document.querySelector('input[aria-label="对学习伙伴说"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(i,${JSON.stringify(text)});i.dispatchEvent(new Event('input',{bubbles:true}))})()`)
  await sleep(100)
  await app.cdp.evaluate(`document.querySelector('input[aria-label="对学习伙伴说"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))`)
}
async function prompt() {
  return app.cdp.evaluate(`(async()=>{const r=await window.desktop.getRuntimeConfig(),x=await fetch(r.localApiUrl+'/v1/sessions/'+${JSON.stringify(prepared.session_id)}+'/diagnostic-prompt',{headers:{'X-HSC-Session':r.sessionToken}});if(!x.ok)throw Error('Prompt read failed');return x.json()})()`)
}
try {
  await start()
  await enter('答案是两三')
  await until(async()=>{const p=await prompt();return p.status==='waiting_answer'&&p.prompt.prompt_id!==prepared.prompt_id},'Unclear answer did not arm a clarification')
  const successor=await prompt(), first=attempts()
  if(first.length!==1||first[0].correctness!==null)throw Error('First ambiguous original not preserved')
  await until(()=>app.cdp.evaluate(`document.body.innerText.includes('只说一个数')`),'Clarification invitation missing')
  await until(async()=>turns().length===1&&turns()[0].status==='completed','Original clarification turn did not finish')
  await screenshot(app.cdp,join(outputDir,'clarification-ready.png'))
  await stopOwned(app);app=null
  await start()
  const resumed=await prompt()
  if(resumed.status!=='waiting_answer'||resumed.prompt.prompt_id!==successor.prompt.prompt_id)throw Error('Restart lost the exact clarification')
  await enter('答案是六')
  await until(async()=>attempts().length===2,'Clarified answer not saved')
  const saved=attempts()
  if(JSON.stringify(saved[0])!==JSON.stringify(first[0])||saved[1].correctness!==1)throw Error('Original overwritten or clarified score wrong')
  await until(async()=>turns().length===2&&turns()[1].status==='completed','Clarified answer turn did not finish')
  const completedTurns=turns()
  if(completedTurns.some(t=>t.error))throw Error('Clarification turn contains a tool error')
  await until(()=>app.cdp.evaluate(`document.body.innerText.includes('本次回答已保存')`),'Final feedback did not reach the child surface')
  await screenshot(app.cdp,join(outputDir,'clarified-answer-saved.png'))
  await stopOwned(app);app=null
  await start()
  const finalPrompt=await prompt()
  if(finalPrompt.status!=='answered'||finalPrompt.prompt.prompt_id!==successor.prompt.prompt_id
    ||JSON.stringify(attempts())!==JSON.stringify(saved)||JSON.stringify(turns())!==JSON.stringify(completedTurns))throw Error('Restart changed final clarification receipts')
  await stopOwned(app);app=null
  const after=await hashes()
  if(JSON.stringify(before)!==JSON.stringify(after))throw Error('Runtime inputs changed')
  await writeFile(join(outputDir,'result.json'),JSON.stringify({passed:true,fixture,prepared,successor:successor.prompt.prompt_id,
    saved,completedTurns,final_restart_verified:true,before,after,owned_processes:owned.size,boundary:'Actual main Electron and local backend; synthetic reviewed material and hidden input handler. No ASR/audio/media/provider/Windows/child-quality claim. Same-question clarification is not an independent mastery check.'},null,2))
} catch(error) {
  if(app?.cdp)await screenshot(app.cdp,join(outputDir,'failure.png')).catch(()=>{})
  await writeFile(join(outputDir,'failure.txt'),String(error.stack));process.exitCode=1
} finally { if(app)await stopOwned(app); for(const value of [...owned.values()])await stopOwned(value) }
