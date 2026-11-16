import { app, safeStorage } from 'electron'
import { spawn } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'

const output = process.argv[2]
const port = Number(process.argv[3] || 9222)
if (!output) throw new Error('An output path is required')

const result = { status: 'blocked', error_code: 'DOCUMENT_VISION_PROBE_FAILED', raw_media_saved: false, original_frame_uploaded: false }

async function captureFrame() {
  let target = null
  for (let attempt = 0; attempt < 60 && !target; attempt += 1) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      target = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl) ?? null
    } catch { /* desktop may still be starting */ }
    if (!target) await new Promise((done) => setTimeout(done, 500))
  }
  if (!target) throw new Error('desktop_page_missing')
  const socket = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((done, fail) => {
    socket.addEventListener('open', done, { once: true })
    socket.addEventListener('error', fail, { once: true })
  })
  const id = 1
  const response = new Promise((done, fail) => {
    const timeout = setTimeout(() => fail(new Error('capture_timeout')), 45_000)
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.id !== id) return
      clearTimeout(timeout)
      message.error ? fail(new Error('capture_failed')) : done(message.result)
    })
  })
  socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { awaitPromise: true, returnByValue: true, expression: `(async()=>{
    const wait=(ms)=>new Promise(done=>setTimeout(done,ms));
    const devices=(await navigator.mediaDevices.enumerateDevices()).filter(item=>item.kind==='videoinput');
    const device=devices.find(item=>/^Usb HDCamera(?: |$)/i.test(item.label));
    if(!device)return {status:'device_missing'};
    const existing=Array.from(document.querySelectorAll('video')).find(video=>video.srcObject instanceof MediaStream&&video.srcObject.getVideoTracks().some(track=>track.label===device.label));
    let video=existing??null;let stream=video?.srcObject??null;let owned=false;
    try{
      if(!stream){owned=true;stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:{exact:device.deviceId},width:{ideal:1280,max:1280},height:{ideal:720,max:720},frameRate:{ideal:5,max:10}}});video=document.createElement('video');video.muted=true;video.playsInline=true;video.srcObject=stream;await video.play();}
      for(let i=0;i<50&&(!video.videoWidth||video.readyState<2);i+=1)await wait(100);
      if(!video.videoWidth)return {status:'first_frame_timeout'};
      await wait(700);const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
      return {status:'captured',track_label:stream.getVideoTracks()[0]?.label??null,width:canvas.width,height:canvas.height,image_base64:canvas.toDataURL('image/jpeg',.94).split(',',2)[1]};
    }finally{if(owned&&stream)stream.getTracks().forEach(track=>track.stop());if(owned&&video){video.srcObject=null;video.remove();}}
  })()` } }))
  const message = await response
  socket.close()
  if (message.exceptionDetails) throw new Error('capture_exception')
  return message.result.value
}

async function main() {
  app.setPath('userData', join(process.env.APPDATA, 'home-study-companion'))
  await app.whenReady()
  const frame = await captureFrame()
  if (frame.status !== 'captured') throw new Error(frame.status)
  const profile = join(process.env.APPDATA, 'home-study-companion')
  const config = JSON.parse(await readFile(join(profile, 'asr-settings.json'), 'utf8'))
  if (!safeStorage.isEncryptionAvailable() || config.provider !== 'dashscope'
      || config.baseUrl !== 'https://dashscope.aliyuncs.com/compatible-mode/v1') throw new Error('credential_unavailable')
  const key = safeStorage.decryptString(Buffer.from(config.encryptedApiKey, 'base64'))
  const child = spawn(resolve('.venv/Scripts/python.exe'), [resolve('scripts/probe_document_vision_frame.py')], {
    cwd: process.cwd(), windowsHide: true,
    env: { ...process.env, PYTHONUTF8: '1', PYTHONPATH: resolve('services/local_agent/src'),
      HSC_ASR_PROVIDER: 'dashscope', HSC_ASR_BASE_URL: config.baseUrl, HSC_ASR_API_KEY: key,
      HSC_DOCUMENT_VISION_ENABLED: '1', HSC_DOCUMENT_VISION_BASE_URL: config.baseUrl,
      HSC_DOCUMENT_VISION_CREDENTIAL_SOURCE: 'asr_same_origin' },
    stdio: ['pipe', 'pipe', 'ignore'],
  })
  child.stdin.end(frame.image_base64)
  let stdout = ''
  child.stdout.on('data', (chunk) => { if (stdout.length < 16_000) stdout += chunk.toString() })
  const exitCode = await new Promise((done, fail) => { child.on('exit', done); child.on('error', fail) })
  const parsed = JSON.parse(stdout.trim())
  await writeFile(output, JSON.stringify({ ...parsed, capture: { track_label: frame.track_label, width: frame.width, height: frame.height } }, null, 2))
  app.exit(exitCode ?? 1)
}

main().catch(async () => {
  await writeFile(output, JSON.stringify(result, null, 2)).catch(() => undefined)
  app.exit(2)
})
