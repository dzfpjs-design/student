import {existsSync,lstatSync,mkdirSync,openSync,closeSync,realpathSync} from 'node:fs'
import {isAbsolute,join,parse,sep} from 'node:path'
import {DatabaseSync} from 'node:sqlite'

export type DraftPrivacyBinding={schema:1;kind:'delete'|'restore'|'portable_restore';child_id:string|null;
  scope:'knowledge'|'all_history'|null;request_id:string|null;expected_generation:string|null;
  cleanup_manifest_sha256:string|null;deletion_id:string|null;command_sha256:string}
export type DraftPrivacyRecovery={revokeChild:(childId:string)=>void;revokeAll:()=>void;
  eraseChild:(childId:string)=>void;eraseAll:()=>void}
const keys=['schema','kind','child_id','scope','request_id','expected_generation','cleanup_manifest_sha256','deletion_id','command_sha256']
const id=(v:unknown):v is string=>typeof v==='string'&&v.trim()===v&&v.length>0&&v.length<=128
const digest=(v:unknown):v is string=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v)
export function validDraftPrivacy(value:unknown):value is DraftPrivacyBinding{
  if(!value||typeof value!=='object'||Array.isArray(value))return false
  const v=value as DraftPrivacyBinding
  if(Object.keys(v).length!==keys.length||!keys.every(k=>Object.hasOwn(v,k))||v.schema!==1||!digest(v.command_sha256))return false
  if(v.deletion_id!==null&&!id(v.deletion_id))return false
  const original=id(v.request_id)&&id(v.expected_generation)&&digest(v.cleanup_manifest_sha256)
  const jobOnly=v.request_id===null&&id(v.expected_generation)&&v.cleanup_manifest_sha256===null&&id(v.deletion_id)
  if(v.kind==='delete')return id(v.child_id)&&['knowledge','all_history'].includes(v.scope??'')&&(original||v.scope==='knowledge'&&jobOnly)
  return ['restore','portable_restore'].includes(v.kind)&&v.child_id===null&&v.scope===null&&original&&v.deletion_id===null
}
const same=(a:DraftPrivacyBinding,b:DraftPrivacyBinding)=>keys.every(k=>a[k as keyof DraftPrivacyBinding]===b[k as keyof DraftPrivacyBinding])
const sameOriginal=(a:DraftPrivacyBinding,b:DraftPrivacyBinding)=>['kind','child_id','scope','request_id','expected_generation','cleanup_manifest_sha256'].every(k=>a[k as keyof DraftPrivacyBinding]===b[k as keyof DraftPrivacyBinding])
function safe<T>(fn:()=>T):T{try{return fn()}catch(error){if(error instanceof Error&&/^DRAFT_PRIVACY_[A-Z_]+$/.test(error.message))throw error;throw Error('DRAFT_PRIVACY_IO')}}

/** Main-only durable fence. Root supplies fixed managed userDataPath, never a renderer path.
 * Stores identity only: no parent token, command body, draft content or backend authority. */
