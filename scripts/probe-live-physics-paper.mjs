import { writeFile } from 'node:fs/promises'

const outputPath = process.argv[2]
const targetChildId = process.argv[3] || 'child-junior-3'
const port = Number(process.argv[4] || 9222)
const prepareStudy = process.argv.includes('--prepare-study')
const captureOnly = process.argv.includes('--capture-only')
const diagnosticFrameArgument = process.argv.find((value) => value.startsWith('--diagnostic-frame='))
const diagnosticFramePath = diagnosticFrameArgument?.slice('--diagnostic-frame='.length) || null

if (!outputPath) throw new Error('An output path is required')

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))

async function connect() {
  let page = null
  for (let attempt = 0; attempt < 80 && !page; attempt += 1) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
      page = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl) ?? null
    } catch {
      // The installed desktop may be reloading after a profile handoff.
    }
    if (!page) await wait(500)
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

  const evaluate = async (expression, timeoutMs = 240_000) => {
    const id = ++nextId
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Desktop evaluation timed out')), timeoutMs)
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

  return { socket, evaluate }
}

let connection = await connect()
const bindingPreparation = await connection.evaluate(`(async()=>{
  const runtime=await window.desktop.getRuntimeConfig()
  const headers={'Content-Type':'application/json','X-HSC-Session':runtime.sessionToken}
  const bindingsResponse=await fetch(runtime.localApiUrl+'/v1/device-bindings',{headers,cache:'no-store'})
  const bindingsBody=await bindingsResponse.json()
  if(!bindingsResponse.ok)throw new Error('DEVICE_BINDINGS_READ_FAILED:'+JSON.stringify(bindingsBody))
  const current=Array.isArray(bindingsBody.items)?bindingsBody.items:[]
  if(current.some(item=>item.device_type==='camera')&&current.some(item=>item.device_type==='document_camera')){
    return {changed:false,labels:current.map(item=>({device_type:item.device_type,label:item.label}))}
  }
  const devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='videoinput')
  const observer=devices.find(device=>/^L523(?: |$)/i.test(device.label))
  const documentCamera=devices.find(device=>/^Usb HDCamera(?: |$)/i.test(device.label))
  if(!observer||!documentCamera){
    throw new Error('REQUIRED_EXTERNAL_CAMERAS_NOT_ENUMERATED:'+JSON.stringify(devices.map(device=>device.label)))
  }
  const hash=async deviceId=>{
    const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(deviceId))
    return Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,'0')).join('')
  }
  const parentResponse=await fetch(runtime.localApiUrl+'/v1/parent/setup',{
    method:'POST',headers,body:JSON.stringify({pin:'2468'})
  })
  const parent=await parentResponse.json()
  if(!parentResponse.ok)throw new Error('PARENT_SETUP_FAILED:'+JSON.stringify(parent))
  const verifiedAt=new Date().toISOString()
  const payload={
    client_action_id:crypto.randomUUID(),
    bindings:[
      {device_type:'camera',system_id_hash:await hash(observer.deviceId),label:observer.label,verified_at:verifiedAt,
        capabilities:{system_enumeration_name:observer.label,identity_version:'browser_device_id_v2',transport:'browser-media-device',external_only:true,role_verified:'observation_camera',raw_device_id_stored:false}},
      {device_type:'document_camera',system_id_hash:await hash(documentCamera.deviceId),label:documentCamera.label,verified_at:verifiedAt,
        capabilities:{system_enumeration_name:documentCamera.label,identity_version:'browser_device_id_v2',transport:'browser-media-device',external_only:true,role_verified:'document_capture',width:4160,height:3120,frame_rate:5,focus_mode:'continuous',autofocus:true,raw_device_id_stored:false}}
    ]
  }
  const saveResponse=await fetch(runtime.localApiUrl+'/v1/parent/device-bindings',{
    method:'PUT',headers:{...headers,'X-HSC-Parent':parent.parent_token},body:JSON.stringify(payload)
  })
  const saved=await saveResponse.json()
  if(!saveResponse.ok)throw new Error('DEVICE_BINDINGS_SAVE_FAILED:'+JSON.stringify(saved))
  setTimeout(()=>location.reload(),25)
  return {changed:true,labels:(saved.items??[]).map(item=>({device_type:item.device_type,label:item.label}))}
})()`)
if (bindingPreparation.changed) {
  connection.socket.close()
  await wait(3_000)
  connection = await connect()
}
let profile = await connection.evaluate(`(async()=>{
  const runtime=await window.desktop.getRuntimeConfig()
  const headers={'Content-Type':'application/json','X-HSC-Session':runtime.sessionToken}
  const response=await fetch(runtime.localApiUrl+'/v1/bootstrap',{headers,cache:'no-store'})
  return {runtime_status:runtime.status,bootstrap:await response.json()}
})()`)

