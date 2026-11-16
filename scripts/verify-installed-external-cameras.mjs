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
    const timer = setTimeout(() => reject(new Error('Desktop evaluation timed out')), 60_000)
    pending.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({
      id,
      method: 'Runtime.evaluate',
      params: { expression, awaitPromise: true, returnByValue: true },
    }))
  })
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
  }
  return result.result.value
}

const report = await evaluate(`(async()=>{
  const integratedPattern=/(integrated|built[ -]?in|internal|集成|内置)/i
  const wait=(milliseconds)=>new Promise(resolve=>setTimeout(resolve,milliseconds))
  const runtime=await window.desktop.getRuntimeConfig()
  const headers={'X-HSC-Session':runtime.sessionToken}
  const bindingsResponse=await fetch(runtime.localApiUrl+'/v1/device-bindings',{headers,cache:'no-store'})
  const bindings=await bindingsResponse.json()
  const devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='videoinput')
  const observation=devices.find(device=>/^L523(?: |$)/i.test(device.label))
  const documentCamera=devices.find(device=>/^Usb HDCamera(?: |$)/i.test(device.label))
  const inventory=devices.map(device=>({label:device.label,external:!integratedPattern.test(device.label)}))
  if(!observation||!documentCamera){
    return {
      status:'required_external_device_missing',
      inventory,
      bindings_http:bindingsResponse.status,
      bindings,
      raw_media_saved:false,
      media_uploaded:false,
    }
  }

  const ownedStreams=[]
  const ownedVideos=[]
  const sample=async(video)=>{
    const canvas=document.createElement('canvas')
    canvas.width=64
    canvas.height=36
    const context=canvas.getContext('2d',{willReadFrequently:true})
    context.drawImage(video,0,0,canvas.width,canvas.height)
    const pixels=context.getImageData(0,0,canvas.width,canvas.height).data
    let sum=0
    let squareSum=0
    for(let index=0;index<pixels.length;index+=4){
      const luminance=(pixels[index]+pixels[index+1]+pixels[index+2])/3
      sum+=luminance
      squareSum+=luminance*luminance
    }
    const count=pixels.length/4
    const mean=sum/count
    return {mean,variance:(squareSum/count)-(mean*mean),pixels:Array.from(pixels)}
  }
  const inspect=async(role,device,constraints)=>{
    const existing=Array.from(document.querySelectorAll('video')).map(video=>({
      video,
      stream:video.srcObject instanceof MediaStream?video.srcObject:null,
    })).find(candidate=>candidate.stream?.getVideoTracks().some(track=>track.label===device.label))
    let stream=existing?.stream??null
    let video=existing?.video??null
    let source='application_stream'
    if(!stream||!video){
      source='verification_stream'
      try{
        stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:{exact:device.deviceId},...constraints}})
      }catch(error){
        throw new Error(role+'_get_user_media_'+String(error?.name??'Error')+'_'+String(error?.message??error))
      }
      ownedStreams.push(stream)
      video=document.createElement('video')
      video.muted=true
      video.playsInline=true
      video.srcObject=stream
      video.style.cssText='position:fixed;width:1px;height:1px;opacity:0;pointer-events:none'
      document.body.appendChild(video)
      ownedVideos.push(video)
      await video.play()
    }
    for(let attempt=0;attempt<40&&(!video.videoWidth||video.readyState<2);attempt+=1) await wait(100)
    if(!video.videoWidth) throw new Error(role+'_first_frame_timeout')
    const firstTime=video.currentTime
    const first=await sample(video)
    await wait(1800)
    const secondTime=video.currentTime
    const second=await sample(video)
    let absoluteDifference=0
    for(let index=0;index<first.pixels.length;index+=4){
      absoluteDifference+=Math.abs(first.pixels[index]-second.pixels[index])
      absoluteDifference+=Math.abs(first.pixels[index+1]-second.pixels[index+1])
      absoluteDifference+=Math.abs(first.pixels[index+2]-second.pixels[index+2])
    }
    const track=stream.getVideoTracks()[0]
    const settings=track.getSettings()
    return {
      role,
      source,
      requested_label:device.label,
      track_label:track.label,
      integrated_selected:integratedPattern.test(track.label),
      width:settings.width??video.videoWidth,
      height:settings.height??video.videoHeight,
      frame_rate:settings.frameRate??null,
      frame_time_advanced:secondTime>firstTime,
      frame_delta_seconds:Number((secondTime-firstTime).toFixed(3)),
      luminance_mean:Number(second.mean.toFixed(3)),
      luminance_variance:Number(second.variance.toFixed(3)),
      mean_frame_difference:Number((absoluteDifference/(first.pixels.length/4)/3).toFixed(3)),
      ready_state:video.readyState,
    }
  }

  try{
    const results=[]
    results.push(await inspect('camera',observation,{width:{ideal:1280,max:1280},height:{ideal:720,max:720},frameRate:{ideal:10,max:15}}))
    results.push(await inspect('document_camera',documentCamera,{width:{ideal:1280},height:{ideal:960},frameRate:{ideal:5,max:15}}))
    const bindingItems=Array.isArray(bindings)?bindings:bindings.items??bindings.bindings??[]
    const bindingMap=Object.fromEntries(bindingItems.map(binding=>[binding.device_type??binding.device_role,binding]))
    const bindingLabel=(binding)=>binding?.label??binding?.capabilities?.system_enumeration_name??''
    const bindingChecks={
      camera:/^L523(?: |$)/i.test(bindingLabel(bindingMap.camera)),
      document_camera:/^Usb HDCamera(?: |$)/i.test(bindingLabel(bindingMap.document_camera)),
      integrated_absent:!Object.values(bindingMap).some(binding=>integratedPattern.test(bindingLabel(binding))),
    }
    return {
      status:results.every(result=>result.frame_time_advanced&&!result.integrated_selected)&&Object.values(bindingChecks).every(Boolean)?'passed':'failed',
      inventory,
      bindings_http:bindingsResponse.status,
      bindings,
      binding_checks:bindingChecks,
      streams:results,
      raw_media_saved:false,
      media_uploaded:false,
    }
  }catch(error){
    return {
      status:'stream_probe_failed',
      error_name:error?.name??'Error',
      error_message:String(error?.message??error),
      inventory,
      bindings_http:bindingsResponse.status,
      bindings,
      raw_media_saved:false,
      media_uploaded:false,
    }
  }finally{
    for(const stream of ownedStreams) stream.getTracks().forEach(track=>track.stop())
    for(const video of ownedVideos){video.srcObject=null;video.remove()}
  }
})()`)

socket.close()
await writeFile(output, JSON.stringify(report, null, 2))
console.log(JSON.stringify(report))
