#!/usr/bin/env node
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'
import { startElectron, stopOwned, unlockParent, visible, screenshot, root, outputDir, pythonBin,
  owned, diagnosticSecrets, run, captureOwnedFailure, redactHarnessDiagnostic } from './electron-parent-report-delivery-e2e.mjs'

const args = new Map(process.argv.slice(2).reduce((items,value,index,values)=>index%2?items:[...items,[value,values[index+1]]],[]))
const reused = args.get('--fixture-dir')
const resumeRevoked = args.get('--resume-revoked') === 'true'
const resumeAuthorized = args.get('--resume-authorized') === 'true'
const distinctRights = args.get('--distinct-rights') === 'true'
if(resumeRevoked && !reused)throw new Error('Revoked continuation requires the retained fixture')
if(resumeAuthorized && (!reused || !distinctRights || !args.get('--pending-command-path')))throw new Error('Authorized continuation requires retained fixture and original command evidence')
if(reused && !basename(reused).startsWith('hsc-material-purpose-native-'))throw new Error('Only a retained synthetic material fixture may be reused')
const temporaryRoot = reused ?? await mkdtemp(join(tmpdir(), 'hsc-material-purpose-native-'))
const dataDir = join(temporaryRoot, 'data'), profile = join(temporaryRoot, 'profile'), databasePath = join(dataDir, 'open-supply.db')
await mkdir(dataDir, {recursive:true}); await mkdir(profile, {recursive:true}); await mkdir(outputDir, {recursive:true})
const checks = [], images = [], boundaries = []
const pause = () => new Promise(resolve => setTimeout(resolve, 180))
async function press(cdp, text) {
  const clicked = await cdp.evaluate(`(() => { const b=Array.from(document.querySelectorAll('.writing-question-authorization button')).find(e=>e.textContent.includes(${JSON.stringify(text)}) || e.getAttribute('aria-label')===${JSON.stringify(text)}); if(!b || b.disabled)return false; b.click();return true })()`)
  if (!clicked) throw new Error(`Missing enabled permission command: ${text}`)
}
async function readJournal(cdp) {
  return cdp.evaluate(`(() => { const key=Object.keys(localStorage).find(k=>k.startsWith('hsc-writing-authorization:child-junior-3:'));return key?JSON.parse(localStorage.getItem(key)):null })()`)
}
async function fill(cdp, index, license = 'original_author') {
  for (const [selector, value] of [['select', license], ['input[type=text]', license === 'CC0-1.0' ? 'https://example.invalid/synthetic-material/cc0' : `本机合成第${index+1}份独立声明`],
    ['textarea', `第${index+1}份文字是本机验收合成资料，独立确认仅允许列明的形成性反馈用途，不涉及真实家庭资料。`]]) {
    await cdp.evaluate(`(() => {const group=document.querySelectorAll('.writing-question-authorization fieldset')[${index}]; const e=group.querySelector(${JSON.stringify(selector)}); const p=e.tagName==='SELECT'?HTMLSelectElement.prototype:e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`)
    await pause()
  }
  await cdp.evaluate(`document.querySelectorAll('.writing-question-authorization fieldset')[${index}].querySelector('label.wqa-declaration').click()`)
  await pause()
}
async function image(cdp, label, width, selector) {
  await cdp.send('Emulation.setDeviceMetricsOverride', {width, height:900, deviceScaleFactor:1, mobile:false})
  await cdp.evaluate(`document.querySelector(${JSON.stringify(selector)})?.scrollIntoView({block:'start'})`)
  await pause()
  const bounds = await cdp.evaluate(`(() => {const p=document.querySelector('.writing-question-authorization');return {viewport:innerWidth,document:document.documentElement.scrollWidth,rows:[p,...p.querySelectorAll('fieldset,input,textarea,select,legend')].map(e=>{const r=e.getBoundingClientRect();return {tag:e.tagName,left:r.left,right:r.right,width:r.width,scroll:e.scrollWidth,client:e.clientWidth}})}})()`)
  if (bounds.document > width+1 || bounds.rows.some(r=>r.left < -1 || r.right > width+1)) throw new Error(`Material UI overflow: ${JSON.stringify(bounds)}`)
  boundaries.push({label,...bounds})
  const name=`${label}-${width}.png`; await screenshot(cdp,join(outputDir,name)); images.push(name)
}
async function open(cdp, first) {
  await unlockParent(cdp, first)
  await cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="选择孩子"] button')).find(b=>b.textContent.includes('初三'))?.click()`)
  await visible(cdp, '题目文字用途授权')
  await visible(cdp, '本题包含2份材料')
}
try {
  if(!reused) {
    const seed=await run(pythonBin,[join(root,'scripts/seed-material-purpose-native.py'),'--data-dir',dataDir,'--receipt-path',join(outputDir,'seed.json')],
      {cwd:root,env:parentReportDeliveryEnvironment({root,pythonBin,dataDir,databasePath})})
    await writeFile(join(outputDir,'seed.log'),redactHarnessDiagnostic(seed.stdout+seed.stderr,[...diagnosticSecrets]))
  }
  let app=await startElectron(profile,dataDir,databasePath)
  await open(app.cdp,!reused)
  if(!resumeRevoked && !resumeAuthorized) {
  await visible(app.cdp,'尚未授权此题用途')
  const initial=await app.cdp.evaluate(`({groups:document.querySelectorAll('.writing-question-authorization fieldset').length,panel:!!document.querySelector('.parent-center'),question:document.querySelector('.writing-question-authorization .wqa-question')?.textContent})`)
  if(initial.groups!==3 || !initial.question?.includes('二十欧姆')) throw new Error('Complete question or independent materials missing')
  await fill(app.cdp,0)
  const disabled=await app.cdp.evaluate(`Array.from(document.querySelectorAll('.writing-question-authorization button')).find(e=>e.textContent.includes('确认用途授权')).disabled`)
  if(!disabled) throw new Error('Question declaration authorized material without independent permission')
  await fill(app.cdp,1);await fill(app.cdp,2)
  await image(app.cdp,'01-question-declaration',1100,'.writing-question-authorization')
  await image(app.cdp,'02-material-one',390,'.writing-question-authorization .wqa-material')
  await image(app.cdp,'03-material-two',390,'.writing-question-authorization .wqa-material:last-of-type')
  checks.push('full_parent_center_original_review_entry_two_independent_material_decisions')
  // Deliver to the actual backend, then lose the response at the renderer boundary.
  await app.cdp.evaluate(`(() => {const original=window.fetch.bind(window);window.__materialWrites=[];let lose=true;window.fetch=async(...args)=>{const url=String(args[0]),init=args[1];const r=await original(...args);if(init?.method==='POST'&&url.includes('/writing-question-authorizations/')){window.__materialWrites.push({url,body:init.body,status:r.status});if(lose&&url.endsWith('/grant')&&r.ok){lose=false;await r.clone().text();throw new TypeError('Synthetic lost response after durable grant')}}return r}})()`)
  await press(app.cdp,'确认用途授权');await visible(app.cdp,'未能核对结果')
  const pending=await readJournal(app.cdp)
  if(pending?.schema!==2 || pending.pending?.body.material_license_bases?.length!==2) throw new Error('Material pending was not durable schema 2')
  await writeFile(join(outputDir,'original-command.json'),JSON.stringify(pending.pending,null,2))
  await writeFile(join(outputDir,'native-1.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
  app=await startElectron(profile,dataDir,databasePath);await open(app.cdp,false)
  await visible(app.cdp,'当前已授权指定用途')
  const reopened=await readJournal(app.cdp)
  if(JSON.stringify(reopened?.pending)!==JSON.stringify(pending.pending)) throw new Error('Restart rewrote pending command')
  const before=await run(pythonBin,['-c','import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute("SELECT COUNT(*) FROM writing_question_authorization_event").fetchone()[0])',databasePath])
  if(Number(before.stdout)!==1) throw new Error('Restart unexpectedly submitted another decision')
  await press(app.cdp,'重试原授权请求');await visible(app.cdp,'操作回执已核对')
  if(await readJournal(app.cdp)) throw new Error('Confirmed original pending not cleared')
  checks.push('actual_http_commit_lost_response_backend_renderer_restart_exact_key_retry_single_event')
  await image(app.cdp,'04-restarted-authorized',1100,'.writing-question-authorization')
  await press(app.cdp,'撤销此题用途授权');await visible(app.cdp,'当前授权已撤销')
  await writeFile(join(outputDir,'native-2.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
  app=await startElectron(profile,dataDir,databasePath);await open(app.cdp,false)
  }
  if(!resumeAuthorized) {
  await visible(app.cdp,'当前授权已撤销')
  await press(app.cdp,'刷新用途授权');await visible(app.cdp,'当前授权已撤销')
  await image(app.cdp,'05-restarted-revoked',390,'.writing-question-authorization')
  const blank=await app.cdp.evaluate(`Array.from(document.querySelectorAll('.writing-question-authorization input[type=checkbox]')).every(e=>!e.checked)`)
  if(!blank) throw new Error('Old declaration survived revoke/restart')
  checks.push('revoke_backend_renderer_restart_refresh_stays_revoked_without_replay')
  }
  if(distinctRights) {
    if(!resumeAuthorized) {
    await fill(app.cdp,0)
    await fill(app.cdp,1,'CC0-1.0');await fill(app.cdp,2,'explicit_cloud_processing_permission')
    await image(app.cdp,'06-distinct-material-rights',390,'.writing-question-authorization .wqa-material')
    await app.cdp.evaluate(`(() => {const original=window.fetch.bind(window);let lose=true;window.fetch=async(...args)=>{const response=await original(...args);if(lose&&args[1]?.method==='POST'&&String(args[0]).endsWith('/grant')&&response.ok){lose=false;await response.clone().text();throw new TypeError('Synthetic second lost response')}return response}})()`)
    await press(app.cdp,'确认用途授权');await visible(app.cdp,'未能核对结果')
    const distinct=await readJournal(app.cdp)
    const licenses=distinct?.pending?.body.material_license_bases?.map(m=>m.license_basis.license_id)
    if(JSON.stringify(licenses)!==JSON.stringify(['CC0-1.0','explicit_cloud_processing_permission']))throw new Error('Material rights were copied from the question')
    await writeFile(join(outputDir,'distinct-rights-command.json'),JSON.stringify(distinct.pending,null,2))
    await writeFile(join(outputDir,'native-distinct-grant.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
    app=await startElectron(profile,dataDir,databasePath);await open(app.cdp,false)
    await visible(app.cdp,'当前已授权指定用途')
    if(JSON.stringify((await readJournal(app.cdp))?.pending)!==JSON.stringify(distinct.pending))throw new Error('Distinct material pending changed across restart')
    } else {
      await visible(app.cdp,'当前已授权指定用途')
      const original=JSON.parse(await readFile(args.get('--pending-command-path'),'utf8'))
      if(JSON.stringify((await readJournal(app.cdp))?.pending)!==JSON.stringify(original) || original.body.expected_revision!==2)throw new Error('Revision3 pending differs from preserved command')
      const count=await run(pythonBin,['-c','import sqlite3,sys;print(sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True).execute("SELECT MAX(revision) FROM writing_question_authorization_event").fetchone()[0])',databasePath])
      if(Number(count.stdout)!==3)throw new Error('Expected exactly the preserved revision3')
      await app.cdp.evaluate(`(() => {const original=window.fetch.bind(window);window.fetch=(...args)=>{if(args[1]?.method==='POST'&&String(args[0]).includes('/writing-question-authorizations/')&&String(args[0]).endsWith('/grant'))throw new Error('Grant replay prohibited during revision3 continuation');return original(...args)}})()`)
      await writeFile(join(outputDir,'preserved-command.json'),JSON.stringify(original,null,2))
    }
    checks.push('two_different_material_rights_committed_and_recovered_without_auto_replay')
    // Change the actual current question source through the parent curriculum controls.
    const changed=await app.cdp.evaluate(`(() => {const p=document.querySelector('.curriculum-supply-review');const label=Array.from(p.querySelectorAll('label')).find(e=>e.firstChild?.textContent==='当前教材');const s=label?.querySelector('select');const o=Array.from(s?.options??[]).find(e=>e.text.includes('Isolated authored open task')&&!e.disabled);if(!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,o.value);s.dispatchEvent(new Event('change',{bubbles:true}));return true})()`)
    if(!changed)throw new Error('Existing alternative reviewed source is not available')
    await pause()
    let standard=false
    for(let count=0;count<30&&!standard;count++) {
      standard=await app.cdp.evaluate(`(() => {const p=document.querySelector('.curriculum-supply-review');const s=Array.from(p.querySelectorAll('label')).find(e=>e.firstChild?.textContent==='对照课标')?.querySelector('select');const o=Array.from(s?.options??[]).find(e=>e.value&&!e.disabled);if(!o)return false;Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(s,o.value);s.dispatchEvent(new Event('change',{bubbles:true}));return true})()`)
      await pause()
    }
    if(!standard)throw new Error('Reviewed source standard did not load')
    const selected=await app.cdp.evaluate(`(() => {const b=Array.from(document.querySelectorAll('.curriculum-supply-review button')).find(e=>e.textContent.includes('保存当前教材'));if(!b||b.disabled)return false;b.click();return true})()`)
    if(!selected)throw new Error('Current curriculum cannot be saved')
    await visible(app.cdp,'当前用途许可不可用')
    if(await readJournal(app.cdp))throw new Error('Material source change retained stale pending permission')
    const originalGone=await app.cdp.evaluate(`!document.querySelector('.writing-question-authorization .wqa-question') && !Array.from(document.querySelectorAll('.writing-question-authorization button')).some(b=>b.textContent.includes('确认用途授权'))`)
    if(!originalGone)throw new Error('Changed source retained authorizable old material text')
    await image(app.cdp,'07-source-changed-invalidated',390,'.writing-question-authorization')
    checks.push('real_current_source_change_invalidates_old_question_and_clears_pending_without_replay')
    await press(app.cdp,'撤销此题用途授权');await visible(app.cdp,'当前授权已撤销')
    await writeFile(join(outputDir,'native-distinct-invalidated.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
    app=await startElectron(profile,dataDir,databasePath);await unlockParent(app.cdp,false)
    await app.cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="选择孩子"] button')).find(b=>b.textContent.includes('初三'))?.click()`)
    await visible(app.cdp,'当前授权已撤销')
    await image(app.cdp,'08-revoked-changed-source-restart',1100,'.writing-question-authorization')
    checks.push('invalidated_scope_can_be_explicitly_revoked_and_remains_revoked_after_restart')
  }
  await app.cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="选择孩子"] button')).find(b=>b.textContent.includes('四年级'))?.click()`)
  await new Promise(resolve=>setTimeout(resolve,700))
  if(await app.cdp.evaluate(`!!document.querySelector('.writing-question-authorization')`)) throw new Error('Other child inherited material permission')
  checks.push('child_switch_removes_original_question_and_material_rights')
  await writeFile(join(outputDir,'native-3.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
  const facts=await run(pythonBin,['-c',`import sqlite3,json,sys
c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row
print(json.dumps({'events':[dict(r) for r in c.execute('SELECT revision,command,client_action_id FROM writing_question_authorization_event ORDER BY revision')], 'attempts':c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0], 'model_calls':c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0], 'deliveries':c.execute('SELECT COUNT(*) FROM report_delivery_outbox').fetchone()[0]}))`,databasePath])
  const state=JSON.parse(facts.stdout)
  const expectedCommands=distinctRights?['grant','revoke','grant','revoke']:['grant','revoke']
  if(JSON.stringify(state.events.map(e=>e.command))!==JSON.stringify(expectedCommands) || state.attempts || state.model_calls || state.deliveries)throw new Error('Unexpected learning/cloud/send facts')
  checks.push('exact_grant_revoke_history_without_fabricated_learning_cloud_or_delivery')
  await writeFile(join(outputDir,'result.json'),JSON.stringify({status:'passed',checks,state,boundaries,images,synthetic_only:true,actual_parent_center:true,windows:false,retained_fixture:temporaryRoot},null,2))
  console.log(JSON.stringify({status:'passed',checks:checks.length,outputDir}))
} catch(error) {
  await captureOwnedFailure({outputDir,temporaryRoot,apps:[...owned.values()],error,secrets:[...diagnosticSecrets],smtpMessages:0})
  const latest=[...owned.values()].at(-1)
  if(latest?.cdp)await screenshot(latest.cdp,join(outputDir,'failure.png')).catch(()=>undefined)
  throw error
} finally {
  for(const app of [...owned.values()])await stopOwned(app)
}
