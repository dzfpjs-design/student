import { createHash, randomUUID } from 'node:crypto'
import { closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import type { ExecutableIdentity, ProcessIdentity } from './game-process-identity'

export type GameRecord={schema:1;revision:number;sessionId:string;instanceId:string;token:string|null;state:'reserved'|'launching'|'running'|'closed';launchIdentity:ExecutableIdentity;deadline:number;supervisor:ProcessIdentity|null;process:ProcessIdentity|null;port:number|null;exitProof:'actual_exit'|'launch_failed'|'windows_tree_exit'|'cancelled_before_spawn'|null;reason:string|null}
export class GameProcessJournal {
  constructor(readonly directory:string){}
  private path(sessionId:string):string{return join(this.directory,createHash('sha256').update(sessionId).digest('hex')+'.json')}
  private ensure():void {
    mkdirSync(this.directory,{recursive:true,mode:0o700})
    if(!lstatSync(this.directory).isDirectory()||lstatSync(this.directory).isSymbolicLink())throw Error('GAME_JOURNAL_UNSAFE')
  }
  read(sessionId:string):GameRecord|null {
    this.ensure();const path=this.path(sessionId)
    if(!existsSync(path))return null
    const value=this.readPath(path)
    if(value.sessionId!==sessionId)throw Error('GAME_JOURNAL_SCOPE_MISMATCH')
    return value
  }
  private readPath(path:string):GameRecord {
    if(lstatSync(path).isSymbolicLink())throw Error('GAME_JOURNAL_UNSAFE')
    const value=JSON.parse(readFileSync(path,'utf8')) as GameRecord
    if(!value||value.schema!==1||!Number.isSafeInteger(value.revision)||value.revision<1||typeof value.sessionId!=='string'||!value.sessionId.trim()||value.sessionId.length>128||typeof value.instanceId!=='string'||!['reserved','launching','running','closed'].includes(value.state)||!Number.isFinite(value.deadline)||!value.launchIdentity||typeof value.launchIdentity.pathHash!=='string'||typeof value.launchIdentity.fileId!=='string')throw Error('GAME_JOURNAL_CORRUPT')
    if(value.state==='closed'&&(!['actual_exit','launch_failed','windows_tree_exit','cancelled_before_spawn'].includes(value.exitProof??'')||value.token!==null))throw Error('GAME_JOURNAL_CORRUPT')
    if(value.state!=='closed'&&(typeof value.token!=='string'||!/^[a-f0-9]{64}$/.test(value.token)))throw Error('GAME_JOURNAL_CORRUPT')
    return value
  }
  list():GameRecord[]{this.ensure();return readdirSync(this.directory).filter(name=>/^[a-f0-9]{64}\.json$/.test(name)).map(name=>this.readPath(join(this.directory,name)))}
  private syncDirectory():void {if(process.platform==='win32')return;const fd=openSync(this.directory,'r');try{fsyncSync(fd)}finally{closeSync(fd)}}
  private mutex<T>(operation:()=>T):T {
    this.ensure()
    // The unpublished directory-lock prototype had no recoverable owner proof.
    // Never guess whether such an unknown external writer is alive.
    if(existsSync(join(this.directory,'.writer-lock')))throw Error('GAME_JOURNAL_LEGACY_LOCK_UNKNOWN')
    const path=join(this.directory,'.writer.sqlite')
    if(existsSync(path)&&lstatSync(path).isSymbolicLink())throw Error('GAME_JOURNAL_UNSAFE')
    closeSync(openSync(path,'a',0o600))
    const lock=new DatabaseSync(path)
    try {
      // SQLite owns the cross-process OS lock. Death releases it, with no
      // heartbeat, PID inference, stale-lock timeout, or lock-file unlink.
      try{lock.exec('PRAGMA busy_timeout=0; BEGIN IMMEDIATE')}
      catch(error){if([5,6].includes((error as {errcode?:number}).errcode??-1))throw Error('GAME_JOURNAL_BUSY');throw error}
      return operation()
    }finally{try{if(lock.isTransaction)lock.exec('ROLLBACK')}finally{lock.close()}}
  }
  reserve(record:GameRecord):void {
    this.mutex(()=>this.reserveLocked(record))
  }
  private reserveLocked(record:GameRecord):void {
    this.ensure()
    if(this.read(record.sessionId)||this.list().some(row=>row.state!=='closed'))throw Error('原游戏尚未确认关闭，不能启动或重放游戏。')
    // The exclusive lease is retained across host crashes. A partial lease or
    // record is uncertainty, never permission to start a second game.
    const lease=join(this.directory,'active.lock')
    if(existsSync(lease))this.releaseClosedLeaseLocked()
    if(existsSync(this.path(record.sessionId)))throw Error('GAME_JOURNAL_CONFLICT')
    // Publish the no-spawn reservation before the lease. A killed writer can
    // leave a cancellable reservation, never an ownerless half-written lease.
    this.atomic(record)
    const temp=join(this.directory,'.lease-'+randomUUID()),fd=openSync(temp,'wx',0o600)
    try{writeFileSync(fd,JSON.stringify({sessionId:record.sessionId,instanceId:record.instanceId}));fsyncSync(fd)}finally{closeSync(fd)}
    renameSync(temp,lease);this.syncDirectory()
  }
  update(previous:GameRecord,values:Partial<GameRecord>):GameRecord {
    return this.mutex(()=>this.updateLocked(previous,values))
  }
  private updateLocked(previous:GameRecord,values:Partial<GameRecord>):GameRecord {
    const current=this.read(previous.sessionId)
    if(!current||current.revision!==previous.revision||current.instanceId!==previous.instanceId||current.state==='closed')throw Error('GAME_JOURNAL_CONFLICT')
    if(values.state&&values.state!==current.state&&!({reserved:['launching','closed'],launching:['running','closed'],running:['closed']}[current.state] as string[]).includes(values.state))throw Error('GAME_JOURNAL_INVALID_TRANSITION')
    const next={...previous,...values,schema:1 as const,sessionId:previous.sessionId,instanceId:previous.instanceId,revision:previous.revision+1}
    this.atomic(next)
    if(next.state==='closed')this.releaseClosedLeaseLocked()
    return next
  }
  private atomic(record:GameRecord):void {
    const temp=join(this.directory,'.write-'+randomUUID()),fd=openSync(temp,'wx',0o600)
    try{writeFileSync(fd,JSON.stringify(record));fsyncSync(fd)}finally{closeSync(fd)}
    renameSync(temp,this.path(record.sessionId));this.syncDirectory()
  }
  confirmClosed(sessionId:string):GameRecord {
    const record=this.read(sessionId)
    if(record?.state!=='closed')throw Error('GAME_EXIT_NOT_CONFIRMED')
    const fd=openSync(this.path(sessionId),'r+')
    try{fsyncSync(fd)}finally{closeSync(fd)}
    this.syncDirectory();return record
  }
  releaseClosedLease():void {
    this.mutex(()=>this.releaseClosedLeaseLocked())
  }
  private releaseClosedLeaseLocked():void {
    const path=join(this.directory,'active.lock')
    if(!existsSync(path))return
    if(lstatSync(path).isSymbolicLink())throw Error('GAME_JOURNAL_UNSAFE')
    const lease=JSON.parse(readFileSync(path,'utf8')) as {sessionId:string;instanceId:string}
    const record=this.read(lease.sessionId)
    if(!record||record.instanceId!==lease.instanceId||record.state!=='closed')throw Error('原游戏尚未确认关闭。')
    unlinkSync(path);this.syncDirectory()
  }
  hasUnresolvedLease():boolean {
    this.ensure()
    try{this.releaseClosedLease();return false}catch{return true}
  }
}
