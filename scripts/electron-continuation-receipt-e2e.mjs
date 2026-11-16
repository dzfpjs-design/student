import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { startElectron, stopOwned, screenshot, root, outputDir, pythonBin, owned, run } from './electron-parent-report-delivery-e2e.mjs'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'

await mkdir(outputDir,{recursive:true})
const fixture=await mkdtemp('/private/tmp/hsc-continuation-receipt-native-')
const data=join(fixture,'data'),profile=join(fixture,'profile'),db=join(data,'home-study.db')
await mkdir(data);await mkdir(profile)
const seedPath=join(outputDir,'seed.json')
const seed=await run(pythonBin,[join(root,'scripts/seed-continuation-receipt-native.py'),db,seedPath],{
  cwd:root,env:parentReportDeliveryEnvironment({root,pythonBin,dataDir:data,databasePath:db})})
await writeFile(join(outputDir,'seed.log'),seed.stdout+seed.stderr)
const prepared=JSON.parse(await readFile(seedPath,'utf8'))
const html=await readFile(join(root,'out/renderer/index.html'),'utf8'),asset=html.match(/\.\/assets\/index-[^" ]+\.js/)?.[0]
if(!asset)throw Error('Built renderer missing')
const paths=['services/local_agent/src/home_study_agent/agent_continuation.py','services/local_agent/src/home_study_agent/app.py',
  'services/local_agent/src/home_study_agent/agent_runtime.py','src/renderer/src/agentWakeResumePrompt.ts',
  'src/renderer/src/realtimeVoiceRuntime.ts',
  'src/renderer/src/LocalInputs.tsx',
  'src/renderer/src/AgentObjectiveStatus.tsx','src/renderer/src/types.ts',
  'src/renderer/src/agentContinuationDriver.ts','src/renderer/src/SessionWorkspace.tsx','out/main/index.js','out/renderer/'+asset.slice(2)]
const hashes=async()=>Object.fromEntries(await Promise.all(paths.map(async p=>[p,createHash('sha256').update(await readFile(join(root,p))).digest('hex')])))
const before=await hashes()
const sleep=ms=>new Promise(r=>setTimeout(r,ms))
async function until(read,label){const deadline=Date.now()+45000;while(Date.now()<deadline){if(await read())return;await sleep(150)}throw Error(label)}
function persisted(){
  const r=spawnSync('/usr/bin/sqlite3',['-readonly','-cmd','.timeout 5000',db,
    "SELECT json_object('id',agent_turn_id,'status',tool_status,'response',response_text) FROM agent_turn ORDER BY rowid; SELECT json_object('step',step_id,'status',status,'result',result_json) FROM agent_step ORDER BY rowid"],{encoding:'utf8'})
  if(r.status!==0)throw Error('Fixture read failed')
  return r.stdout
}
const original=persisted()
const setup=`Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>{throw new DOMException('Synthetic no media','NotAllowedError')}});window.__continuationWrites=[];const fetchOriginal=window.fetch.bind(window);window.fetch=async(input,init)=>{if(init?.method&&['POST','PUT'].includes(init.method)&&/agent-turn|continuation-admission/.test(String(input)))window.__continuationWrites.push({url:String(input),method:init.method});return fetchOriginal(input,init)};`
let app
const receipts=[]
try{
  for(let round=0;round<2;round++){
    app=await startElectron(profile,data,db)
    await app.cdp.send('Page.addScriptToEvaluateOnNewDocument',{source:setup})
    await app.cdp.evaluate(`(async()=>{await window.desktop.saveWakeWordSettings({childId:'child-primary-4',wakeWord:'小知',inactivitySeconds:60,enabled:false,updatedAt:new Date().toISOString()});await window.desktop.saveVoiceOnboarding({childId:'child-primary-4',version:1,step:'deferred',updatedAt:new Date().toISOString()});location.reload()})()`)
    await until(()=>app.cdp.evaluate(`Boolean(document.querySelector('input[aria-label="对学习伙伴说"]'))||(()=>{[...document.querySelectorAll('button')].find(b=>b.textContent.includes('继续学习'))?.click();return false})()`),'Child surface missing')
    await until(()=>app.cdp.evaluate(`document.body.innerText.includes('麦克风权限被拒绝')`),'Synthetic microphone boundary missing')
    if(!await app.cdp.evaluate(`document.body.innerText.includes('麦克风暂不可用，唤醒监听未启动')&&!document.body.innerText.includes('唤醒词“小知”已启用')`))throw Error('Wake status misrepresents microphone readiness')
    const receipt=await app.cdp.evaluate(`(async()=>{const r=await window.desktop.getRuntimeConfig(),x=await fetch(r.localApiUrl+'/v1/sessions/'+${JSON.stringify(prepared.session_id)}+'/agent-continuation?parent_turn_id='+${JSON.stringify(prepared.parent_turn_id)},{headers:{'X-HSC-Session':r.sessionToken}});if(!x.ok)throw Error('Receipt read failed');return x.json()})()`)
    if(receipt.status!=='awaiting_receipt'||receipt.replay_allowed!==false||receipt.next_turn_id!==prepared.next_turn_id)throw Error('Original receipt changed')
    const runtime=await app.cdp.evaluate(`(async()=>{const r=await window.desktop.getRuntimeConfig(),x=await fetch(r.localApiUrl+'/v1/sessions/'+${JSON.stringify(prepared.session_id)}+'/agent-runtime',{headers:{'X-HSC-Session':r.sessionToken}});if(!x.ok)throw Error('Runtime read failed');return x.json()})()`)
    if(runtime.continuation?.status!=='awaiting_receipt'||runtime.continuation?.next_turn_id!==prepared.next_turn_id||runtime.continuation?.replay_allowed!==false)throw Error('Runtime omitted original unknown receipt')
    await until(()=>app.cdp.evaluate(`document.body.innerText.includes('待核对的操作')&&document.body.innerText.includes('刚才的后续操作结果还没有确认')`),'Unknown operation notice missing from actual child surface')
    await sleep(1500)
    if((await app.cdp.evaluate('window.__continuationWrites')).length)throw Error('Historical receipt triggered a new operation')
    if(persisted()!==original)throw Error('Historical read changed original turns or steps')
    receipts.push({receipt,runtime_continuation:runtime.continuation,visible_unknown_notice:true})
    await screenshot(app.cdp,join(outputDir,`receipt-round-${round}.png`))
    await stopOwned(app);app=null
  }
  const after=await hashes()
  if(JSON.stringify(before)!==JSON.stringify(after))throw Error('Runtime changed during verification')
  await writeFile(join(outputDir,'result.json'),JSON.stringify({passed:true,fixture,prepared,receipts,before,after,
    owned_processes:owned.size,boundary:'Actual main Electron/backend and two full starts; synthetic preseeded production planner with two MockTransport replies. Microphone denied and conversation inactive: verifies original read-only receipt and no automatic execution, not spoken active-conversation notification. No real ASR, device calls, paid models, Windows or child outcome claim.'},null,2))
}catch(error){
  if(app?.cdp)await screenshot(app.cdp,join(outputDir,'failure.png')).catch(()=>{})
  await writeFile(join(outputDir,'failure.txt'),String(error.stack));process.exitCode=1
}finally{if(app)await stopOwned(app);for(const value of [...owned.values()])await stopOwned(value)}
