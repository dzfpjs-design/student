import { writeFile } from 'node:fs/promises'

const output = process.argv[2]
const port = Number(process.argv[3] || 9234)
if (!output) throw new Error('An output path is required')

let page = null
for (let attempt = 0; attempt < 80 && !page; attempt += 1) {
  try {
    const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
    page = pages.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl) ?? null
  } catch {
    // The packaged desktop may still be starting its local Agent.
  }
  if (!page) await new Promise((resolve) => setTimeout(resolve, 500))
}
if (!page) throw new Error('Installed desktop page not found')

const socket = new WebSocket(page.webSocketDebuggerUrl)
let nextId = 0
const pending = new Map()
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)
  const request = pending.get(message.id)
  if (!request) return
  clearTimeout(request.timer)
  pending.delete(message.id)
  message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result)
})
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})

async function evaluate(expression, timeoutMs = 240_000) {
  const id = ++nextId
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Desktop capability probe timed out')), timeoutMs)
    pending.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } }))
  })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
  return result.result.value
}

const report = {
  at: new Date().toISOString(),
  source: 'installed_desktop_isolated_data',
  synthetic_inputs_only: true,
  production_child_records_used: false,
  raw_media_saved: false,
  media_uploaded: false,
}

try {
  report.result = await evaluate(`(async()=>{
    const runtime=await window.desktop.getRuntimeConfig()
    if(runtime.status!=='ready')throw new Error('local_agent_not_ready')
    const qwen=await window.desktop.getQwenRealtimeSettings()
    if(!qwen.configured||!qwen.enabled)throw new Error('qwen_realtime_not_configured')
    const qwenTest=await window.desktop.testQwenRealtimeSettings({...qwen,apiKey:''})
    const headers={'X-HSC-Session':runtime.sessionToken,'Content-Type':'application/json'}
    const request=async(path,options={})=>{
      const response=await fetch(runtime.localApiUrl+path,{...options,headers:{...headers,...(options.headers||{})}})
      const type=response.headers.get('content-type')||''
      const body=type.includes('application/json')?await response.json():await response.arrayBuffer()
      if(!response.ok)throw new Error(path+': '+response.status+' '+(body?.detail?.code||body?.detail||''))
      return {response,body}
    }
    const bootstrap=(await request('/v1/bootstrap')).body
    const child=bootstrap.children.find(item=>item.child_id==='child-junior-3')??bootstrap.children[0]
    if(!child)throw new Error('seed_child_missing')
    const sessionResult=await request('/v1/sessions',{method:'POST',body:JSON.stringify({child_id:child.child_id,client_action_id:'installed-cloud-session-20260926'})})
    const session=sessionResult.body.session??sessionResult.body
    const sessionId=session.session_id??sessionResult.body.session_id
    if(!sessionId)throw new Error('session_id_missing')
    const tutor=(await request('/v1/sessions/'+encodeURIComponent(sessionId)+'/tutor',{method:'POST',body:JSON.stringify({
      subject:'物理',question:'这是隔离验收题：请只用一层提示说明为什么月球会围绕地球运动，并问一个检查理解的问题。',
      hint_level:1,client_action_id:'installed-cloud-tutor-20260926'
    })})).body
    if(!tutor.turn_id||!tutor.answer)throw new Error('tutor_result_incomplete')
    const ttsResponse=await fetch(runtime.localApiUrl+'/v1/sessions/'+encodeURIComponent(sessionId)+'/voice/synthesize/stream',{
      method:'POST',headers,body:JSON.stringify({text:tutor.answer,client_action_id:'installed-cloud-tts-20260926'})
    })
    if(!ttsResponse.ok)throw new Error('tts_http_'+ttsResponse.status+':'+await ttsResponse.text())
    const audio=new Uint8Array(await ttsResponse.arrayBuffer())
    let offset=0,frames=0,sampleRate=null,pcmBytes=0
    const view=new DataView(audio.buffer,audio.byteOffset,audio.byteLength)
    while(audio.length-offset>=8){
      const rate=view.getUint32(offset,true),size=view.getUint32(offset+4,true)
      if(!rate||size<=0||size%2||offset+8+size>audio.length)throw new Error('invalid_tts_frame')
      if(sampleRate!==null&&sampleRate!==rate)throw new Error('mixed_tts_sample_rates')
      sampleRate=rate;frames+=1;pcmBytes+=size;offset+=8+size
    }
    if(offset!==audio.length||!frames||!pcmBytes)throw new Error('incomplete_tts_stream')
    const persisted=(await request('/v1/sessions/'+encodeURIComponent(sessionId))).body
    return {
      runtime:{status:runtime.status,health:true},
      qwen_realtime:{configured:qwen.configured,enabled:qwen.enabled,model:qwen.model,test:qwenTest},
      tutor:{turn_id:tutor.turn_id,provider:tutor.provider,model:tutor.model,hint_level:tutor.hint_level,answer_chars:tutor.answer.length,blackboard_blocks:tutor.blackboard?.blocks?.length??0},
      tts:{protocol:ttsResponse.headers.get('x-hsc-tts-protocol'),frames,sample_rate:sampleRate,pcm_bytes:pcmBytes},
      persistence:{session_id:sessionId,child_id:child.child_id,state:persisted.session?.state??persisted.state??null}
    }
  })()`)
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'
  report.error = error instanceof Error ? error.message : String(error)
  process.exitCode = 1
} finally {
  socket.close()
  await writeFile(output, JSON.stringify(report, null, 2))
  console.log(JSON.stringify(report))
}
