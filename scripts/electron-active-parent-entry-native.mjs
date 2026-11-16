#!/usr/bin/env node
import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, join, resolve } from 'node:path'
import { parentReportDeliveryEnvironment } from './electron-parent-report-delivery-env.mjs'
import { awaitCdpResponse, CDP_COMMAND_TIMEOUT_MS } from './electron-owned-process.mjs'
import { stopOwned, visible, screenshot, root, outputDir, pythonBin, owned, diagnosticSecrets,
  run, captureOwnedFailure, redactHarnessDiagnostic } from './electron-parent-report-delivery-e2e.mjs'

const args=new Map(process.argv.slice(2).reduce((all,v,i,a)=>i%2?all:[...all,[v,a[i+1]]],[]))
const electronBin=resolve(args.get('--electron-bin'))
const reused=args.get('--fixture-dir')
if(reused&&!basename(reused).startsWith('hsc-active-parent-entry-'))throw new Error('Only the owned active fixture may be continued')
const temporaryRoot=reused??await mkdtemp(join(tmpdir(),'hsc-active-parent-entry-'))
const dataDir=join(temporaryRoot,'data'),profile=join(temporaryRoot,'profile'),databasePath=join(dataDir,'agent.db')
await mkdir(dataDir,{recursive:true});await mkdir(profile,{recursive:true});await mkdir(outputDir,{recursive:true})
const delay=ms=>new Promise(r=>setTimeout(r,ms))
async function until(read,label) {
  const start=Date.now();while(Date.now()-start<45000){try{const value=await read();if(value)return value}catch{}await delay(120)}
  throw new Error('Timed out: '+label)
}
class Cdp {
  constructor(url){this.socket=new WebSocket(url);this.pending=new Map();this.nextId=0;this.events=[]}
  async open(){await new Promise((yes,no)=>{this.socket.addEventListener('open',yes,{once:true});this.socket.addEventListener('error',no,{once:true})});this.socket.addEventListener('message',event=>{const m=JSON.parse(String(event.data)),p=this.pending.get(m.id);if(!p){if(m.method)this.events.push(m);return}this.pending.delete(m.id);m.error?p.reject(new Error(m.error.message)):p.resolve(m.result)})}
  send(method,params={}){const id=++this.nextId,p=awaitCdpResponse(this.pending,id,method,CDP_COMMAND_TIMEOUT_MS);this.socket.send(JSON.stringify({id,method,params}));return p}
  async evaluate(expression){const r=await this.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw new Error(r.exceptionDetails.exception?.description??r.exceptionDetails.text);return r.result.value}
  close(){this.socket.close()}
}
async function launch(initialScript){
  const server=createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r))
  let logs=''
  const child=spawn(electronBin,[root,`--remote-debugging-port=${port}`,'--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'],{
    cwd:root,detached:true,stdio:['ignore','pipe','pipe'],env:parentReportDeliveryEnvironment({root,pythonBin,profile,dataDir,databasePath})})
  const app={child,cdp:null,cdpPort:port,phase:'cdp-page',logs:()=>logs,runtime:null};owned.set(child,app)
  child.stdout.on('data',b=>{logs+=b});child.stderr.on('data',b=>{logs+=b})
  const page=await until(async()=>{const r=await fetch(`http://127.0.0.1:${port}/json/list`);return(await r.json()).find(p=>p.type==='page'&&p.webSocketDebuggerUrl)},'owned Electron page')
  const cdp=new Cdp(page.webSocketDebuggerUrl);app.cdp=cdp;await cdp.open();await cdp.send('Page.enable');await cdp.send('Runtime.enable')
  await cdp.send('Page.addScriptToEvaluateOnNewDocument',{source:initialScript})
  await until(async()=>{const r=await cdp.evaluate('window.desktop?.getRuntimeConfig()');if(r?.sessionToken)diagnosticSecrets.add(r.sessionToken);app.runtime={status:r?.status};return r?.status==='ready'},'owned backend ready')
  // Reload only to install the pre-document interception; the original active DB is unchanged.
  await cdp.send('Page.reload',{ignoreCache:true});app.phase='ready';return app
}
async function button(cdp,text){await until(()=>cdp.evaluate(`(()=>{const b=Array.from(document.querySelectorAll('button')).find(b=>(b.textContent.trim()===${JSON.stringify(text)}||b.title===${JSON.stringify(text)})&&!b.disabled);if(!b)return false;b.click();return true})()`),'button '+text)}
async function facts(){
  const r=await run(pythonBin,['-c',`import sqlite3,json,sys
c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
print(json.dumps({'sessions':[dict(r) for r in c.execute('SELECT session_id,child_id,status,state FROM daily_session ORDER BY session_id')], 'model_calls':c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0], 'attempts':c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0]}))`,databasePath]);return JSON.parse(r.stdout)
}
try {
  if(!reused){
    const seeded=await run(pythonBin,[join(root,'scripts/seed-active-parent-entry.py'),'--data-dir',dataDir,'--receipt-path',join(outputDir,'seed.json')],{cwd:root,env:parentReportDeliveryEnvironment({root,pythonBin,dataDir,databasePath})})
    await writeFile(join(outputDir,'seed.log'),redactHarnessDiagnostic(seeded.stdout+seeded.stderr,[...diagnosticSecrets]))
  }
  const existing=reused?await facts():null
  if(existing&&(existing.sessions.length!==1||existing.sessions[0].status!=='active'))throw new Error('Retained original session must still be uniquely active')
  const seed=reused?existing.sessions[0]:JSON.parse(await readFile(join(outputDir,'seed.json'),'utf8'))
  const script=`(()=>{
    const proof=window.__entryProof={requests:[],held:false,released:false,delivered:false,wakeDelivered:false};let once=true;
    const original=window.fetch.bind(window);
    window.fetch=async(...args)=>{const path=new URL(String(args[0]),location.href).pathname,method=args[1]?.method??'GET';
      if(/^https?:/.test(String(args[0]))&&!['localhost','127.0.0.1','::1','[::1]'].includes(new URL(String(args[0])).hostname))throw new Error('No external requests');
      const response=await original(...args);
      if(path==='/v1/bootstrap'||path===${JSON.stringify('/v1/sessions/'+seed.session_id)}||path==='/v1/sessions'||path==='/v1/parent/unlock'){
        const data=await response.clone().json().catch(()=>null);proof.requests.push({path,method,status:response.status,active_session_id:data?.active_session?.session_id??null,session_id:data?.session?.session_id??null});
      }
      if(once&&method==='GET'&&path===${JSON.stringify('/v1/sessions/'+seed.session_id)}){
        once=false;const data=await response.clone().json();if(!response.ok||data.session?.session_id!==${JSON.stringify(seed.session_id)})throw new Error('Original real workspace response invalid');
        proof.held=true;await new Promise(r=>{window.__releaseOriginalWorkspace=()=>{proof.released=true;r()}});proof.delivered=true;
      }
      return response;
    };
    Object.defineProperty(navigator.mediaDevices,'getUserMedia',{configurable:true,value:async()=>{throw new DOMException('No physical media in entry acceptance','NotAllowedError')}});
  })()`
  const app=await launch(script),cdp=app.cdp
  await until(()=>cdp.evaluate('window.__entryProof?.held'),'real original workspace response held')
  const before=await facts()
  if(before.sessions.length!==1||before.sessions[0].status!=='active')throw new Error('Fixture not active before parent navigation')
  await button(cdp,'家长中心');await visible(cdp,'家长 PIN')
  await cdp.evaluate(`(()=>{const e=document.querySelector('[aria-label="家长 PIN"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'2468');e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))})()`)
  await button(cdp,'解锁');await visible(cdp,'报告审核与送达')
  if(!await cdp.evaluate('window.__entryProof.held&&!window.__entryProof.released'))throw new Error('Response was not held during real unlock')
  await cdp.send('Emulation.setDeviceMetricsOverride',{width:1100,height:900,deviceScaleFactor:1,mobile:false})
  await screenshot(cdp,join(outputDir,'01-parent-unlocked-response-held-1100.png'))
  await cdp.evaluate('window.__releaseOriginalWorkspace()')
  await until(()=>cdp.evaluate('window.__entryProof.delivered'),'real response delivered')
  await delay(500)
  if(!await cdp.evaluate(`!!document.querySelector('.parent-shell .parent-workspace')&&!document.querySelector('.session-shell')`))throw new Error('Late auto-resume replaced parent view')
  await cdp.send('Emulation.setDeviceMetricsOverride',{width:390,height:900,deviceScaleFactor:1,mobile:false})
  await screenshot(cdp,join(outputDir,'02-parent-retained-after-real-response-390.png'))
  await button(cdp,'返回孩子首页');await visible(cdp,'家长中心')
  // Inject one already-recognized synthetic wake into the current production voice component.
  // No claim is made about microphone, VAD or ASR recognition accuracy.
  await until(()=>cdp.evaluate(`(()=>{const node=document.querySelector('.household-standby-voice');if(!node)return false;let f=node[Object.keys(node).find(k=>k.startsWith('__reactFiber$'))];for(;f;f=f.return){const p=f.memoizedProps;if(typeof p?.onWake==='function'&&Array.isArray(p.profiles)){const profile=p.profiles.find(p=>p.childId===${JSON.stringify(seed.child_id)});if(!profile)return false;window.__entryProof.wakeDelivered=true;void p.onWake(profile,'');return true}}return false})()`),'current voice-entry wake callback')
  await until(()=>cdp.evaluate(`!!document.querySelector('.session-shell')&&!document.querySelector('.parent-shell')`),'original child workspace after new wake')
  await cdp.send('Emulation.setDeviceMetricsOverride',{width:1100,height:900,deviceScaleFactor:1,mobile:false})
  await screenshot(cdp,join(outputDir,'03-current-voice-entry-original-session-1100.png'))
  const proof=await cdp.evaluate('window.__entryProof'),after=await facts()
  const workspaceReads=proof.requests.filter(r=>r.path===`/v1/sessions/${seed.session_id}`&&r.method==='GET')
  if(workspaceReads.length!==2||workspaceReads.some(r=>r.session_id!==seed.session_id||r.status!==200))throw new Error('Voice entry did not re-read the original real session')
  if(!proof.requests.some(r=>r.path==='/v1/bootstrap'&&r.active_session_id===seed.session_id)||!proof.requests.some(r=>r.path==='/v1/parent/unlock'&&r.status===200))throw new Error('Real bootstrap or parent unlock missing')
  if(proof.requests.some(r=>r.path==='/v1/sessions'&&r.method==='POST')||after.sessions.length!==1||after.sessions[0].session_id!==seed.session_id||after.sessions[0].status!=='active'||after.model_calls||after.attempts)throw new Error('Original active session changed, duplicated, or cloud/learning facts appeared')
  await writeFile(join(outputDir,'native.log'),redactHarnessDiagnostic(app.logs(),[...diagnosticSecrets]))
  await stopOwned(app)
  await writeFile(join(outputDir,'result.json'),JSON.stringify({status:'passed',checks:['real_active_bootstrap_real_workspace_response_held_during_parent_unlock','late_real_response_keeps_parent_view','new_current_voice_callback_reads_original_session_without_cancel_or_duplicate'],before,after,proof,retained_fixture:temporaryRoot,synthetic_only:true,asr_boundary:'recognized-wake callback injection, no microphone/VAD/ASR claim',actual_parent_center:true,windows:false},null,2))
  console.log(JSON.stringify({status:'passed',checks:3,outputDir}))
}catch(error){
  await captureOwnedFailure({outputDir,temporaryRoot,apps:[...owned.values()],error,secrets:[...diagnosticSecrets],smtpMessages:0})
  for(const app of owned.values())if(app.cdp){await screenshot(app.cdp,join(outputDir,'failure.png')).catch(()=>{});await writeFile(join(outputDir,'renderer-exceptions.json'),redactHarnessDiagnostic(JSON.stringify(app.cdp.events.filter(e=>e.method==='Runtime.exceptionThrown'),null,2),[...diagnosticSecrets]))}
  throw error
}finally{for(const app of [...owned.values()])await stopOwned(app)}
