#!/usr/bin/env node
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'
import { startElectron, stopOwned, visible, screenshot, root, outputDir, pythonBin,
  owned, diagnosticSecrets, run, captureOwnedFailure, redactHarnessDiagnostic } from './electron-parent-report-delivery-e2e.mjs'

const args=new Map(process.argv.slice(2).reduce((all,v,i,a)=>i%2?all:[...all,[v,a[i+1]]],[]))
const reused=args.get('--fixture-dir')
if(reused&&!basename(reused).startsWith('hsc-behavior-goal-native-'))throw new Error('Only an owned synthetic fixture may be resumed')
const temporaryRoot=reused??await mkdtemp(join(tmpdir(),'hsc-behavior-goal-native-'))
const dataDir=join(temporaryRoot,'data'),profile=join(temporaryRoot,'profile'),databasePath=join(dataDir,'agent.db')
await mkdir(dataDir,{recursive:true});await mkdir(profile,{recursive:true});await mkdir(outputDir,{recursive:true})
const checks=[],images=[],boundaries=[],reads=[]
const pause=()=>new Promise(r=>setTimeout(r,180))
async function press(cdp,label,scope='.behavior-goal-review') {
  if(!await cdp.evaluate(`(()=>{const b=Array.from(document.querySelectorAll(${JSON.stringify(scope+' button')})).find(b=>b.textContent.includes(${JSON.stringify(label)}));if(!b||b.disabled)return false;b.click();return true})()`))throw new Error('Missing enabled command: '+label)
}
async function open(cdp,first) {
  await visible(cdp,'家长中心')
  await press(cdp,'家长中心','body');await visible(cdp,'家长 PIN')
  await cdp.evaluate(`(()=>{const input=document.querySelector('[aria-label="家长 PIN"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'2468');input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))})()`)
  await pause()
  const action=await cdp.evaluate(`Array.from(document.querySelectorAll('button')).find(b=>['设置并进入','解锁'].includes(b.textContent.trim())&&!b.disabled)?.textContent.trim()`)
  if(!action)throw new Error('Current parent setup/unlock action is not available')
  await press(cdp,action,'body');await visible(cdp,'报告审核与送达')
  await cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="选择孩子"] button')).find(b=>b.textContent.includes('四年级'))?.click()`)
  await press(cdp,'查看证据','.behavior-evidence-panel');await visible(cdp,'核对具体机会')
  await press(cdp,'核对具体机会');await visible(cdp,'请选择一次具体机会')
}
async function instrument(cdp,lose=false) {
  await cdp.evaluate(`(()=>{const original=window.fetch.bind(window);let lose=${lose};window.__behaviorNetwork=[];window.fetch=async(...args)=>{
    const url=String(args[0]);if(/^https?:/.test(url)&&!['localhost','127.0.0.1','::1','[::1]'].includes(new URL(url).hostname))throw new Error('No external requests in synthetic acceptance');
    const r=await original(...args);if(url.includes('/behavior/habit-experiments/')||url.endsWith('/longitudinal-model')){let data=null;try{data=await r.clone().json()}catch{}window.__behaviorNetwork.push({path:new URL(url,location.href).pathname,method:args[1]?.method??'GET',status:r.status,data,body:args[1]?.body});}
    if(lose&&args[1]?.method==='POST'&&url.endsWith('/confirmation')&&r.ok){lose=false;throw new TypeError('Synthetic response lost after durable confirmation')};return r}})()`)
}
async function select(cdp,label,value) {
  await cdp.evaluate(`(()=>{const e=document.querySelector('[aria-label='+${JSON.stringify(JSON.stringify(label))}+']');if(!e)throw new Error('Field missing');const p=e.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLTextAreaElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))})()`)
  await pause()
}
async function journal(cdp) {return cdp.evaluate(`(()=>{const k=Object.keys(localStorage).find(k=>k.startsWith('hsc:behavior-goal-review:child-primary-4:'));return k?JSON.parse(localStorage.getItem(k)):null})()`)}
async function shot(cdp,name,width,selector) {
  await cdp.send('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false})
  await cdp.evaluate(`document.querySelector(${JSON.stringify(selector)})?.scrollIntoView({block:'start'})`);await pause()
  const bounds=await cdp.evaluate(`(()=>{const p=document.querySelector(${JSON.stringify(selector)});if(!p)throw new Error('Screenshot target missing');return {width:innerWidth,scroll:document.documentElement.scrollWidth,text:p.textContent.slice(0,500),outside:[p,...p.querySelectorAll('select,textarea,fieldset,button,label,p,h4')].map(e=>{const r=e.getBoundingClientRect();return{tag:e.tagName,left:r.left,right:r.right,width:r.width,text:e.textContent.slice(0,60)}}).filter(r=>r.left < -1 || r.right>innerWidth+1)}})()`)
  boundaries.push({name,...bounds});if(bounds.scroll>width+1||bounds.outside.length)throw new Error('Visible layout overflow: '+JSON.stringify(bounds))
  const file=`${name}-${width}.png`;await screenshot(cdp,join(outputDir,file));images.push(file)
}
async function facts() {
  const result=await run(pythonBin,['-c',`import sqlite3,json,sys
c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
rows=[dict(r) for r in c.execute("SELECT event_type,client_action_id,payload_json FROM habit_experiment_event WHERE event_type IN ('goal_behavior_confirmed','goal_behavior_retracted') ORDER BY rowid")]
for r in rows:r['payload']=json.loads(r.pop('payload_json'));r['payload'].pop('authorization_ref',None)
print(json.dumps({'events':rows,'model_calls':c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0],'attempts':c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]}))`,databasePath])
  return JSON.parse(result.stdout)
}
try {
  if(!reused){
    const seed=await run(pythonBin,[join(root,'scripts/seed-behavior-goal-native.py'),'--data-dir',dataDir,'--receipt-path',join(outputDir,'seed.json')],{cwd:root,env:parentReportDeliveryEnvironment({root,pythonBin,dataDir,databasePath})})
    await writeFile(join(outputDir,'seed.log'),redactHarnessDiagnostic(seed.stdout+seed.stderr,[...diagnosticSecrets]))
  }
  let app=await startElectron(profile,dataDir,databasePath);await open(app.cdp,true);await instrument(app.cdp,true)
  const oid=await app.cdp.evaluate(`Array.from(document.querySelector('[aria-label="选择需要补充的机会"]').options).at(-1).value`)
  await select(app.cdp,'选择需要补充的机会',oid)
  const defaults=await app.cdp.evaluate(`Array.from(document.querySelectorAll('.behavior-goal-opportunity fieldset select')).map(e=>e.value)`)
  if(!defaults.every(v=>v==='unknown'))throw new Error('Unknown choices were promoted automatically')
  await select(app.cdp,'当时是否有这个机会','yes')
  await select(app.cdp,'标出不确定的步骤','yes');await select(app.cdp,'说清具体卡在哪里','unknown');await select(app.cdp,'先指出问题，再请求帮助','no')
  await select(app.cdp,'谁发起这次行动','child');await select(app.cdp,'协助程度','prompted')
  await select(app.cdp,'补充说明（可选）','本机合成验收：我提醒了一次，不能确认孩子是否说清卡点。')
  await shot(app.cdp,'01-original-opportunity',1100,'.behavior-goal-content')
  await shot(app.cdp,'02-three-state-correction',390,'.behavior-goal-opportunity fieldset')
  await press(app.cdp,'保存这次补充');await visible(app.cdp,'未收到可核对的回执')
  const pending=await journal(app.cdp),state=await facts()
  if(!pending?.pending || state.events.length!==1 || state.events[0].payload.criteria.stated_specific_blocker!==null)throw new Error('Nullable decision or durable original command missing')
  await writeFile(join(outputDir,'original-command.json'),JSON.stringify(pending.pending,null,2))
  checks.push('actual_parent_center_explicit_opportunity_three_state_actual_http_commit_lost_response')
  await writeFile(join(outputDir,'native-before-restart.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
  app=await startElectron(profile,dataDir,databasePath);await instrument(app.cdp);await open(app.cdp,false)
  await visible(app.cdp,'按原操作核对回执')
  if(JSON.stringify((await journal(app.cdp))?.pending)!==JSON.stringify(pending.pending)||(await facts()).events.length!==1)throw new Error('Restart rewrote or replayed pending')
  await press(app.cdp,'按原操作核对回执');await visible(app.cdp,'成长模型已按当前证据重读')
  const net=await app.cdp.evaluate('window.__behaviorNetwork')
  const post=net.find(r=>r.method==='POST')
  if(post?.body!==JSON.stringify(pending.pending)||!net.some(r=>r.path.endsWith('/longitudinal-model')&&r.status===200))throw new Error('Original retry or real ChildModel refresh missing')
  if((await journal(app.cdp))?.pending || (await facts()).events.length!==1)throw new Error('Idempotent retry created extra event')
  reads.push(...net.filter(r=>r.path.endsWith('/longitudinal-model')).map(r=>({status:r.status,child_id:r.data.child_id,fingerprint:r.data.evidence_fingerprint,habit_experiments:r.data.payload.behavior.habit_experiments})))
  await shot(app.cdp,'03-confirmed-restarted',1100,'.behavior-goal-current')
  checks.push('same_profile_and_database_restart_explicit_exact_key_retry_real_child_model_read')
  await press(app.cdp,'撤回这次补充');await visible(app.cdp,'补充已撤回')
  await writeFile(join(outputDir,'native-retracted.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
  app=await startElectron(profile,dataDir,databasePath);await instrument(app.cdp);await open(app.cdp,false)
  await visible(app.cdp,'当前没有有效补充；未知不计为失败。')
  await press(app.cdp,'刷新当前证据');await visible(app.cdp,'当前没有有效补充；未知不计为失败。')
  await shot(app.cdp,'04-revoked-restarted',390,'.behavior-goal-content')
  if((await facts()).events.length!==2)throw new Error('Refresh or restart replayed a confirmation')
  checks.push('withdraw_same_database_backend_restart_read_refresh_no_active_confirmation_no_replay')
  // Inspect C07 in this new synthetic database; never touch the retained C02 fixture.
  const rules=await app.cdp.evaluate(`document.querySelectorAll('[aria-label="考试规则依据"]').length`)
  if(!rules)throw new Error('Actual ParentCenter goal rules are not mounted')
  await shot(app.cdp,'05-pet-public-rule',1100,'[aria-label="考试规则依据"]')
  await shot(app.cdp,'06-pet-public-rule',390,'[aria-label="考试规则依据"]')
  await app.cdp.evaluate(`Array.from(document.querySelectorAll('[aria-label="选择孩子"] button')).find(b=>b.textContent.includes('初三'))?.click()`)
  await visible(app.cdp,'2027 年规则尚未核实')
  await shot(app.cdp,'07-nanjing-target-year',1100,'[aria-label="考试规则依据"]')
  await shot(app.cdp,'08-nanjing-target-year',390,'[aria-label="考试规则依据"]')
  if(await app.cdp.evaluate(`!!document.querySelector('.behavior-goal-opportunity')`))throw new Error('Another child inherited original opportunity')
  checks.push('other_child_no_original_opportunity_or_confirmation_c07_rules_public_reference_not_registration')
  const final=await facts()
  if(final.model_calls||final.attempts||final.events.map(r=>r.event_type).join(',')!=='goal_behavior_confirmed,goal_behavior_retracted')throw new Error('Unexpected execution or cloud facts')
  await writeFile(join(outputDir,'native-final.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]));await stopOwned(app)
  await writeFile(join(outputDir,'result.json'),JSON.stringify({status:'passed',checks,reads,state:final,boundaries,images,retained_fixture:temporaryRoot,synthetic_only:true,actual_parent_center:true,windows:false},null,2))
  console.log(JSON.stringify({status:'passed',checks:checks.length,outputDir}))
}catch(error){
  for(const [i,app] of [...owned.values()].entries())if(app.cdp)await writeFile(join(outputDir,`renderer-exceptions-${i}.json`),redactHarnessDiagnostic(JSON.stringify(app.cdp.events.filter(e=>e.method==='Runtime.exceptionThrown'),null,2),[...diagnosticSecrets]))
  await captureOwnedFailure({outputDir,temporaryRoot,apps:[...owned.values()],error,secrets:[...diagnosticSecrets],smtpMessages:0})
  const app=[...owned.values()].at(-1);if(app?.cdp)await screenshot(app.cdp,join(outputDir,'failure.png')).catch(()=>{})
  throw error
}finally{for(const app of [...owned.values()])await stopOwned(app)}
