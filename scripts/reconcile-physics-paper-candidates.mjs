import { writeFile } from 'node:fs/promises'

const outputPath = process.argv[2]
const targetArtifactId = process.argv[3]
const childId = process.argv[4] || 'child-junior-3'
const port = Number(process.argv[5] || 9222)
const obsoleteArtifactIds = process.argv.slice(6)

if (!outputPath || !targetArtifactId) {
  throw new Error('Usage: node reconcile-physics-paper-candidates.mjs OUTPUT TARGET CHILD PORT [OBSOLETE...]')
}

const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
const page = targets.find((candidate) => candidate.type === 'page' && candidate.webSocketDebuggerUrl)
if (!page) throw new Error('Desktop page not found')

const socket = new WebSocket(page.webSocketDebuggerUrl)
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once: true })
  socket.addEventListener('error', reject, { once: true })
})

let nextId = 0
const pending = new Map()
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data)
  const request = pending.get(message.id)
  if (!request) return
  pending.delete(message.id)
  message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result)
})

const evaluate = async (expression) => {
  const id = ++nextId
  const result = await new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject })
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

const result = await evaluate(`(async()=>{
  const runtime=await window.desktop.getRuntimeConfig()
  const headers={'Content-Type':'application/json','X-HSC-Session':runtime.sessionToken}
  const request=async(path,init={})=>{
    const response=await fetch(runtime.localApiUrl+path,{...init,headers:{...headers,...(init.headers??{})}})
    const body=await response.json().catch(()=>null)
    if(!response.ok)throw new Error(path+':'+response.status+':'+JSON.stringify(body))
    return body
  }
  const obsolete=${JSON.stringify(obsoleteArtifactIds)}
  const skipped=[]
  for(const artifactId of obsolete){
    const listed=await request('/v1/capture-artifacts/'+artifactId+'/mistake-candidates?child_id='+encodeURIComponent(${JSON.stringify(childId)}))
    for(const item of listed.items??[]){
      if(item.status!=='pending')continue
      const updated=await request('/v1/mistake-capture-candidates/'+item.candidate_id+'/skip',{
        method:'POST',body:JSON.stringify({child_id:${JSON.stringify(childId)},client_action_id:'acceptance-skip-'+item.candidate_id})
      })
      skipped.push({candidate_id:updated.candidate_id,artifact_id:updated.artifact_id,status:updated.status})
    }
  }
  const prepared=await request('/v1/capture-artifacts/'+${JSON.stringify(targetArtifactId)}+'/mistake-candidates/prepare',{
    method:'POST',body:JSON.stringify({child_id:${JSON.stringify(childId)},client_action_id:'acceptance-prepare-'+${JSON.stringify(targetArtifactId)}})
  })
  return {skipped,prepared}
})()`)

socket.close()
await writeFile(outputPath, JSON.stringify(result, null, 2))
console.log(JSON.stringify({
  skipped_count: result.skipped.length,
  prepared_count: result.prepared.items?.length ?? 0,
  prepared: (result.prepared.items ?? []).map((item) => ({
    candidate_id: item.candidate_id,
    question_index: item.question_index,
    status: item.status,
    question_head: item.question_text.slice(0, 80),
  })),
}))