if (profile.bootstrap?.active_session?.child_id !== targetChildId) {
  const oldSessionId = profile.bootstrap?.active_session?.session_id
  const switched = await connection.evaluate(`(async()=>{
    const runtime=await window.desktop.getRuntimeConfig()
    const headers={'Content-Type':'application/json','X-HSC-Session':runtime.sessionToken}
    const response=await fetch(runtime.localApiUrl+${oldSessionId
      ? JSON.stringify(`/v1/sessions/${oldSessionId}/switch-child`)
      : JSON.stringify('/v1/sessions')},{
      method:'POST',headers:{'Content-Type':'application/json','X-HSC-Session':runtime.sessionToken},
      body:JSON.stringify(${oldSessionId
        ? `{target_child_id:${JSON.stringify(targetChildId)},client_action_id:crypto.randomUUID()}`
        : `{child_id:${JSON.stringify(targetChildId)},client_action_id:crypto.randomUUID()}`})
    })
    const body=await response.json()
    if(!response.ok)throw new Error(JSON.stringify(body))
    setTimeout(()=>location.reload(),25)
    return body
  })()`)
  connection.socket.close()
  await wait(2_500)
  connection = await connect()
  profile = { switched, ...(await connection.evaluate(`(async()=>{
    const runtime=await window.desktop.getRuntimeConfig()
    const response=await fetch(runtime.localApiUrl+'/v1/bootstrap',{headers:{'X-HSC-Session':runtime.sessionToken},cache:'no-store'})
    return {runtime_status:runtime.status,bootstrap:await response.json()}
  })()`)) }
}