export class LocalDraftPrivacy{
  private readonly root:string
  private readonly file:string
  constructor(userDataPath:string,private readonly recovery:DraftPrivacyRecovery,
    private readonly fault?:(point:'before-commit'|'after-commit')=>void,private readonly prepareRecovery?:()=>Promise<void>){
    this.root=join(userDataPath,'parent-draft-privacy');this.file=join(this.root,'barrier.sqlite')
    safe(()=>{
      if(!isAbsolute(userDataPath))throw Error('DRAFT_PRIVACY_UNSAFE_PATH')
      this.directory(userDataPath)
      if(!existsSync(this.root))mkdirSync(this.root,{mode:0o700})
      this.directory(this.root)
      if(!existsSync(this.file))closeSync(openSync(this.file,'wx',0o600))
      this.connection(c=>{
        c.exec("CREATE TABLE IF NOT EXISTS draft_privacy_fence(singleton INTEGER PRIMARY KEY CHECK(singleton=1),binding TEXT NOT NULL,dispatch_state TEXT NOT NULL DEFAULT 'prepared' CHECK(dispatch_state IN ('prepared','possibly_dispatched')))")
        // R1 did not durably distinguish dispatch. Never turn an old ambiguous
        // request into a locally proven non-dispatch during migration.
        if(!c.prepare('PRAGMA table_info(draft_privacy_fence)').all().some(r=>r.name==='dispatch_state'))
          c.exec("ALTER TABLE draft_privacy_fence ADD COLUMN dispatch_state TEXT NOT NULL DEFAULT 'possibly_dispatched' CHECK(dispatch_state IN ('prepared','possibly_dispatched'))")
      })
      this.active()
    })
  }
  private directory(target:string):void{
    let p=parse(target).root
    for(const part of target.slice(p.length).split(sep).filter(Boolean)){
      p=join(p,part);const s=lstatSync(p)
      if(!s.isDirectory()||s.isSymbolicLink())throw Error('DRAFT_PRIVACY_UNSAFE_PATH')
    }
    if(realpathSync(target)!==target)throw Error('DRAFT_PRIVACY_UNSAFE_PATH')
  }
  private connection<T>(fn:(c:DatabaseSync)=>T):T{
    return safe(()=>{
      this.directory(this.root)
      for(const p of [this.file,...['-journal','-wal','-shm'].map(s=>this.file+s)])if(existsSync(p)){
        const s=lstatSync(p);if(!s.isFile()||s.isSymbolicLink()||s.nlink!==1)throw Error('DRAFT_PRIVACY_UNSAFE_PATH')
      }
      if(!existsSync(this.file))throw Error('DRAFT_PRIVACY_STORE_MISSING')
      const c=new DatabaseSync(this.file)
      try{c.exec('PRAGMA synchronous=FULL; PRAGMA busy_timeout=0');return fn(c)}finally{c.close()}
    })
  }
  private transaction(fn:(c:DatabaseSync)=>void):void{
    this.connection(c=>{c.exec('BEGIN IMMEDIATE');try{fn(c);this.fault?.('before-commit');c.exec('COMMIT');this.fault?.('after-commit')}catch(error){if(c.isTransaction)c.exec('ROLLBACK');throw error}})
  }
  active():DraftPrivacyBinding|null{return this.connection(c=>{
    const row=c.prepare('SELECT binding,dispatch_state FROM draft_privacy_fence WHERE singleton=1').get()
    if(!row)return null
    if(row.dispatch_state!=='prepared'&&row.dispatch_state!=='possibly_dispatched')throw Error('DRAFT_PRIVACY_CORRUPT')
    if(typeof row.binding!=='string'||row.binding.length>4096)throw Error('DRAFT_PRIVACY_CORRUPT')
    let value:unknown;try{value=JSON.parse(row.binding)}catch{throw Error('DRAFT_PRIVACY_CORRUPT')}
    if(!validDraftPrivacy(value))throw Error('DRAFT_PRIVACY_CORRUPT')
    return value
  })}
  isPrivacyBlocked(childId:string):boolean{const active=this.active();return !!active&&(active.child_id===null||active.child_id===childId)}
  isBlocked(childId:string):boolean{return this.isPrivacyBlocked(childId)}
  dispatchState(binding:DraftPrivacyBinding):'prepared'|'possibly_dispatched'{
    const active=this.active();if(!active||!same(active,binding))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
    return this.connection(c=>{
      const state=c.prepare('SELECT dispatch_state FROM draft_privacy_fence WHERE singleton=1').get()?.dispatch_state
      if(state!=='prepared'&&state!=='possibly_dispatched')throw Error('DRAFT_PRIVACY_CORRUPT')
      return state
    })
  }
  markDispatched(binding:DraftPrivacyBinding):void{
    this.transaction(c=>{
      const active=this.active();if(!active||!same(active,binding))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
      c.prepare("UPDATE draft_privacy_fence SET dispatch_state='possibly_dispatched' WHERE singleton=1").run()
    })
  }
  begin(binding:DraftPrivacyBinding,authorizedSameJobRetry=false):void{
    if(!validDraftPrivacy(binding))throw Error('DRAFT_PRIVACY_INVALID')
    // Synchronous revoke includes pending authorization; no await separates fence and dispatch.
    if(binding.child_id===null)this.recovery.revokeAll();else this.recovery.revokeChild(binding.child_id)
    this.transaction(c=>{
      const row=c.prepare('SELECT binding FROM draft_privacy_fence WHERE singleton=1').get()
      if(row){
        const previous=this.active()
        if(!previous||!same(previous,binding)&&!(authorizedSameJobRetry&&sameOriginal(previous,binding)&&binding.deletion_id!==null
          &&(previous.deletion_id===null||previous.deletion_id===binding.deletion_id)))throw Error('DRAFT_PRIVACY_PENDING')
      }
      c.prepare("INSERT INTO draft_privacy_fence(singleton,binding,dispatch_state) VALUES(1,?,'prepared') ON CONFLICT(singleton) DO UPDATE SET binding=excluded.binding").run(JSON.stringify(binding))
    })
    const saved=this.active();if(!saved||!same(saved,binding))throw Error('DRAFT_PRIVACY_CORRUPT')
  }
  async prepareErase(binding:DraftPrivacyBinding):Promise<void>{
    const before=this.active();if(!before||!same(before,binding))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
    await this.prepareRecovery?.()
    const after=this.active();if(!after||!same(after,binding))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
  }
  erase(binding:DraftPrivacyBinding):void{
    const active=this.active();if(!active||!same(active,binding))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
    if(binding.child_id===null)this.recovery.eraseAll();else this.recovery.eraseChild(binding.child_id)
  }
  finish(binding:DraftPrivacyBinding):void{
    this.transaction(c=>{
      const row=c.prepare('SELECT binding FROM draft_privacy_fence WHERE singleton=1').get()
      if(!row||!same(JSON.parse(row.binding as string),binding))throw Error('DRAFT_PRIVACY_SCOPE_CHANGED')
      c.prepare('DELETE FROM draft_privacy_fence WHERE singleton=1').run()
    })
  }
}
