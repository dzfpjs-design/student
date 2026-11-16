import { writeFile } from 'node:fs/promises'

const outputPath = process.argv[2]
const candidateId = process.argv[3]
const childId = process.argv[4] || 'child-junior-3'
const port = Number(process.argv[5] || 9222)
if (!outputPath || !candidateId) throw new Error('Output path and candidate ID are required')

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
    socket.send(JSON.stringify({id,method:'Runtime.evaluate',params:{expression,awaitPromise:true,returnByValue:true}}))
  })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text)
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
  const search=await request('/v1/children/'+${JSON.stringify(childId)}+'/mistake-knowledge-candidates?q='+encodeURIComponent('电流热效应 焦耳定律 电热')+'&subject='+encodeURIComponent('物理'))
  const node=(search.items??[]).find(item=>/(电热|焦耳|热效应)/.test(item.title??''))??search.items?.[0]
  if(!node)throw new Error('NO_PHYSICS_KNOWLEDGE_NODE')
  const captured=await request('/v1/mistake-capture-candidates/'+${JSON.stringify(candidateId)}+'/capture',{
    method:'POST',body:JSON.stringify({
      child_id:${JSON.stringify(childId)},knowledge_node_id:node.knowledge_node_id,subject:'物理',
      title:'电流热效应实验的变量与内能改变',submitted_answer:'D',
      client_action_id:'acceptance-capture-'+${JSON.stringify(candidateId)}
    })
  })
  const mistakeId=captured.mistake.mistake_id
  const confirmed=await request('/v1/mistakes/'+mistakeId+'/confirm',{
    method:'POST',body:JSON.stringify({child_id:${JSON.stringify(childId)},decision:'confirm',client_action_id:'acceptance-confirm-'+mistakeId})
  })
  const diagnosed=await request('/v1/mistakes/'+mistakeId+'/diagnose',{
    method:'POST',body:JSON.stringify({
      child_id:${JSON.stringify(childId)},error_type:'unknown',confidence:0.45,
      explanation:'当前只有题面和作答D的证据。正确错误项是B：容器中气体从电阻丝吸热，内能通过热传递增大。是否混淆做功与热传递，需要孩子口述后再确认，不把候选原因当作事实。',
      client_action_id:'acceptance-diagnose-'+mistakeId
    })
  })
  return {node,captured,confirmed,diagnosed}
})()`)
socket.close()
await writeFile(outputPath, JSON.stringify(result, null, 2))
console.log(JSON.stringify({
  knowledge_node_id: result.node.knowledge_node_id,
  mistake_id: result.captured.mistake.mistake_id,
  candidate_status: result.captured.candidate.status,
  mistake_status: result.diagnosed.status,
  diagnosis_type: result.diagnosed.diagnosis?.error_type,
  diagnosis_confidence: result.diagnosed.diagnosis?.confidence,
}))