const result = await connection.evaluate(`(async()=>{
  const wait=(milliseconds)=>new Promise(resolve=>setTimeout(resolve,milliseconds))
  const runtime=await window.desktop.getRuntimeConfig()
  const headers={'Content-Type':'application/json','X-HSC-Session':runtime.sessionToken}
  const bootstrapResponse=await fetch(runtime.localApiUrl+'/v1/bootstrap',{headers,cache:'no-store'})
  const bootstrap=await bootstrapResponse.json()
  const healthResponse=await fetch(runtime.localApiUrl+'/v1/health',{headers,cache:'no-store'})
  const runtimeHealth=await healthResponse.json()
  if(!healthResponse.ok)throw new Error('HEALTH_FAILED:'+JSON.stringify(runtimeHealth))
  let session=bootstrap.active_session
  if(!session||session.child_id!==${JSON.stringify(targetChildId)})throw new Error('TARGET_CHILD_SESSION_NOT_ACTIVE')
  const request=async(path,init={})=>{
    const response=await fetch(runtime.localApiUrl+path,{...init,headers:{...headers,...(init.headers??{})}})
    const body=await response.json().catch(()=>null)
    if(!response.ok)throw new Error(path+':'+response.status+':'+JSON.stringify(body))
    return body
  }
  const action=()=>crypto.randomUUID()
  const transitions=[]
  if(${prepareStudy ? 'true' : 'false'}&&session.state!=='study'){
    if(session.state==='check_in'){
      session=await request('/v1/sessions/'+session.session_id+'/transition',{method:'POST',body:JSON.stringify({expected_state:'check_in',target_state:'classroom_recall',reason:'physical_paper_acceptance_test',client_action_id:action()})})
      transitions.push('check_in->classroom_recall')
    }
    if(session.state==='classroom_recall'){
      await request('/v1/sessions/'+session.session_id+'/recalls/none',{method:'POST',body:JSON.stringify({client_action_id:action()})})
      session=await request('/v1/sessions/'+session.session_id+'/transition',{method:'POST',body:JSON.stringify({expected_state:'classroom_recall',target_state:'homework_capture',reason:'classroom_recall_not_part_of_device_test',client_action_id:action()})})
      transitions.push('classroom_recall->homework_capture')
    }
    if(session.state==='homework_capture'){
      await request('/v1/sessions/'+session.session_id+'/homework',{method:'POST',body:JSON.stringify({subject:'物理',title:'物理试卷三题检查',expected_minutes:20,task_type:'test_prep',range_text:'桌面试卷上已作答的3道题',special_requirements:'高拍仪识别题目、作答和批改痕迹；未核对前不写入掌握度',source_artifact_id:null,ocr_confidence:null,client_action_id:action()})})
      const workspace=await request('/v1/sessions/'+session.session_id+'/plan/generate',{method:'POST',body:JSON.stringify({client_action_id:action()})})
      session=workspace.session
      transitions.push('homework_capture->plan_review')
    }
    if(session.state==='plan_review'){
      session=await request('/v1/sessions/'+session.session_id+'/transition',{method:'POST',body:JSON.stringify({expected_state:'plan_review',target_state:'study',reason:'physical_paper_acceptance_test_confirmed',client_action_id:action()})})
      transitions.push('plan_review->study')
    }
    if(session.state!=='study')throw new Error('STUDY_STATE_NOT_REACHED:'+session.state)
  }

  let video=null
  let track=null
  for(let attempt=0;attempt<120;attempt+=1){
    video=Array.from(document.querySelectorAll('video')).find(candidate=>{
      const stream=candidate.srcObject instanceof MediaStream?candidate.srcObject:null
      return stream?.getVideoTracks().some(item=>/^Usb HDCamera(?: |$)/i.test(item.label))
    })??null
    track=video?.srcObject instanceof MediaStream?video.srcObject.getVideoTracks()[0]??null:null
    if(video&&track?.readyState==='live'&&video.readyState>=2&&video.videoWidth>=320)break
    await wait(250)
  }
  if(!video||!track||track.readyState!=='live'||!video.videoWidth)throw new Error('DOCUMENT_CAMERA_FRAME_NOT_READY')

  const firstTime=video.currentTime
  await wait(900)
  if(video.currentTime===firstTime)throw new Error('DOCUMENT_CAMERA_FRAME_NOT_ADVANCING')
  const canvas=document.createElement('canvas')
  canvas.width=video.videoWidth;canvas.height=video.videoHeight
  const context=canvas.getContext('2d')
  if(!context)throw new Error('CANVAS_UNAVAILABLE')
  context.drawImage(video,0,0,canvas.width,canvas.height)
  const imageBase64=canvas.toDataURL('image/jpeg',0.94).split(',',2)[1]
  const capture={track_label:track.label,width:canvas.width,height:canvas.height,frame_rate:track.getSettings?.().frameRate??null,focus_mode:track.getSettings?.().focusMode??null,frame_advanced:true}
  if(${captureOnly ? 'true' : 'false'})return {status:'frame_captured',runtime_health:runtimeHealth,child:{child_id:session.child_id,state:session.state,session_id:session.session_id,transitions},capture,_diagnostic_image_base64:imageBase64}
  const actionId=crypto.randomUUID()
  const ocrResponse=await fetch(runtime.localApiUrl+'/v1/sessions/'+session.session_id+'/ocr',{
    method:'POST',headers,body:JSON.stringify({image_base64:imageBase64,client_action_id:actionId})
  })
  const artifact=await ocrResponse.json()
  if(!ocrResponse.ok)throw new Error('OCR_FAILED:'+JSON.stringify(artifact))
  const candidatesResponse=await fetch(runtime.localApiUrl+'/v1/capture-artifacts/'+artifact.artifact_id+'/mistake-candidates/prepare',{
    method:'POST',headers,body:JSON.stringify({child_id:session.child_id,client_action_id:crypto.randomUUID()})
  })
  const candidateBody=await candidatesResponse.json()
  const details=artifact.details??{}
  const pipeline=details.local_pipeline??{}
  const quality=pipeline.quality??details.quality??{}
  const cloud=details.cloud_vision_fallback??null
  return {
    status:candidatesResponse.ok?'captured_and_segmented':'captured_candidate_review_blocked',
    isolated_device_binding:${JSON.stringify(bindingPreparation)},
    runtime_health:runtimeHealth,
    child:{child_id:session.child_id,state:session.state,session_id:session.session_id,transitions},
    capture,
    artifact:{artifact_id:artifact.artifact_id,status:artifact.status,confidence:artifact.confidence,text_length:(artifact.extracted_text??'').length,extracted_text:artifact.extracted_text??'',
      engine:pipeline.engine??details.engine??null,orientation_degrees:quality.transforms?.orientation_degrees??null,
      mirror_horizontal:quality.transforms?.mirror_horizontal??null,
      orientation_ambiguous:quality.orientation_evidence?.ambiguous??null,quality_issues:quality.issues??[],
      question_blocks:pipeline.question_blocks??[],wrong_question_candidates:pipeline.wrong_question_candidates??[],cloud_vision_fallback:cloud},
    candidates:candidatesResponse.ok?(candidateBody.items??[]):[],
    candidate_error:candidatesResponse.ok?null:candidateBody,
    raw_media_saved:Boolean(artifact.local_path),
    original_frame_uploaded:Boolean(cloud?.privacy?.original_frame_uploaded),
    derived_page_uploaded:cloud?.privacy?.uploaded==='page_region_binary_ink',
    derived_page_transmission_attempted:Boolean(cloud?.privacy?.transmission_attempted),
    derived_page_provider_response_received:Boolean(cloud?.privacy?.provider_response_received),
  }
})()`, 300_000)

connection.socket.close()
if (diagnosticFramePath && result._diagnostic_image_base64) {
  await writeFile(diagnosticFramePath, Buffer.from(result._diagnostic_image_base64, 'base64'))
}
delete result._diagnostic_image_base64
await writeFile(outputPath, JSON.stringify(result, null, 2))
console.log(JSON.stringify({
  status: result.status,
  child: result.child,
  capture: result.capture,
  artifact_id: result.artifact?.artifact_id,
  text_length: result.artifact?.text_length,
  orientation_degrees: result.artifact?.orientation_degrees,
  mirror_horizontal: result.artifact?.mirror_horizontal,
  orientation_ambiguous: result.artifact?.orientation_ambiguous,
  candidate_count: result.candidates?.length ?? 0,
  raw_media_saved: result.raw_media_saved,
  original_frame_uploaded: result.original_frame_uploaded,
  derived_page_uploaded: result.derived_page_uploaded,
  derived_page_transmission_attempted: result.derived_page_transmission_attempted,
  derived_page_provider_response_received: result.derived_page_provider_response_received,
}))
