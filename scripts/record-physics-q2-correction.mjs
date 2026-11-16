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
  const search=await request('/v1/children/'+${JSON.stringify(childId)}+'/mistake-knowledge-candidates?q='+encodeURIComponent('灯泡 额定电压 额定功率 电阻')+'&subject='+encodeURIComponent('物理'))
  const node=(search.items??[]).find(item=>/(功率|电阻|灯泡|电学)/.test(item.title??''))??search.items?.[0]
  if(!node)throw new Error('NO_PHYSICS_KNOWLEDGE_NODE')
  const captured=await request('/v1/mistake-capture-candidates/'+${JSON.stringify(candidateId)}+'/capture',{
    method:'POST',body:JSON.stringify({
      child_id:${JSON.stringify(childId)},knowledge_node_id:node.knowledge_node_id,subject:'物理',
      title:'额定电压与功率条件下的灯泡连接和电阻',submitted_answer:'B、D',
      client_action_id:'user-correction-capture-'+${JSON.stringify(candidateId)}
    })
  })
  const mistakeId=captured.mistake.mistake_id
  const confirmed=await request('/v1/mistakes/'+mistakeId+'/confirm',{
    method:'POST',body:JSON.stringify({child_id:${JSON.stringify(childId)},decision:'confirm',client_action_id:'user-correction-confirm-'+mistakeId})
  })
  const diagnosed=await request('/v1/mistakes/'+mistakeId+'/diagnose',{
    method:'POST',body:JSON.stringify({
      child_id:${JSON.stringify(childId)},error_type:'unknown',confidence:0.45,
      explanation:'家长更正为孩子实际选了B、D。本题正确项只有B：两灯均正常工作时端电压都为6V；由R=U²/P，甲灯电阻12Ω、乙灯6Ω，所以D错。是否因为功率与电阻关系混淆或多选策略失误，需孩子口述后确认。',
      client_action_id:'user-correction-diagnose-'+mistakeId
    })
  })
  return {node,captured,confirmed,diagnosed}
})()`)
socket.close()
await writeFile(outputPath, JSON.stringify(result, null, 2))
console.log(JSON.stringify({
  knowledge_node_id: result.node.knowledge_node_id,
  mistake_id: result.captured.mistake.mistake_id,
  submitted_answer: result.captured.mistake.submitted_answer,
  candidate_status: result.captured.candidate.status,
  mistake_status: result.diagnosed.status,
  diagnosis_type: result.diagnosed.diagnosis?.error_type,
  diagnosis_confidence: result.diagnosed.diagnosis?.confidence,
}))
