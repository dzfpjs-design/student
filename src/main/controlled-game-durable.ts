import { randomBytes, randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import { request, type ClientRequest } from 'node:http'
import { GameProcessJournal, type GameRecord } from './game-process-journal'
import { executableIdentity, queryProcessIdentity, sameProcessIdentity } from './game-process-identity'
import type { ControlledGameLaunch, ControlledGameLaunchResult } from './controlled-game'
import type { ControlledCloseReceipt } from './controlled-resource-scope'

export type DurableGameOptions={directory:string;supervisorPath:string;runtimeExecutable?:string;timeoutMs?:number}
const sleep=(ms:number):Promise<void>=>new Promise(resolve=>setTimeout(resolve,ms))
const unknown=(sessionId?:string):ControlledCloseReceipt=>({closed:false,...(sessionId?{sessionId}:{}),proof:'unknown'})
export class DurableGameController {
  private readonly journal:GameProcessJournal
  private readonly watches=new Map<string,ClientRequest>()
  private readonly notified=new Set<string>()
  private starting=false
  constructor(private readonly onClosed:(reason:string,id:string)=>void,private readonly options:DurableGameOptions){this.journal=new GameProcessJournal(options.directory)}
  hasRecord(sessionId:string):boolean{return this.journal.read(sessionId)!==null}
  pendingIds():string[]{if(this.journal.hasUnresolvedLease())throw Error('GAME_JOURNAL_BUSY');return this.journal.list().filter(row=>row.state!=='closed').map(row=>row.sessionId)}

  async initialize():Promise<{unresolved:number}> {
    try{
      const pending=this.journal.list().filter(record=>record.state!=='closed')
      for(const record of pending)await this.watch(record)
      return {unresolved:pending.length||Number(this.journal.hasUnresolvedLease())}
    }catch{return {unresolved:1}}
  }
  async open(values:ControlledGameLaunch):Promise<ControlledGameLaunchResult> {
    if(this.starting)throw Error('原游戏启动或关闭尚未结束。')
    this.starting=true
    try{
      if(typeof values.gameSessionId!=='string'||!values.gameSessionId.trim()||values.gameSessionId.length>128)throw Error('游戏记录无效。')
      if((await this.initialize()).unresolved)throw Error('原游戏尚未确认关闭，没有启动新游戏。')
      if(this.journal.read(values.gameSessionId))throw Error('原游戏记录已存在，没有重新启动。')
      const record:GameRecord={schema:1,revision:1,sessionId:values.gameSessionId,instanceId:randomUUID(),token:randomBytes(32).toString('hex'),state:'reserved',launchIdentity:await executableIdentity(values.path),deadline:Date.now()+Math.min(3600,Math.max(30,Math.floor(values.maxSeconds)))*1000,supervisor:null,process:null,port:null,exitProof:null,reason:null}
      this.journal.reserve(record)
      const env:NodeJS.ProcessEnv={ELECTRON_RUN_AS_NODE:'1'}
      for(const key of ['PATH','SystemRoot','WINDIR','TEMP','TMP','TMPDIR','HOME','USERPROFILE','LOCALAPPDATA','APPDATA'])if(process.env[key])env[key]=process.env[key]
      const worker=spawn(this.options.runtimeExecutable??process.execPath,[this.options.supervisorPath,'--hsc-game-supervisor',this.options.directory,record.sessionId],{env,detached:true,stdio:['pipe','ignore','ignore'],windowsHide:true})
      // No launch path, title, model key or family content is persisted in the journal.
      worker.stdin?.on('error',()=>{})
      worker.on('error',()=>{try{this.journal.update(record,{state:'closed',token:null,exitProof:'launch_failed',reason:'launch_failed'})}catch{/* An uncertain launch remains blocking. */}})
      worker.stdin?.end(JSON.stringify({path:values.path,maxSeconds:values.maxSeconds,gameSessionId:values.gameSessionId}))
      worker.unref()
      const end=Date.now()+(this.options.timeoutMs??7000)
      while(Date.now()<end){
        const current=this.journal.read(record.sessionId)
        if(current?.state==='closed')throw Error('游戏启动未完成或已经退出，没有重新启动。')
        if(current?.state==='running'&&current.process){await this.watch(current);return {opened:true,processId:current.process.pid}}
        await sleep(35)
      }
      throw Error('原游戏启动状态尚未核实，请核对原记录，不要再次启动。')
    }finally{this.starting=false}
  }
  private async verified(record:GameRecord):Promise<boolean> {
    if(!record.supervisor||!record.token||!Number.isInteger(record.port)||record.port!<1||record.port!>65535)return false
    try{
      const owner=await queryProcessIdentity(record.supervisor.pid)
      if(!owner||!sameProcessIdentity(owner,record.supervisor))return false
      const response=await this.rpc(record,'GET','/status')
      return response.instanceId===record.instanceId&&response.sessionId===record.sessionId&&response.supervisor!=null&&sameProcessIdentity(response.supervisor,record.supervisor)
        &&response.launchIdentity.pathHash===record.launchIdentity.pathHash&&response.launchIdentity.fileId===record.launchIdentity.fileId
        &&(record.process===null?response.process===null:response.process!==null&&sameProcessIdentity(response.process,record.process))
    }catch{return false}
  }
  private rpc(record:GameRecord,method:string,path:string,body?:unknown):Promise<GameRecord> {
    return new Promise((resolve,reject)=>{
      const req=request({host:'127.0.0.1',port:record.port!,path,method,headers:{Authorization:`Bearer ${record.token}`,'Content-Type':'application/json'}},res=>{
        let data='';res.on('data',chunk=>{data+=chunk;if(data.length>32768)req.destroy(Error('SUPERVISOR_RESPONSE_INVALID'))})
        res.on('end',()=>{try{if(res.statusCode!==200)throw Error('SUPERVISOR_REJECTED');resolve(JSON.parse(data) as GameRecord)}catch(error){reject(error)}})
      })
      req.setTimeout(this.options.timeoutMs??7000,()=>req.destroy(Error('SUPERVISOR_TIMEOUT')))
      req.on('error',reject);req.end(body?JSON.stringify(body):undefined)
    })
  }
  private async watch(record:GameRecord):Promise<void> {
    if(this.watches.has(record.instanceId)||!await this.verified(record))return
    const req=request({host:'127.0.0.1',port:record.port!,path:'/watch',headers:{Authorization:`Bearer ${record.token}`}},res=>{
      let body='';res.on('data',chunk=>{body+=chunk;if(body.length>32768)req.destroy();if(body.includes('\n')){try{const message=JSON.parse(body.trim()) as GameRecord;if(message.instanceId===record.instanceId)this.observeClosed(record.sessionId)}catch{}req.destroy()}})
    })
    this.watches.set(record.instanceId,req)
    const release=():void=>{if(this.watches.get(record.instanceId)===req)this.watches.delete(record.instanceId)}
    req.on('error',release);req.on('close',release);req.end()
  }
  private observeClosed(sessionId:string):void {
    try{
      const record=this.journal.confirmClosed(sessionId)
      if(record?.state!=='closed'||!record.exitProof||this.notified.has(record.instanceId))return
      this.notified.add(record.instanceId);this.onClosed(record.reason??'process_exited',sessionId)
    }catch{/* A corrupt journal is not a resource-closed notification. */}
  }
  async close(reason='child_requested',expectedSessionId?:string):Promise<ControlledCloseReceipt> {
    try{
      const rows=this.journal.list(),pending=rows.filter(row=>row.state!=='closed')
      if(expectedSessionId!==undefined){
        if(!expectedSessionId.trim()||expectedSessionId.length>128)return unknown()
        if(pending.some(row=>row.sessionId!==expectedSessionId))return {closed:false,sessionId:expectedSessionId,proof:'identity_mismatch'}
        const record=this.journal.read(expectedSessionId)
        if(!record)return unknown(expectedSessionId)
        if(record.state==='closed'){this.journal.confirmClosed(expectedSessionId);return {closed:true,sessionId:expectedSessionId,proof:'observed_closed'}}
        return await this.closeRecord(record,reason)
      }
      for(const record of pending)if(!(await this.closeRecord(record,reason)).closed)return unknown()
      return {closed:!this.journal.hasUnresolvedLease()&&this.journal.list().every(row=>row.state==='closed')}
    }catch{return unknown(expectedSessionId)}
  }
  private async closeRecord(record:GameRecord,reason:string):Promise<ControlledCloseReceipt> {
    if(record.state==='reserved'){
      // The supervisor must commit launching with this exact revision BEFORE
      // spawning. Closing reserved fences a late supervisor; it cannot revive it.
      this.journal.update(record,{state:'closed',token:null,exitProof:'cancelled_before_spawn',reason:'launch_cancelled'})
      this.journal.confirmClosed(record.sessionId)
      return {closed:true,sessionId:record.sessionId,proof:'observed_closed'}
    }
    if(!await this.verified(record))return unknown(record.sessionId)
    try{await this.rpc(record,'POST','/close',{sessionId:record.sessionId,instanceId:record.instanceId,reason})}catch{/* Lost response must be resolved from durable proof, never a second signal here. */}
    const current=this.journal.read(record.sessionId)
    if(current?.instanceId===record.instanceId&&current.state==='closed'){
      this.journal.confirmClosed(record.sessionId)
      this.observeClosed(record.sessionId)
      return {closed:true,sessionId:record.sessionId,proof:'observed_closed'}
    }
    return unknown(record.sessionId)
  }
  detach():void{for(const request of this.watches.values())request.destroy();this.watches.clear()}
}
