import {mkdirSync,lstatSync,existsSync,openSync,closeSync} from 'node:fs'
import {join} from 'node:path'
import {randomUUID,randomBytes,createHmac} from 'node:crypto'
import {DatabaseSync} from 'node:sqlite'
import {queryProcessIdentity,type ProcessIdentity} from './game-process-identity'
import type {ControlledLaunchIntent,ControlledLaunchProof,ControlledLaunchReceipt,ControlledLaunchScope} from '../shared/controlledLaunch'
import {validNativePrivacy,type NativePrivacyMetadata} from '../shared/nativePrivacy'

type Row=ControlledLaunchIntent&{owner:ProcessIdentity|null;proof:string|null;proofNonce:string|null;revision:number}
type Drivers={read:(intent:ControlledLaunchIntent)=>Promise<ControlledLaunchReceipt|null>;
  epoch?:(scope:ControlledLaunchScope)=>Promise<string>;
  cancelMissing?:(intent:ControlledLaunchIntent)=>Promise<boolean>;
  confirmNoDispatch?:(binding:ControlledLaunchReceipt,proof:string,version:{nonce:string;revision:number})=>Promise<ControlledLaunchReceipt>;
  close:(kind:'video'|'game',id:string)=>Promise<ControlledLaunchProof>;hasResource:(kind:'video'|'game',id:string)=>Promise<boolean>;
  own?:()=>Promise<ProcessIdentity|null>;queryOwner?:(pid:number)=>Promise<ProcessIdentity|null>}
