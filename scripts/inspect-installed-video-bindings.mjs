import { writeFile } from 'node:fs/promises'

const output = process.argv[2]
const port = Number(process.argv[3] || 9222)
if (!output) throw new Error('An output path is required')

let page = null
for (let attempt = 0; attempt < 60 && !page; attempt += 1) {
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  page = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl) ?? null
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

async function evaluate(expression) {
  const id = ++nextId
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Desktop evaluation timed out')), 30_000)
    pending.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } }))
  })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
  return result.result.value
}

const report = await evaluate(`(async()=>{
  const runtime=await window.desktop.getRuntimeConfig()
  const permission=await navigator.mediaDevices.getUserMedia({audio:false,video:true})
  permission.getTracks().forEach(track=>track.stop())
  const devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='videoinput')
  const hash=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))),byte=>byte.toString(16).padStart(2,'0')).join('')
  const inventory=[]
  for(const device of devices) inventory.push({label:device.label,system_id_hash:await hash(device.deviceId)})
  const headers={'X-HSC-Session':runtime.sessionToken}
  const bindingsResponse=await fetch(runtime.localApiUrl+'/v1/device-bindings',{headers,cache:'no-store'})
  const parentStatusResponse=await fetch(runtime.localApiUrl+'/v1/parent/status',{headers,cache:'no-store'})
  const activeVideos=Array.from(document.querySelectorAll('video')).map(video=>{
    const stream=video.srcObject instanceof MediaStream?video.srcObject:null
    const track=stream?.getVideoTracks?.()[0]??null
    const settings=track?.getSettings?.()??{}
    return {aria_label:video.getAttribute('aria-label'),track_label:track?.label??null,track_state:track?.readyState??null,
      width:video.videoWidth,height:video.videoHeight,ready_state:video.readyState,media_time:video.currentTime,
      settings:{width:settings.width??null,height:settings.height??null,frame_rate:settings.frameRate??null,focus_mode:settings.focusMode??null}}
  }).filter(item=>item.track_label)
  return {
    runtime_status:runtime.status,
    devices:inventory,
    active_videos:activeVideos,
    bindings_http:bindingsResponse.status,
    bindings:await bindingsResponse.json(),
    parent_status_http:parentStatusResponse.status,
    parent_status:await parentStatusResponse.json(),
    raw_media_saved:false,
    media_uploaded:false,
  }
})()`)

socket.close()
await writeFile(output, JSON.stringify(report, null, 2))
console.log(JSON.stringify(report))
