// Bounded current-source qualification. Only a new synthetic SQLite is writable.
const { app, BrowserWindow, ipcMain } = require('electron')
const fs = require('node:fs'), path = require('node:path'), { spawn, execFileSync } = require('node:child_process')
const http = require('node:http')
const root = path.resolve(process.argv[2] || '')
const windows = process.platform === 'win32'
const owned = root.startsWith('/private/tmp/hsc-public-reply-') || (windows && path.dirname(root) === 'C:\\HSC\\diagnostics' && /^hsc-public-reply-20261006-[a-zA-Z0-9-]+$/.test(path.basename(root)))
if (!owned || !fs.existsSync(root) || fs.existsSync(path.join(root, 'status.json'))) process.exit(2)
const repo = windows ? root : path.resolve(__dirname, '..')
const python = windows ? 'C:\\HSC\\candidates\\refactor-v83-key-loss-20261003-r5\\.venv\\Scripts\\python.exe' : path.join(repo, '.venv/bin/python')
const matrix = process.argv.includes('--matrix')
const cancelOnly = process.argv.includes('--cancel-check')
app.setPath('userData', path.join(root, 'desktop'))
app.on('window-all-closed',()=>{})
let child, service, win, server, retired = [], runtime, turns = []
const started = Date.now(), status = { status: 'running', paid_planner_requests: 0, speech_generation_attempts: 0,
  supplier_real: false, input_asr_real: false, family_data_sent: false, hardware_audible_verified: false, scenarios: [] }