const id=(value:unknown):value is string=>typeof value==='string'&&value.trim()===value&&value.length>0&&value.length<=128
const scopeValid=(s:ControlledLaunchScope):boolean=>s&&id(s.childId)&&id(s.sessionId)&&id(s.requestId)&&['video','game'].includes(s.kind)
const sameScope=(a:ControlledLaunchScope,b:ControlledLaunchScope):boolean=>a.childId===b.childId&&a.sessionId===b.sessionId&&a.kind===b.kind&&a.requestId===b.requestId
const publicRow=({owner:_owner,proof:_proof,proofNonce:_nonce,revision:_revision,...row}:Row):ControlledLaunchIntent=>row
const unknown=(binding?:ControlledLaunchReceipt):ControlledLaunchProof=>({closed:false,proof:'unknown',...(binding?{sessionId:binding.activityId,binding}:{})})
export class ControlledLaunchIntents {
  private readonly path:string
  constructor(directory:string,private readonly drivers:Drivers){
    mkdirSync(directory,{recursive:true,mode:0o700})
    if(lstatSync(directory).isSymbolicLink())throw Error('LAUNCH_JOURNAL_UNSAFE')
    this.path=join(directory,'intents.sqlite')
    if(existsSync(this.path)&&lstatSync(this.path).isSymbolicLink())throw Error('LAUNCH_JOURNAL_UNSAFE')
    closeSync(openSync(this.path,'a',0o600))
    this.connection(c=>c.exec(`CREATE TABLE IF NOT EXISTS launch_intent(
      intent_id TEXT PRIMARY KEY,child_id TEXT NOT NULL,session_id TEXT NOT NULL,kind TEXT NOT NULL,
      request_id TEXT NOT NULL,activity_id TEXT UNIQUE,state TEXT NOT NULL,owner_json TEXT,proof TEXT,
      proof_nonce TEXT,revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),native_epoch TEXT,
      UNIQUE(child_id,session_id,kind,request_id),CHECK(kind IN ('video','game')),
      CHECK(state IN ('prepared','dispatching','closed','settled')))`))
    this.transaction(c=>{
      if(!c.prepare('PRAGMA table_info(launch_intent)').all().some(r=>r.name==='native_epoch'))c.exec('ALTER TABLE launch_intent ADD COLUMN native_epoch TEXT')
      c.exec('CREATE TABLE IF NOT EXISTS privacy_barrier(singleton INTEGER PRIMARY KEY CHECK(singleton=1),metadata_json TEXT NOT NULL)')
      c.exec('CREATE TABLE IF NOT EXISTS cancelled_request(scope_hash TEXT PRIMARY KEY)')
      c.exec('CREATE TABLE IF NOT EXISTS native_launch_policy(singleton INTEGER PRIMARY KEY CHECK(singleton=1))')
      c.exec('CREATE TABLE IF NOT EXISTS journal_secret(singleton INTEGER PRIMARY KEY CHECK(singleton=1),key TEXT NOT NULL)')
      c.prepare('INSERT OR IGNORE INTO journal_secret VALUES(1,?)').run(randomBytes(32).toString('hex'))
    })
  }
  private connection<T>(fn:(c:DatabaseSync)=>T):T{const c=new DatabaseSync(this.path);try{c.exec('PRAGMA synchronous=FULL; PRAGMA busy_timeout=0');return fn(c)}finally{c.close()}}
  private transaction<T>(fn:(c:DatabaseSync)=>T):T{return this.connection(c=>{c.exec('BEGIN IMMEDIATE');try{const result=fn(c);c.exec('COMMIT');return result}catch(e){if(c.isTransaction)c.exec('ROLLBACK');throw e}})}
  private decode(raw:Record<string,unknown>):Row{
    const row:Row={intentId:raw.intent_id as string,childId:raw.child_id as string,sessionId:raw.session_id as string,kind:raw.kind as Row['kind'],requestId:raw.request_id as string,activityId:raw.activity_id as string|null,state:raw.state as Row['state'],owner:raw.owner_json?JSON.parse(raw.owner_json as string):null,proof:raw.proof as string|null,proofNonce:raw.proof_nonce as string|null,revision:raw.revision as number,...(raw.native_epoch?{nativeEpoch:raw.native_epoch as string}:{})}
    if(!scopeValid(row)||!id(row.intentId)||(row.activityId!==null&&!id(row.activityId))||!['prepared','dispatching','closed','settled'].includes(row.state)
      ||(row.owner&&(!Number.isSafeInteger(row.owner.pid)||row.owner.pid<=0||!id(row.owner.birth)))
      ||!Number.isSafeInteger(row.revision)||row.revision<1
      ||(row.state==='dispatching'&&!row.owner)||(['closed','settled'].includes(row.state)&&(!row.proof||!id(row.proofNonce))))throw Error('LAUNCH_JOURNAL_CORRUPT')
    return row
  }
  private rows():Row[]{return this.connection(c=>c.prepare('SELECT * FROM launch_intent').all().map(r=>this.decode(r)))}
  private assertNoPrivacy(c:DatabaseSync):void{if(c.prepare('SELECT 1 FROM privacy_barrier').get())throw Error('NATIVE_PRIVACY_PENDING')}
  assertLegacyDispatch(activityId:string):void{
    this.connection(c=>{this.assertNoPrivacy(c);if(c.prepare('SELECT 1 FROM native_launch_policy').get())throw Error('LAUNCH_INTENT_REQUIRED')})
    if(this.unresolved()||this.hasActivity(activityId))throw Error('LAUNCH_INTENT_REQUIRED')
  }
  private scopeHash(c:DatabaseSync,scope:ControlledLaunchScope):string{
    const key=c.prepare('SELECT key FROM journal_secret WHERE singleton=1').get()?.key
    if(typeof key!=='string'||!/^[a-f0-9]{64}$/.test(key))throw Error('LAUNCH_JOURNAL_CORRUPT')
    return createHmac('sha256',Buffer.from(key,'hex')).update(JSON.stringify([scope.childId,scope.sessionId,scope.kind,scope.requestId])).digest('hex')
  }
  activePrivacy():NativePrivacyMetadata|null{return this.connection(c=>{
    const raw=c.prepare('SELECT metadata_json FROM privacy_barrier WHERE singleton=1').get()
    if(!raw)return null
    const value=JSON.parse(raw.metadata_json as string);if(!validNativePrivacy(value))throw Error('NATIVE_PRIVACY_JOURNAL_CORRUPT')
    return value
  })}
  beginPrivacy(metadata:NativePrivacyMetadata):void{
    if(!validNativePrivacy(metadata))throw Error('NATIVE_PRIVACY_SCOPE_INVALID')
    this.transaction(c=>{
      c.exec('INSERT OR IGNORE INTO native_launch_policy VALUES(1)')
      const current=c.prepare('SELECT metadata_json FROM privacy_barrier').get()
      if(current){const old=JSON.parse(current.metadata_json as string);if(!validNativePrivacy(old)||Object.keys(metadata).some(k=>old[k as keyof NativePrivacyMetadata]!==metadata[k as keyof NativePrivacyMetadata]))throw Error('NATIVE_PRIVACY_PENDING');return}
      c.prepare('INSERT INTO privacy_barrier VALUES(1,?)').run(JSON.stringify(metadata))
    })
  }
  privacyIntents(metadata:NativePrivacyMetadata):ControlledLaunchIntent[]{
    const active=this.activePrivacy();if(!active||active.nonce!==metadata.nonce)throw Error('NATIVE_PRIVACY_SCOPE_CHANGED')
    return this.rows().filter(r=>r.state!=='settled'&&(!metadata.child_id||r.childId===metadata.child_id)).map(publicRow)
  }
  finishPrivacy(metadata:NativePrivacyMetadata,committed:boolean):void{
    this.transaction(c=>{
      const raw=c.prepare('SELECT metadata_json FROM privacy_barrier').get()
      const stored=raw?JSON.parse(raw.metadata_json as string):null
      if(!validNativePrivacy(stored)||Object.keys(metadata).some(k=>stored[k as keyof NativePrivacyMetadata]!==metadata[k as keyof NativePrivacyMetadata]))throw Error('NATIVE_PRIVACY_SCOPE_CHANGED')
      const affected=c.prepare('SELECT * FROM launch_intent').all().map(r=>this.decode(r)).filter(r=>!metadata.child_id||r.childId===metadata.child_id)
      if(committed&&affected.some(r=>r.state!=='settled'))throw Error('NATIVE_PRIVACY_INTENT_UNSETTLED')
      if(committed)for(const row of affected){
        c.prepare('INSERT OR IGNORE INTO cancelled_request VALUES(?)').run(this.scopeHash(c,row))
        c.prepare('DELETE FROM launch_intent WHERE intent_id=?').run(row.intentId)
      }
      c.prepare('DELETE FROM privacy_barrier WHERE singleton=1').run()
    })
  }
  async prepareForBackend(scope:ControlledLaunchScope):Promise<ControlledLaunchIntent>{
    this.transaction(c=>{this.assertNoPrivacy(c);c.exec('INSERT OR IGNORE INTO native_launch_policy VALUES(1)')})
    const old=this.rows().find(r=>sameScope(r,scope))
    if(old)return publicRow(old)
    if(!this.drivers.epoch)throw Error('LAUNCH_EPOCH_UNAVAILABLE')
    const nativeEpoch=await this.drivers.epoch(scope)
    if(!id(nativeEpoch))throw Error('LAUNCH_EPOCH_INVALID')
    return this.prepare(scope,nativeEpoch)
  }
  private read(intentId:string):Row{const row=this.rows().find(r=>r.intentId===intentId);if(!row)throw Error('LAUNCH_INTENT_UNKNOWN');return row}
  list(scope:Pick<ControlledLaunchScope,'childId'|'sessionId'|'kind'>&{requestId?:string;includeSettled?:true}):ControlledLaunchIntent[]{
    if(scope.requestId!==undefined&&!id(scope.requestId))throw Error('LAUNCH_SCOPE_INVALID')
    if(scope.includeSettled!==undefined&&(scope.includeSettled!==true||!id(scope.requestId)))throw Error('LAUNCH_SCOPE_INVALID')
    return this.rows().filter(r=>r.childId===scope.childId&&r.sessionId===scope.sessionId&&r.kind===scope.kind
      &&(scope.requestId===undefined||r.requestId===scope.requestId)
      &&(scope.includeSettled===true||r.state!=='settled')).map(publicRow)
  }
  unresolved():boolean{return this.activePrivacy()!==null||this.rows().some(r=>r.state!=='settled')}
  hasActivity(activityId:string):boolean{return this.rows().some(r=>r.activityId===activityId)}
  async finishActivity(kind:'video'|'game',activityId:string):Promise<void>{
    const row=this.rows().find(r=>r.kind===kind&&r.activityId===activityId)
    if(row){const proof=await this.cancel(row.intentId);if(!proof.closed)throw Error('LAUNCH_CLOSURE_UNCONFIRMED');await this.finish(row.intentId)}
  }
  prepare(scope:ControlledLaunchScope,nativeEpoch?:string):ControlledLaunchIntent{
    if(!scopeValid(scope))throw Error('LAUNCH_SCOPE_INVALID')
    return this.transaction(c=>{
      this.assertNoPrivacy(c)
      if(c.prepare('SELECT 1 FROM cancelled_request WHERE scope_hash=?').get(this.scopeHash(c,scope)))throw Error('LAUNCH_CANCELLED')
      const rows=c.prepare('SELECT * FROM launch_intent').all().map(r=>this.decode(r))
      const existing=rows.find(r=>sameScope(r,scope));if(existing)return publicRow(existing)
      if(rows.some(r=>r.state!=='settled'))throw Error('LAUNCH_INTENT_UNRESOLVED')
      const intentId=randomUUID()
      c.prepare("INSERT INTO launch_intent(intent_id,child_id,session_id,kind,request_id,state,native_epoch) VALUES(?,?,?,?,?,'prepared',?)").run(intentId,scope.childId,scope.sessionId,scope.kind,scope.requestId,nativeEpoch??null)
      return {...scope,intentId,activityId:null,state:'prepared',...(nativeEpoch?{nativeEpoch}:{})}
    })
  }
  private async bind(intentId:string):Promise<ControlledLaunchReceipt|null>{
    const row=this.read(intentId),binding=await this.drivers.read(publicRow(row))
    if(binding===null)return null
    if(!sameScope(binding,row)||binding.intentId!==intentId||!id(binding.activityId)||!['active','completed','stopped','expired','interrupted','failed'].includes(binding.status)
      ||(row.activityId!==null&&row.activityId!==binding.activityId)||(row.nativeEpoch&&row.nativeEpoch!==binding.nativeEpoch))throw Error('LAUNCH_BINDING_MISMATCH')
    this.transaction(c=>{
      const current=this.decode(c.prepare('SELECT * FROM launch_intent WHERE intent_id=?').get(intentId)!)
      if(current.activityId!==null&&current.activityId!==binding.activityId)throw Error('LAUNCH_BINDING_MISMATCH')
      if(current.activityId===null)c.prepare('UPDATE launch_intent SET activity_id=?,revision=revision+1 WHERE intent_id=?').run(binding.activityId,intentId)
    })
    return binding
  }
  async dispatch<T>(intentId:string,activityId:string,open:()=>Promise<T>):Promise<T>{
    this.connection(c=>this.assertNoPrivacy(c))
    const binding=await this.bind(intentId)
    if(!binding||binding.activityId!==activityId||binding.status!=='active')throw Error('LAUNCH_BINDING_NOT_ACTIVE')
    const owner=await (this.drivers.own??(()=>queryProcessIdentity(process.pid)))()
    if(!owner)throw Error('LAUNCH_OWNER_UNKNOWN')
    this.transaction(c=>{
      this.assertNoPrivacy(c)
      const row=this.decode(c.prepare('SELECT * FROM launch_intent WHERE intent_id=?').get(intentId)!)
      if(row.state!=='prepared')throw Error(row.state==='closed'||row.state==='settled'?'LAUNCH_CANCELLED':'LAUNCH_ALREADY_DISPATCHED')
      c.prepare("UPDATE launch_intent SET state='dispatching',owner_json=?,revision=revision+1 WHERE intent_id=?").run(JSON.stringify(owner),intentId)
    })
    try{return await open()}catch(error){
      // The callback has finished failing. Both native drivers persist ownership
      // before creating resources; a strict missing-record read proves no dispatch.
      if(!await this.drivers.hasResource(binding.kind,activityId))this.closeRecord(intentId,'cancelled_before_dispatch')
      throw error
    }
  }
  private closeRecord(intentId:string,proof:string):void{this.transaction(c=>{
    const row=this.decode(c.prepare('SELECT * FROM launch_intent WHERE intent_id=?').get(intentId)!)
    if(!['closed','settled'].includes(row.state))c.prepare("UPDATE launch_intent SET state='closed',proof=?,proof_nonce=?,revision=revision+1 WHERE intent_id=?").run(proof,randomUUID(),intentId)
  })}
  async cancel(intentId:string):Promise<ControlledLaunchProof>{
    // Fence an in-flight backend response BEFORE awaiting readback. No timeout
    // or missing backend row is ever treated as proof of an activity's closure.
    this.transaction(c=>{const row=this.decode(c.prepare('SELECT * FROM launch_intent WHERE intent_id=?').get(intentId)!);
      if(row.state==='prepared')c.prepare("UPDATE launch_intent SET state='closed',proof='cancelled_before_dispatch',proof_nonce=?,revision=revision+1 WHERE intent_id=?").run(randomUUID(),intentId)})
    let binding=await this.bind(intentId)
    if(!binding){
      const row=this.read(intentId)
      if(['closed','settled'].includes(row.state)&&row.proof==='cancelled_before_dispatch'&&row.activityId===null&&this.drivers.cancelMissing){
        if(await this.drivers.cancelMissing(publicRow(row))){
          this.transaction(c=>c.prepare("UPDATE launch_intent SET state='settled' WHERE intent_id=? AND state='closed' AND activity_id IS NULL").run(intentId))
          return {closed:true,noActivity:true,proof:'cancelled_before_start'}
        }
        binding=await this.bind(intentId)
      }
      if(!binding)return unknown()
    }
    let row=this.read(intentId)
    if(row.state==='dispatching'){
      const proof=await this.drivers.close(row.kind,binding.activityId)
      if(proof.closed&&proof.sessionId===binding.activityId)this.closeRecord(intentId,'observed_closed')
      else if(!await this.drivers.hasResource(row.kind,binding.activityId)){
        const owner=await (this.drivers.queryOwner??queryProcessIdentity)(row.owner!.pid)
        if(owner===null||owner.birth!==row.owner!.birth)this.closeRecord(intentId,'owner_exit_before_registration')
      }
      row=this.read(intentId)
    }
    if(['closed','settled'].includes(row.state)&&['cancelled_before_dispatch','owner_exit_before_registration'].includes(row.proof!)&&this.drivers.confirmNoDispatch){
      const current=await this.drivers.confirmNoDispatch(binding,row.proof!,{nonce:row.proofNonce!,revision:row.revision})
      if(!sameScope(current,binding)||current.intentId!==binding.intentId||current.activityId!==binding.activityId||current.status!=='stopped')throw Error('LAUNCH_SETTLEMENT_UNCONFIRMED')
      binding=current
    }
    return ['closed','settled'].includes(row.state)?{closed:true,sessionId:binding.activityId,proof:row.proof!,binding}:unknown(binding)
  }
  async closeActivity(kind:'video'|'game',activityId:string):Promise<ControlledLaunchProof|null>{
    for(const row of this.rows().filter(r=>r.kind===kind)){
      if(row.activityId===activityId)return this.cancel(row.intentId)
      if(row.activityId===null){const binding=await this.bind(row.intentId);if(binding?.activityId===activityId)return this.cancel(row.intentId)}
    }
    return null
  }
  async finish(intentId:string):Promise<void>{
    const binding=await this.bind(intentId),row=this.read(intentId)
    if(!binding||binding.status==='active'||!['closed','settled'].includes(row.state))throw Error('LAUNCH_SETTLEMENT_UNCONFIRMED')
    this.transaction(c=>{c.prepare("UPDATE launch_intent SET state='settled' WHERE intent_id=? AND state='closed'").run(intentId)})
  }
}
