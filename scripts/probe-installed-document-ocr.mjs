import { spawnSync } from 'node:child_process'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { writeFile } from 'node:fs/promises'

const output = process.argv[2]
const port = Number(process.argv[3] || 9222)
if (!output) throw new Error('An output path is required')

let page = null
for (let attempt = 0; attempt < 60 && !page; attempt += 1) {
  try {
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
    page = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl) ?? null
  } catch {
    // The installed desktop may still be starting.
  }
  if (!page) await new Promise((resolveWait) => setTimeout(resolveWait, 500))
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
await new Promise((resolveOpen, reject) => {
  socket.addEventListener('open', resolveOpen, { once: true })
  socket.addEventListener('error', reject, { once: true })
})

const evaluate = async (expression) => {
  const id = ++nextId
  const result = await new Promise((resolveResult, reject) => {
    const timer = setTimeout(() => reject(new Error('Desktop evaluation timed out')), 45_000)
    pending.set(id, { resolve: resolveResult, reject, timer })
    socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } }))
  })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
  return result.result.value
}

const capture = await evaluate(`(async()=>{
  const wait=(milliseconds)=>new Promise(resolve=>setTimeout(resolve,milliseconds))
  const devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='videoinput')
  const device=devices.find(candidate=>/^Usb HDCamera(?: |$)/i.test(candidate.label))
  const observation=devices.find(candidate=>/^L523(?: |$)/i.test(candidate.label))
  if(!device)return {status:'device_missing',inventory:devices.map(item=>item.label)}
  const existing=Array.from(document.querySelectorAll('video')).map(video=>({video,stream:video.srcObject instanceof MediaStream?video.srcObject:null}))
    .find(candidate=>candidate.stream?.getVideoTracks().some(track=>track.label===device.label))
  let stream=existing?.stream??null
  let video=existing?.video??null
  let owned=false
  let warmup=null
  let warmupVideo=null
  try{
    if(!stream||!video){
      owned=true
      if(observation){
        try{warmup=await navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:{exact:observation.deviceId},width:{ideal:1280,max:1280},height:{ideal:720,max:720},frameRate:{ideal:10,max:15}}})}
        catch(error){return {status:'capture_failed',stage:'observation_get_user_media',error_name:error?.name??'Error',error_message:String(error?.message??error)}}
        warmupVideo=document.createElement('video');warmupVideo.muted=true;warmupVideo.playsInline=true;warmupVideo.srcObject=warmup
        warmupVideo.style.cssText='position:fixed;width:1px;height:1px;opacity:0;pointer-events:none';document.body.appendChild(warmupVideo)
        try{await warmupVideo.play()}catch(error){return {status:'capture_failed',stage:'observation_play',error_name:error?.name??'Error',error_message:String(error?.message??error)}}
        for(let attempt=0;attempt<40&&(!warmupVideo.videoWidth||warmupVideo.readyState<2);attempt+=1)await wait(100)
        await wait(2000)
      }
      try{stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:{exact:device.deviceId},width:{exact:4160},height:{exact:3120},resizeMode:{ideal:'none'},frameRate:{ideal:5,max:10}}})}
      catch(error){return {status:'capture_failed',stage:'document_get_user_media',error_name:error?.name??'Error',error_message:String(error?.message??error)}}
      video=document.createElement('video');video.muted=true;video.playsInline=true;video.srcObject=stream
      video.style.cssText='position:fixed;width:1px;height:1px;opacity:0;pointer-events:none';document.body.appendChild(video)
      try{await video.play()}catch(error){return {status:'capture_failed',stage:'document_play',error_name:error?.name??'Error',error_message:String(error?.message??error)}}
    }
    for(let attempt=0;attempt<50&&(!video.videoWidth||video.readyState<2);attempt+=1)await wait(100)
    if(!video.videoWidth)return {status:'first_frame_timeout',track_label:stream.getVideoTracks()[0]?.label??null}
    const track=stream.getVideoTracks()[0]
    const beforeSettings=track?.getSettings?.()??{}
    const capabilities=track?.getCapabilities?.()??{}
    const resolutionUpgrade={attempted:owned,applied:video.videoWidth===4160&&video.videoHeight===3120,error:null}
    await wait(700)
    const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight
    canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height)
    return {status:'captured',source:owned?'verification_stream':'application_stream',track_label:stream.getVideoTracks()[0]?.label??null,
      width:canvas.width,height:canvas.height,before_settings:beforeSettings,after_settings:track?.getSettings?.()??{},
      capabilities:{width:capabilities.width??null,height:capabilities.height??null,frameRate:capabilities.frameRate??null,focusMode:capabilities.focusMode??null},
      resolution_upgrade:resolutionUpgrade,image_base64:canvas.toDataURL('image/jpeg',0.94).split(',',2)[1]}
  }finally{
    if(owned&&stream)stream.getTracks().forEach(track=>track.stop())
    if(warmup)warmup.getTracks().forEach(track=>track.stop())
    if(warmupVideo){warmupVideo.srcObject=null;warmupVideo.remove()}
    if(owned&&video){video.srcObject=null;video.remove()}
  }
})()`)
socket.close()

let report
if (capture.status !== 'captured') {
  report = { ...capture, raw_media_saved: false, media_uploaded: false }
} else {
  const scriptDirectory = dirname(fileURLToPath(import.meta.url))
  const repository = resolve(scriptDirectory, '..')
  const python = join(repository, '.venv', 'Scripts', 'python.exe')
  const helper = join(scriptDirectory, 'probe_document_ocr_frame.py')
  const child = spawnSync(python, [helper], {
    input: capture.image_base64,
    encoding: 'utf8',
    env: { ...process.env, PYTHONUTF8: '1' },
    maxBuffer: 8 * 1024 * 1024,
    timeout: 180_000,
  })
  if (child.status !== 0) {
    report = { status: 'ocr_failed', error: (child.stderr || child.stdout || '').slice(-1200), raw_media_saved: false, media_uploaded: false }
  } else {
    report = {
      status: 'passed',
      capture: { source: capture.source, track_label: capture.track_label, width: capture.width, height: capture.height,
        before_settings: capture.before_settings, after_settings: capture.after_settings, capabilities: capture.capabilities,
        resolution_upgrade: capture.resolution_upgrade },
      ocr: JSON.parse(child.stdout.trim()),
      raw_media_saved: false,
      media_uploaded: false,
    }
  }
}

await writeFile(output, JSON.stringify(report, null, 2))
console.log(JSON.stringify(report))
