// Synthetic recognized utterances enter the real renderer voice callback.
// Devices, cloud planning, HTTP execution and persistence are not mocked.
import { writeFile } from 'node:fs/promises'
const port = Number(process.argv[2] || 9231)
const output = process.argv[3]
if (!output || port === 9222) throw new Error('Use an isolated development desktop, never the family debugger')
let input = ''
for await (const part of process.stdin) input += part
const key = JSON.parse(input).deepseek_key
if (typeof key !== 'string' || !key.startsWith('sk-')) throw new Error('Missing probe key')
const target = (await (await fetch(`http://127.0.0.1:${port}/json/list`, { signal: AbortSignal.timeout(10000) })).json()).find(x => x.type === 'page')
if (!target?.url.includes('/candidates/') || target.url.includes('app.asar')) throw new Error('Not a development candidate')
const socket = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject })
let sequence = 0
const pending = new Map()
socket.onmessage = event => {
  const value = JSON.parse(String(event.data)), call = pending.get(value.id)
  if (!call) return
  pending.delete(value.id); clearTimeout(call.timer)
  if (value.error || value.result?.exceptionDetails) call.reject(new Error('Browser evaluation failed'))
  else call.resolve(value.result?.result?.value)
}
async function evaluate(expression) {
  const id = ++sequence
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, timer: setTimeout(() => { pending.delete(id); reject(new Error('Browser timeout')) }, 90000) })
    socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } }))
  })
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
async function until(read, label, timeout = 45000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) { const value = await read(); if (value) return value; await sleep(400) }
  throw new Error(`Timed out: ${label}`)
}
const report = { boundary: 'isolated development Electron; synthetic recognized text; real camera, cloud and stores; no family profile; no raw media exported', checks: [], failures: [] }
const require = (condition, label) => { (condition ? report.checks : report.failures).push(label) }
const apiInit = `window.__proofApi=async(path,body,method='POST',extra={})=>{const r=await window.desktop.getRuntimeConfig();const v=await fetch(r.localApiUrl+path,{method,headers:{'Content-Type':'application/json','X-HSC-Session':r.sessionToken,...extra},...(body===undefined?{}:{body:JSON.stringify(body)})});if(v.status===409&&path==='/v1/parent/setup')return window.__proofApi('/v1/parent/unlock',body);if(!v.ok)throw new Error(path+':'+v.status);return v.json()}`
async function ready() { return until(() => evaluate(`window.desktop?.getRuntimeConfig().then(r=>r.status==='ready')`), 'runtime', 90000) }
async function attach() {
  await ready()
  await evaluate(`(()=>{${apiInit};window.__voiceProof=[];const original=window.fetch;window.fetch=async(...args)=>{const response=await original(...args);const url=String(args[0]);if(/agent-turn|voice-interaction/.test(url)){const value=await response.clone().json().catch(()=>null);window.__voiceProof.push({url,status:response.status,value})}return response};return true})()`)
}
async function say(text) {
  await until(() => evaluate(`(()=>{const e=document.querySelector('.hands-free-control');if(!e)return false;let f=e[Object.keys(e).find(k=>k.startsWith('__reactFiber$'))];for(;f;f=f.return){const p=f.memoizedProps;if(typeof p?.onTranscript==='function'&&p.sessionId){p.onTranscript(${JSON.stringify(text)});return true}}return false})()`), 'voice entry callback')
}
async function turn(text) {
  await say(text)
  const value = await until(() => evaluate(`window.__voiceProof.find(x=>x.value?.transcript===${JSON.stringify(text)}&&x.value?.agent_turn_id)?.value`), 'cloud turn')
  return until(() => evaluate(`(()=>{const x=window.__voiceProof.find(x=>(x.url.endsWith('/v1/agent-turns/${value.agent_turn_id}/outcome')&&['completed','failed'].includes(x.value?.tool_status))||(x.url.endsWith('/v1/agent-turns/${value.agent_turn_id}/execute')&&['completed','failed'].includes(x.value?.turn?.tool_status)));return x?.value?.turn??x?.value})()`), `terminal ${value.action.type}`)
}
try {
  await ready()
  await evaluate(`window.desktop.saveModelSettings(${JSON.stringify({ provider: 'deepseek', apiKey: key, baseUrl: 'https://api.deepseek.com', model: 'deepseek-v4-pro', voiceIntentModel: 'deepseek-v4-flash' })}).then(()=>true)`)
  await ready()
  const sid = await evaluate(`(async()=>{${apiInit};const p=await __proofApi('/v1/parent/setup',{pin:'2468'});const s=await __proofApi('/v1/sessions',{child_id:'child-primary-4',client_action_id:'voice-controls-synthetic-session'});await window.desktop.saveWakeWordSettings({childId:'child-primary-4',wakeWord:'测试伙伴',inactivitySeconds:60,enabled:true,updatedAt:new Date().toISOString()});await window.desktop.saveVoiceOnboarding({childId:'child-primary-4',version:1,step:'completed',updatedAt:new Date().toISOString()});const ds=await navigator.mediaDevices.enumerateDevices();const camera=ds.find(d=>d.kind==='videoinput'&&/L523/i.test(d.label)),doc=ds.find(d=>d.kind==='videoinput'&&/HDCamera/i.test(d.label));if(!camera||!doc)throw new Error('Required real USB cameras missing');const hash=async d=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(d.deviceId)))].map(x=>x.toString(16).padStart(2,'0')).join('');await __proofApi('/v1/parent/device-bindings',{bindings:await Promise.all([[camera,'camera'],[doc,'document_camera']].map(async([d,role])=>({device_type:role,system_id_hash:await hash(d),label:d.label,capabilities:{width:1280,height:720},verified_at:new Date().toISOString()}))),client_action_id:'voice-controls-device-bindings'},'PUT',{'X-HSC-Parent':p.parent_token});return s.session_id})()`)
  report.session_id = sid
  await evaluate('location.reload();true'); await sleep(1200); await attach()
  await turn('把唤醒词改为小枝小枝')
  await say('对，就用这个名字')
  const wake = await until(() => evaluate(`window.desktop.getWakeWordSettings('child-primary-4').then(x=>x?.wakeWord==='小枝小枝'?x:null)`), 'spoken wake confirmation persisted')
  require(wake.wakeWord === '小枝小枝', 'voice_name_confirmed_and_saved')
  const opened = await turn('你把摄像头也打开')
  report.camera_command = { action: opened.action?.type, status: opened.tool_status, error: opened.tool_error_code }
  require(opened.action?.type === 'resume_observation', 'camera_open_routes_to_execution')
  require(opened.tool_status === 'completed' && opened.action?.type === 'resume_observation', 'camera_resume_completed')
  const frames = await evaluate(`[...document.querySelectorAll('video')].map(v=>({width:v.videoWidth,height:v.videoHeight,ready:v.readyState,live:v.srcObject?.getVideoTracks().some(t=>t.readyState==='live')??false}))`)
  report.camera_frames = frames
  require(frames.some(x => x.live && x.width > 0 && x.ready >= 2), 'actual_camera_frame')
  const queried = await turn('摄像头现在打开了吗')
  require(queried.action?.type === 'query_device_status', 'device_question_queries_not_opens')
  await turn('关闭摄像头')
  const paused = await evaluate(`window.__proofApi('/v1/sessions/${sid}',undefined,'GET')`)
  require(paused.session.observation_status === 'paused', 'camera_pause_persisted')
  const docStatus = await turn('高拍仪现在打开了吗')
  require(docStatus.action?.type === 'query_device_status' && docStatus.tool_status === 'completed', 'document_status_queries_not_captures')
  await turn('稍微有点遮挡是正常的，你不要提示')
  const prefs = await evaluate(`window.__proofApi('/v1/sessions/${sid}/agent-runtime',undefined,'GET').then(x=>x.document_guidance_preferences)`)
  require(prefs?.settings_saved === true && prefs.occlusion_reminders_enabled === false, 'occlusion_preference_saved')
  await evaluate('location.reload();true'); await sleep(1200); await attach()
  require(await evaluate(`window.desktop.getWakeWordSettings('child-primary-4').then(x=>x?.wakeWord==='小枝小枝')`), 'wake_name_survives_reload')
  await evaluate('window.desktop.restartLocalService().then(()=>true)'); await ready()
  const restored = await evaluate(`window.__proofApi('/v1/sessions/${sid}/agent-runtime',undefined,'GET').then(x=>x.document_guidance_preferences)`)
  require(restored?.occlusion_reminders_enabled === false, 'preference_survives_backend_restart')
  report.passed = report.failures.length === 0
  if (!report.passed) process.exitCode = 1
} catch (error) {
  report.passed = false; report.error = String(error.message).slice(0,300)
  report.last_results = await evaluate(`(window.__voiceProof||[]).slice(-8).map(x=>({status:x.status,path:x.url.replace(/^.*\/v1/,'/v1'),intent:x.value?.intent,action:x.value?.action?.type,tool_status:x.value?.tool_status,response:x.value?.response_text}))`).catch(()=>[])
  process.exitCode = 1
} finally {
  await writeFile(output, JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: report.passed, checks: report.checks, error: report.error }))
  socket.close()
}
