import { createServer, type ServerResponse } from 'node:http'
import { timingSafeEqual } from 'node:crypto'
import { ControlledGameProcessController } from './controlled-game'
import { GameProcessJournal } from './game-process-journal'
import { executableIdentity, queryProcessIdentity, sameProcessIdentity, stopPinnedWindowsProcess, type ProcessIdentity } from './game-process-identity'

async function main(directory:string,sessionId:string):Promise<void> {
  const journal=new GameProcessJournal(directory)
  let record=journal.read(sessionId)
  if(!record||record.state!=='reserved'||!record.token)throw Error('INVALID_RESERVED_GAME')
  const original=record,token=record.token,watchers=new Set<ServerResponse>()
  let stop:Promise<boolean>|null=null,rootIdentity:ProcessIdentity|null=null,windowsTreeVerified=false,shutdown:ReturnType<typeof setTimeout>|undefined,deadlineTimer:ReturnType<typeof setTimeout>|undefined,observedExitReason:string|null=null
  const closed=():boolean=>record?.state==='closed'
  const finish=(reason:string):void=>{
    if(!record||record.state==='closed')return
    if(process.platform==='win32'&&reason!=='launch_failed'&&!windowsTreeVerified)return
    const safeReason=['child_requested','renderer_reloaded','app_quit','process_exited','time_limit','launch_failed','launch_cancelled','replaced','parent_requested','privacy_pause'].includes(reason)?reason:'closed'
    record=journal.update(record,{state:'closed',token:null,exitProof:reason==='launch_failed'?'launch_failed':process.platform==='win32'?'windows_tree_exit':'actual_exit',reason:safeReason})
    if(deadlineTimer)clearTimeout(deadlineTimer)
    for(const response of watchers)response.end(JSON.stringify(record)+'\n')
    watchers.clear()
    shutdown=setTimeout(()=>server.close(()=>process.exit(0)),250)
    shutdown.unref()
  }
  const controller=new ControlledGameProcessController((reason)=>{
    if(process.platform!=='win32'||reason==='launch_failed'||windowsTreeVerified)observedExitReason=reason
    try{finish(reason)}catch{/* The lease remains unresolved on a failed proof write. */}
  }, {
    windowsStop:async pid=>{
      if(!rootIdentity||rootIdentity.pid!==pid)return false
      windowsTreeVerified=await stopPinnedWindowsProcess(rootIdentity)
      return windowsTreeVerified
    },
  })
  const server=createServer(async(req,res)=>{
    try{
    const supplied=req.headers.authorization?.slice(7)??''
    const suppliedBytes=Buffer.from(supplied),tokenBytes=Buffer.from(token)
    if(!req.headers.authorization?.startsWith('Bearer ')||suppliedBytes.byteLength!==tokenBytes.byteLength||!timingSafeEqual(suppliedBytes,tokenBytes)){res.writeHead(403).end();return}
    if(req.method==='GET'&&req.url==='/status'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify(record));return}
    if(req.method==='GET'&&req.url==='/watch'){
      if(record?.state==='closed'){res.end(JSON.stringify(record)+'\n');return}
      res.writeHead(200,{'Content-Type':'application/x-ndjson','Cache-Control':'no-store'});res.flushHeaders();watchers.add(res);res.on('close',()=>watchers.delete(res));return
    }
    if(req.method!=='POST'||req.url!=='/close'){res.writeHead(404).end();return}
    let body='';for await(const chunk of req){body+=chunk;if(body.length>4096){res.writeHead(413).end();return}}
      const input=JSON.parse(body) as {sessionId?:string;instanceId?:string;reason?:string}
      if(input.sessionId!==sessionId||input.instanceId!==original.instanceId||typeof input.reason!=='string'){res.writeHead(409).end();return}
      const persisted=journal.read(sessionId)
      if(!persisted||persisted.instanceId!==record?.instanceId||persisted.revision!==record.revision||persisted.state!==record.state){res.writeHead(409).end();return}
      if(rootIdentity&&(!persisted.process||!sameProcessIdentity(rootIdentity,persisted.process))){res.writeHead(409).end();return}
      if(record?.state==='closed'){res.end(JSON.stringify(record));return}
      if(observedExitReason!==null){finish(observedExitReason);res.end(JSON.stringify(record));return}
      // No PID-only signals. The supervisor still owns this original child;
      // additionally reject changed OS birth/image before requesting its exit.
      if(!rootIdentity){res.writeHead(409).end();return}
      const actual=await queryProcessIdentity(rootIdentity.pid)
      if(!actual||!sameProcessIdentity(actual,rootIdentity)){res.writeHead(409).end();return}
      if(!stop)stop=controller.close(input.reason,sessionId).then(result=>result.closed).finally(()=>{stop=null})
      const exited=await stop
      if(exited&&!closed())finish(input.reason)
      if(!closed()){res.writeHead(409).end();return}
      res.setHeader('Content-Type','application/json');res.end(JSON.stringify(record))
    }catch{if(!res.destroyed){if(!res.headersSent)res.writeHead(409);res.end()}}
  })
  const supervisor=await queryProcessIdentity(process.pid)
  if(!supervisor)throw Error('SUPERVISOR_IDENTITY_UNAVAILABLE')
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve))
  const address=server.address()
  if(!address||typeof address==='string')throw Error('SUPERVISOR_ADDRESS_UNAVAILABLE')
  record=journal.update(record,{state:'launching',supervisor,port:address.port})
  let text=''
  const inputTimeout=setTimeout(()=>{try{finish('launch_failed')}finally{process.exit(1)}},5000)
  for await(const chunk of process.stdin){text+=chunk;if(text.length>8192)throw Error('LAUNCH_INPUT_INVALID')}
  clearTimeout(inputTimeout)
  let launch:{path:string;maxSeconds:number;gameSessionId:string}
  try{
    launch=JSON.parse(text) as typeof launch
    if(launch.gameSessionId!==sessionId||typeof launch.path!=='string'||Date.now()>=record.deadline)throw Error('LAUNCH_INPUT_INVALID')
    const executable=await executableIdentity(launch.path)
    if(executable.pathHash!==record.launchIdentity.pathHash||executable.fileId!==record.launchIdentity.fileId)throw Error('EXECUTABLE_CHANGED')
  }catch{finish('launch_failed');return}
  try{
    const opened=await controller.open({...launch,title:'',maxSeconds:3600})
    rootIdentity=await queryProcessIdentity(opened.processId)
    if(record.state==='closed')return
    if(!rootIdentity)throw Error('GAME_IDENTITY_UNAVAILABLE')
    record=journal.update(record,{state:'running',process:rootIdentity})
    deadlineTimer=setTimeout(()=>{void controller.close('time_limit',sessionId)},Math.max(0,record.deadline-Date.now()))
  }catch{
    const receipt=await controller.close('launch_cancelled')
    if(receipt.closed)finish('launch_failed')
  }
}
const index=process.argv.indexOf('--hsc-game-supervisor')
if(index>=0)void main(process.argv[index+1],process.argv[index+2]).catch(()=>process.exit(1))