const save = () => fs.writeFileSync(path.join(root, 'status.json'), JSON.stringify(status, null, 2))
const finish = async (code, reason) => {
  if (status.status !== 'running') return
  status.status='finishing'; status.reason = reason; status.elapsed_ms = Date.now()-started; save()
  await service?.close().catch(() => undefined)
  if (win && !win.isDestroyed()) win.destroy()
  if (server) await new Promise(resolve => server.close(resolve))
  if (child && child.exitCode === null) {
    child.kill('SIGTERM'); await new Promise(resolve => { child.once('exit', resolve); setTimeout(() => { child.kill('SIGKILL'); resolve() }, 5000).unref() })
  }
  status.status=code?'failed':'observed';status.owned_resources_closed=true;save(); app.exit(code)
}
setTimeout(() => void finish(2, 'qualification_deadline'), matrix ? 300000 : 180000).unref()
app.whenReady().then(async () => {
  status.stage='load_current_source'
  // Resolve Electron's private bindings from the owning desktop module, while
  // keeping the freshly bundled candidate outside the household checkout data.
  const candidate = { exports: {} }
  new Function('require','module','exports',fs.readFileSync(path.join(root,'native.cjs'),'utf8'))(require,candidate,candidate.exports)
  const native = candidate.exports
  const settingsPath = windows ? path.join(app.getPath('appData'),'home-study-companion') : path.join(app.getPath('appData'), 'home-study-companion')
  status.stage='load_selected_model'
  let model = await new native.ModelSettingsStore(settingsPath).load()
  status.stage='load_selected_voice'
  let voice = await new native.QwenRealtimeSettingsStore(settingsPath).load()
  status.configuration = { modelLoaded: !!model, voiceLoaded: !!voice, voiceEnabled: voice?.enabled === true }
  if ((!model || !voice) && !windows) {
    // The isolated desktop may lack safeStorage access. Recover credentials
    // only for the *exact saved public routing metadata*, from already existing
    // authorized keychain services. Never write settings or switch a model.
    const credential = name => execFileSync('/usr/bin/security',['find-generic-password','-s',name,'-w'],
      {encoding:'utf8',stdio:['ignore','pipe','ignore'],timeout:5000,maxBuffer:8192}).trim()
    if (!model) {
      const saved=JSON.parse(fs.readFileSync(path.join(settingsPath,'model-settings.json'),'utf8'))
      if (saved.provider==='deepseek' && /^https:\/\/api\.deepseek\.com(?:\/v1)?$/.test(saved.baseUrl)) {
        model={provider:saved.provider,baseUrl:saved.baseUrl,model:saved.model,voiceIntentModel:saved.voiceIntentModel||saved.model,
          apiKey:credential('hsc-deepseek-api-key')}
      }
    }
    if (!voice) {
      const saved=JSON.parse(fs.readFileSync(path.join(settingsPath,'qwen-realtime-settings.json'),'utf8'))
      voice={endpoint:saved.endpoint,model:saved.model,voice:saved.voice,enabled:saved.enabled,apiKey:credential('hsc-qwen-realtime-api-key')}
    }
    status.configuration.credentialSource='existing_keychain_with_exact_saved_metadata'
  }
  if (!model || !voice?.enabled) { await finish(2, 'selected_configuration_unavailable'); return }
  status.model = { provider: model.provider, model: model.model, voiceIntentModel: model.voiceIntentModel }
  status.voice = { model: voice.model, voice: voice.voice, endpointConfigured: true }
  status.stage='start_isolated_runtime'
  child = spawn(python, [path.join(repo, 'scripts/public-reply-runtime-probe.py'), root], {
    stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONPATH: path.join(repo, 'services/local_agent/src'), PYTHONUNBUFFERED: '1' } })
  // Private configuration stays in this anonymous process pipe.
  child.stdin.end(JSON.stringify({ model })+'\n')
  child.stderr.on('data', () => { status.runtime_stderr_present = true })
  let pending = ''
  runtime = await new Promise((resolve, reject) => {
    const deadline = setTimeout(() => reject(Error('runtime timeout')), 15000)
    child.stdout.on('data', raw => {
      pending += raw.toString(); let i
      while ((i = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0,i); pending=pending.slice(i+1)
        try { const record = JSON.parse(line); if (record.status === 'ready') { clearTimeout(deadline); resolve(record) } } catch {}
      }
      if (pending.length > 64000) pending = ''
    }); child.once('exit', () => reject(Error('owned runtime exited')))
  })
  const headers = { 'Content-Type': 'application/json', 'X-HSC-Session': runtime.sessionToken }
  const request = async (url, body, method = 'POST') => {
    const response = await fetch(runtime.localApiUrl+url, { method, headers, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(30000) })
    if (!response.ok) throw Error('RUNTIME_'+response.status)
    return response.json()
  }
  for (let i=0;i<100;i++) { try { await request('/v1/health', null, 'GET'); break } catch { if(i===99)throw Error('startup'); await new Promise(r=>setTimeout(r,50)) } }
  // Parent consent is an explicit test preference in this isolated seeded profile.
  status.stage='isolated_parent_consent'
  const setup = await request('/v1/parent/setup', { pin: '2468' })
  headers['X-HSC-Parent'] = setup.parent_token
  await request('/v1/parent/privacy', { cloud_model_text_allowed: true }, 'PUT')
  const session = await request('/v1/sessions', { child_id: 'child-primary-4', client_action_id: 'synthetic-public-start' })
  const sid = session.session_id
  // Text-only canonical capture APIs are used; no microphone or household media.
  let captureCount = 0
  const nextTurn = async utterance => {
  const action = 'synthetic-public-capture-' + (++captureCount)
  status.stage='canonical_capture'
  await request(`/v1/sessions/${sid}/voice/realtime/begin`, { client_action_id: action,
    capture_reference: { child_id: 'child-primary-4' }, model_label: 'synthetic-text-qualification', captured_at_ms: Date.now() })
  const capture = await request(`/v1/sessions/${sid}/voice/realtime/complete`, { client_action_id: action, transcript: utterance })
  status.stage='real_cloud_planner'
  const turn = await request(`/v1/sessions/${sid}/voice/realtime/actions/${action}/resume`, {
    child_id: 'child-primary-4', voice_input_id: capture.voice_input_id })
  status.paid_planner_requests++; turns.push(turn.agent_turn_id)
  await request(`/v1/agent-turns/${turn.agent_turn_id}/execute`)
  save();return turn
  }
  const sourceOf = turn => ({ sessionId: sid, childId: 'child-primary-4', agentTurnId: turn.agent_turn_id })
  const turn = await nextTurn('陪我聊聊花园里的花，请只回答一句简短的话。')
  const source = sourceOf(turn)
  status.runtime_executed = true
  status.stage='public_speech'
  service = new native.PublicReplySpeechService({ config: async () => voice, isCurrent: () => true,
    authority: command => native.executeNativeRealtimeOutput(command, { ...runtime, status: 'ready' }, value => native.signNativeRealtimeOutput('ab'.repeat(32), value)),
    context: async src => {
      const response = await fetch(`${runtime.localApiUrl}/v1/sessions/${src.sessionId}/voice-output-context/${src.agentTurnId}`, { headers, signal: AbortSignal.timeout(5000) })
      if (response.status===409) return { status: 'unavailable' }; if (!response.ok) throw Error('context'); return response.json()
    }, onRetired: id => { retired.push(id); if(win&&!win.isDestroyed())win.webContents.send('voice:public-reply-retired',id) },
    onFailure: code=>{(status.failure_codes||=[]).push(code);save()},
    onTranscriptCheck: check=>{status.transcript_check=check;save()},
    onUsage: usage=>{(status.supplier_usage||=[]).push(usage);save()} })
  ipcMain.handle('probe:open', (_, src) => service.open(src))
  ipcMain.handle('probe:generate', async (_, id) => { status.speech_generation_attempts++; const audio=await service.generate(id);
    status.supplier_real=true; status.output_samples={sampleCount:audio.sampleCount,frameCount:audio.frameCount,voice:audio.voice,model:audio.model};return audio })
  let dropStartedAck = false
  ipcMain.handle('probe:settle', async (_, id, event) => {
    await service.settle(id,event)
    if (dropStartedAck && event === 'started') { dropStartedAck=false;throw Error('SYNTHETIC_ACK_LOSS') }
  })
  ipcMain.handle('probe:cancel', (_, id) => service.cancel(id))
  server = http.createServer((_, response) => { response.setHeader('Content-Type','text/html'); response.end('<html><body>家庭学习伙伴：隔离语音验收</body></html>') })
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve))
  const origin = 'http://127.0.0.1:'+server.address().port
  const preload = path.join(root,'preload.cjs')
  fs.writeFileSync(preload, `const{contextBridge,ipcRenderer}=require('electron');contextBridge.exposeInMainWorld('desktop',{
    openPublicReplySpeech:s=>ipcRenderer.invoke('probe:open',s),generatePublicReplySpeech:id=>ipcRenderer.invoke('probe:generate',id),
    settlePublicReplySpeech:(id,event)=>ipcRenderer.invoke('probe:settle',id,event),cancelPublicReplySpeech:id=>ipcRenderer.invoke('probe:cancel',id),
    onPublicReplySpeechRetired:callback=>{const fn=(_,id)=>callback(id);ipcRenderer.on('voice:public-reply-retired',fn);return()=>ipcRenderer.removeListener('voice:public-reply-retired',fn)},
    getRuntimeConfig:async()=>(${JSON.stringify({status:'ready',localApiUrl:runtime.localApiUrl,sessionToken:runtime.sessionToken})})})`)
  win = new BrowserWindow({show:false,webPreferences:{preload,contextIsolation:true,nodeIntegration:false}})
  await win.loadURL(origin)
  const renderer = fs.readFileSync(path.join(root,'renderer.js'),'utf8')
  const play = async (item, name, cancelOnStart = false) => {
  const result = await win.webContents.executeJavaScript(`${renderer};(async()=>{
    let started=0,ended=0,notDelivered=0,result=null;
    const handled=await publicReply.playPublicReply(${JSON.stringify(sourceOf(item))},${JSON.stringify(item.response_text)},
      {isCurrent:()=>true,onPlaybackEnd:()=>{ended++},onNotDelivered:()=>{notDelivered++}},value=>{result=value},()=>{
        started++;if(${cancelOnStart})setTimeout(()=>publicReply.cancelLocalSpeech('runtime-public-'+${JSON.stringify(item.agent_turn_id)}),30)
      });
    await new Promise(resolve=>setTimeout(resolve,500));return{handled,started,ended,notDelivered,result};})()`)
  status.scenarios.push({ name, original_turn:item.agent_turn_id, ...result });save();return result
  }
  const result = await play(turn,cancelOnly?'cancel_during_playback':'normal',cancelOnly)
  const after = await service.open(source); status.repeat_status=after.status
  status.runtime_database=runtime.databasePath; status.original_turn=turn.agent_turn_id
  if (result.started!==1 || result.ended!==(cancelOnly?0:1) || result.result!==(cancelOnly?'interrupted':'managed') || after.status!=='unconfirmed') throw Error('public delivery not confirmed')
  if (matrix) {
    for (const [name,utterance] of [['consecutive_second','接着陪我聊聊树叶，也请只说一句。'],['short_answer','我们问候一下，请只回复“你好。”三个字。']]) {
      const item=await nextTurn(utterance), observed=await play(item,name)
      if(observed.started!==1||observed.ended!==1||observed.result!=='managed')throw Error('matrix normal failed')
      if((await service.open(sourceOf(item))).status!=='unconfirmed')throw Error('repeat allowed')
    }
    const interrupted=await nextTurn('陪我简短说说夜空里星星的样子。'), cancelled=await play(interrupted,'cancel_during_playback',true)
    if(cancelled.started!==1||cancelled.ended!==0)throw Error('cancel promoted')
    if((await service.open(sourceOf(interrupted))).status!=='unconfirmed')throw Error('cancel replay')
    const lost=await nextTurn('陪我聊聊晴天的白云，请只说一句。');dropStartedAck=true
    const unknown=await play(lost,'persisted_started_ack_lost')
    if(unknown.started!==1||unknown.ended!==0)throw Error('ack loss promoted')
    if((await service.open(sourceOf(lost))).status!=='unconfirmed')throw Error('unknown replay')
    // Strong process kill after reserve, before supplier upload. Reopen only
    // the same owned SQLite; recovery must expose the old attempt, not resend.
    const pendingTurn=await nextTurn('陪我简短说说彩虹。'), pendingSource=sourceOf(pendingTurn)
    const lease=await service.open(pendingSource);if(lease.status!=='eligible')throw Error('reserve missing')
    child.kill('SIGKILL');await new Promise(resolve=>child.once('exit',resolve))
    await service.cancel(lease.leaseId)
    const restarted=spawn(python,[path.join(repo,'scripts/public-reply-runtime-probe.py'),root],{
      stdio:['pipe','pipe','pipe'],env:{...process.env,PYTHONPATH:path.join(repo,'services/local_agent/src'),PYTHONUNBUFFERED:'1'}})
    child=restarted;child.stdin.end(JSON.stringify({model})+'\n');child.stderr.on('data',()=>{status.runtime_stderr_present=true})
    let buffer=''
    runtime=await new Promise((resolve,reject)=>{
      const timeout=setTimeout(()=>reject(Error('restart timeout')),15000)
      child.stdout.on('data',raw=>{buffer+=raw.toString();for(const line of buffer.split('\n').slice(0,-1)){
        try{const value=JSON.parse(line);if(value.status==='ready'){clearTimeout(timeout);resolve(value)}}catch{}}
        buffer=buffer.slice(buffer.lastIndexOf('\n')+1)})
    })
    headers['X-HSC-Session']=runtime.sessionToken
    for(let i=0;i<100;i++){try{await request('/v1/health',null,'GET');break}catch{if(i===99)throw Error('restart unavailable');await new Promise(r=>setTimeout(r,50))}}
    const recovered=await service.open(pendingSource)
    status.scenarios.push({name:'runtime_sigkill_reserved_restart',original_turn:pendingTurn.agent_turn_id,
      recovered:recovered.status,supplier_uploads:0});save()
    if(recovered.status!=='unconfirmed')throw Error('restart replay')
  }
  await finish(0,matrix?'bounded_matrix_current_source':cancelOnly?'bounded_cancel_current_source':'bounded_normal_current_source')
}).catch(error => { status.error_type=error?.name; if (/^[A-Z0-9_]{1,80}$/.test(error?.code||''))status.error_code=error.code; void finish(2,'qualification_failed') })
