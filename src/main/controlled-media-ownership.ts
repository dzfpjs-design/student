import { mkdirSync, lstatSync, existsSync, openSync, closeSync } from 'node:fs'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { DatabaseSync } from 'node:sqlite'
import { queryProcessIdentity, type ProcessIdentity } from './game-process-identity'
import type { ControlledCloseReceipt } from './controlled-resource-scope'

type ExitProof='observed_closed'|'owner_exit'|'cancelled_before_create'
export type MediaOwnershipRecord={sessionId:string;instanceId:string;revision:number;owner:ProcessIdentity;state:'reserved'|'open'|'closed';exitProof:ExitProof|null}
type Row={session_id:string;instance_id:string;revision:number;owner_json:string;state:MediaOwnershipRecord['state'];exit_proof:ExitProof|null}
const decode=(row:Row):MediaOwnershipRecord=>{
  const owner=JSON.parse(row.owner_json) as ProcessIdentity
  if(!owner||!Number.isSafeInteger(owner.pid)||owner.pid<=0||typeof owner.birth!=='string'||!owner.birth||!owner.executable||!/^[a-f0-9]{64}$/.test(owner.executable.pathHash)||typeof owner.executable.fileId!=='string')throw Error('MEDIA_OWNER_CORRUPT')
  return {sessionId:row.session_id,instanceId:row.instance_id,revision:row.revision,owner,state:row.state,exitProof:row.exit_proof}
}
export class ControlledMediaOwnership {
  private readonly path:string
  constructor(directory:string,
    private readonly own:()=>Promise<ProcessIdentity>=async()=>{const identity=await queryProcessIdentity(process.pid);if(!identity)throw Error('MEDIA_OWNER_UNAVAILABLE');return identity},
    private readonly query:(pid:number)=>Promise<ProcessIdentity|null>=queryProcessIdentity,
  ){
    mkdirSync(directory,{recursive:true,mode:0o700})
    if(lstatSync(directory).isSymbolicLink())throw Error('MEDIA_JOURNAL_UNSAFE')
    this.path=join(directory,'ownership.sqlite')
    if(existsSync(this.path)&&lstatSync(this.path).isSymbolicLink())throw Error('MEDIA_JOURNAL_UNSAFE')
    closeSync(openSync(this.path,'a',0o600))
    this.connection(c=>c.exec(`CREATE TABLE IF NOT EXISTS media_owner(
      session_id TEXT PRIMARY KEY,instance_id TEXT NOT NULL UNIQUE,revision INTEGER NOT NULL CHECK(revision>=1),
      owner_json TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('reserved','open','closed')),
      exit_proof TEXT CHECK(exit_proof IN ('observed_closed','owner_exit','cancelled_before_create')),
      CHECK((state='closed')=(exit_proof IS NOT NULL)));
      CREATE UNIQUE INDEX IF NOT EXISTS media_one_unresolved ON media_owner((1)) WHERE state!='closed';`))
  }
  private connection<T>(operation:(c:DatabaseSync)=>T):T{
    const c=new DatabaseSync(this.path)
    try{c.exec('PRAGMA busy_timeout=0; PRAGMA synchronous=FULL');return operation(c)}finally{c.close()}
  }
  private transaction<T>(operation:(c:DatabaseSync)=>T):T{
    return this.connection(c=>{c.exec('BEGIN IMMEDIATE');try{const value=operation(c);c.exec('COMMIT');return value}catch(error){if(c.isTransaction)c.exec('ROLLBACK');throw error}})
  }
  read(id:string):MediaOwnershipRecord|null{
    return this.connection(c=>{const row=c.prepare('SELECT * FROM media_owner WHERE session_id=?').get(id) as Row|undefined;return row?decode(row):null})
  }
  private pending():MediaOwnershipRecord[]{return this.connection(c=>(c.prepare("SELECT * FROM media_owner WHERE state!='closed'").all() as Row[]).map(decode))}
  pendingIds():string[]{return this.pending().map(row=>row.sessionId)}
  async recover():Promise<{unresolved:number}>{
    try{
      for(const record of this.pending()){
        try{
          const current=await this.query(record.owner.pid)
          // An image mismatch alone is NOT owner death (e.g. an updated binary).
          // Only absent PID or a different birth proves the old window owner gone.
          if(current===null||current.birth!==record.owner.birth)this.closed(record,'owner_exit')
        }catch{/* Unknown OS identity or CAS conflict never grants closure. */}
      }
      return {unresolved:this.pending().length}
    }catch{return {unresolved:1}}
  }
  async reserve(id:string):Promise<MediaOwnershipRecord>{
    if(typeof id!=='string'||!id.trim()||id.length>128)throw Error('MEDIA_SESSION_REQUIRED')
    if(this.read(id))throw Error('原视频已有记录，没有重复打开。')
    if((await this.recover()).unresolved)throw Error('原视频尚未确认关闭，没有打开新窗口。')
    const owner=await this.own(),record:MediaOwnershipRecord={sessionId:id,instanceId:randomUUID(),revision:1,owner,state:'reserved',exitProof:null}
    this.transaction(c=>{
      if(c.prepare('SELECT 1 FROM media_owner WHERE session_id=?').get(id))throw Error('原视频已有记录，没有重复打开。')
      if(c.prepare("SELECT 1 FROM media_owner WHERE state!='closed'").get())throw Error('原视频尚未确认关闭，没有打开新窗口。')
      c.prepare('INSERT INTO media_owner VALUES(?,?,?,?,?,NULL)').run(id,record.instanceId,1,JSON.stringify(owner),'reserved')
    })
    return record
  }
  opened(record:MediaOwnershipRecord):MediaOwnershipRecord{return this.change(record,'open',null)}
  closed(record:MediaOwnershipRecord,proof:ExitProof):MediaOwnershipRecord{return this.change(record,'closed',proof)}
  private change(record:MediaOwnershipRecord,state:MediaOwnershipRecord['state'],proof:ExitProof|null):MediaOwnershipRecord{
    return this.transaction(c=>{
      const row=c.prepare('SELECT * FROM media_owner WHERE session_id=?').get(record.sessionId) as Row|undefined
      if(!row||row.instance_id!==record.instanceId)throw Error('MEDIA_OWNER_CONFLICT')
      const current=decode(row)
      if(current.state==='closed'&&state==='closed')return current
      if(current.revision!==record.revision||current.state==='closed'||(state==='open'&&current.state!=='reserved')||(proof==='cancelled_before_create'&&current.state!=='reserved'))throw Error('MEDIA_OWNER_CONFLICT')
      c.prepare('UPDATE media_owner SET state=?,exit_proof=?,revision=revision+1 WHERE session_id=? AND revision=?').run(state,proof,record.sessionId,record.revision)
      return {...current,revision:current.revision+1,state,exitProof:proof}
    })
  }
  async receipt(id:string):Promise<ControlledCloseReceipt>{
    await this.recover()
    try{if(this.read(id)?.state==='closed')return {closed:true,sessionId:id,proof:'observed_closed'}}catch{/* Keep uncertain ownership local. */}
    return {closed:false,sessionId:id,proof:'unknown'}
  }
}
