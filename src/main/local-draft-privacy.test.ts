// @vitest-environment node
import {mkdtempSync,mkdirSync,readFileSync,writeFileSync,existsSync,rmSync,linkSync,symlinkSync,realpathSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {afterEach,expect,it,vi} from 'vitest'
import {ControlledLaunchIntents} from './controlled-launch-intent'
import {NativePrivacyCoordinator} from './native-privacy'
import {LocalDraftPrivacy} from './local-draft-privacy'
const roots:string[]=[]
afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true})})
function fixture(scope='knowledge'){
  const root=realpathSync(mkdtempSync(join(tmpdir(),'hsc-draft-privacy-')));roots.push(root)
  const capsules=join(root,'synthetic-hook-capsules');mkdirSync(capsules)
  for(const child of ['a','b'])for(const s of ['capture','language'])writeFileSync(join(capsules,`${child}-${s}`),`SYNTHETIC-${child}-${s}`)
  let epoch=0,eraserFailure=false
  const recovery={revokeChild:vi.fn(()=>{epoch++}),revokeAll:vi.fn(()=>{epoch++}),
    eraseChild:vi.fn((child:string)=>{epoch++;if(eraserFailure)throw Error('SYNTHETIC_ERASE_FAILURE');for(const s of ['capture','language'])rmSync(join(capsules,`${child}-${s}`),{force:true})}),
    eraseAll:vi.fn(()=>{epoch++;if(eraserFailure)throw Error('SYNTHETIC_ERASE_FAILURE');for(const child of ['a','b'])for(const s of ['capture','language'])rmSync(join(capsules,`${child}-${s}`),{force:true})})}
  const drafts=new LocalDraftPrivacy(root,recovery)
  const drivers={read:async()=>null,close:async()=>({closed:false}),hasResource:async()=>false,cancelMissing:async()=>true}
  const journal=new ControlledLaunchIntents(root,drivers)
  const operation={request_id:'original-delete',expected_generation:'generation',cleanup_manifest_sha256:'a'.repeat(64)}
  const command={path:'/v1/parent/children/a/delete',body:{scope,confirmation:'确认删除',client_action_id:operation.request_id,operation}}
  let status='missing',lose=false,permitState='missing',badReceipt=false,finalize=false,wrongJob=false
  const receipt=()=>({request_id:operation.request_id,kind:command.path.endsWith('/restore')?(command.path.includes('/portable/')?'portable_restore':'restore'):'delete',child_id:command.path.endsWith('/restore')?null:'a',scope:command.path.endsWith('/restore')?null:scope,origin_generation:'generation',result_generation:'generation',current_generation:'generation',cleanup_manifest_sha256:operation.cleanup_manifest_sha256,status:finalize?'finalization_required':status,cleanup_allowed:status==='committed',reference:{type:'deletion_job',id:wrongJob?'foreign-job':'job-a'},...(badReceipt?{child_id:'b'}:{})})
  const call=vi.fn(async(path:string,body:unknown,token:string,metadata?:unknown):Promise<{status:number;body:unknown}>=>{
    expect(token).toBe('parent-token')
    if(path.endsWith('/generation'))return {status:200,body:{generation:'generation'}}
    if(path.endsWith('/native-context'))return {status:200,body:{native_required:scope==='all_history',operation:scope==='all_history'?operation:null,child_id:'a',scope}}
    if(path.startsWith('/v1/parent/privacy-operations/'))return {status:status==='missing'?404:200,body:receipt()}
    expect(drafts.isPrivacyBlocked('a')).toBe(true)
    if(scope==='knowledge'&&!command.path.endsWith('/restore'))expect(metadata).toBeUndefined()
    status='committed';permitState='finished'
    if(lose)throw Error('LOST_RESPONSE')
    return {status:200,body:{deletion_id:'job-a',child_id:'a',scope,target_type:'child_scope',status:'completed',operation_receipt:receipt()}}
  })
  const transport={call,permit:vi.fn(async(metadata:any,_token:string,fence:boolean)=>{
    if(fence&&permitState==='missing'){permitState='cancelled';status='failed'}return {...metadata,status:permitState}
  }),end:vi.fn(async()=>undefined),closeLegacy:vi.fn(async()=>undefined)}
  const make=()=>new NativePrivacyCoordinator(journal,transport,drafts)
  const lease=()=>{const original=epoch;return ()=>{if(original!==epoch||drafts.isPrivacyBlocked('a'))throw Error('REVOKED');writeFileSync(join(capsules,'a-capture'),'RESURRECTED')}}
  return {root,capsules,recovery,drafts,journal,transport,command,make,lease,receipt,failErase:(v=true)=>{eraserFailure=v},setStatus:(v:string)=>{status=v},lose:(v=true)=>{lose=v},badReceipt:()=>{badReceipt=true},wrongJob:()=>{wrongJob=true},finalize:(v:boolean)=>{finalize=v}}
}
it('knowledge is fenced before POST and erases only its child, never claims a native permit',async()=>{
  const f=fixture(),lateWriter=f.lease()
  await f.make().execute(f.command,'parent-token')
  expect(f.recovery.revokeChild).toHaveBeenCalledWith('a')
  expect(f.recovery.eraseChild).toHaveBeenCalledWith('a')
  expect(existsSync(join(f.capsules,'a-capture'))).toBe(false)
  expect(readFileSync(join(f.capsules,'b-language'),'utf8')).toBe('SYNTHETIC-b-language')
  expect(()=>lateWriter()).toThrow('REVOKED');expect(f.drafts.active()).toBeNull()
  expect(f.transport.permit).not.toHaveBeenCalled();expect(f.transport.closeLegacy).not.toHaveBeenCalled()
})
it('knowledge lost response survives reconstruction; original committed receipt erases without another POST',async()=>{
  const f=fixture();f.lose()
  await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('LOST_RESPONSE')
  const restarted=new LocalDraftPrivacy(f.root,f.recovery)
  expect(restarted.isPrivacyBlocked('a')).toBe(true);expect(existsSync(join(f.capsules,'a-capture'))).toBe(true)
  const coordinator=new NativePrivacyCoordinator(f.journal,f.transport,restarted)
  await coordinator.recover('original-delete','parent-token')
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(1)
  expect(restarted.active()).toBeNull();expect(existsSync(join(f.capsules,'a-language'))).toBe(false)
})
it('a committed replay erases capsules and invalidates old/pending grants without stopping new video',async()=>{
  const f=fixture();f.setStatus('committed');const pendingGrant=f.lease()
  await f.make().execute(f.command,'parent-token')
  expect(()=>pendingGrant()).toThrow('REVOKED')
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(0)
  expect(f.recovery.eraseChild).toHaveBeenCalledOnce();expect(f.transport.closeLegacy).not.toHaveBeenCalled()
})
it('matching terminal failed backend operation keeps old human work and releases without erase',async()=>{
  const f=fixture();f.setStatus('failed')
  await f.make().execute(f.command,'parent-token')
  expect(f.recovery.eraseChild).not.toHaveBeenCalled()
  expect(readFileSync(join(f.capsules,'a-capture'),'utf8')).toBe('SYNTHETIC-a-capture')
  expect(new LocalDraftPrivacy(f.root,f.recovery).isPrivacyBlocked('a')).toBe(false)
})
it('erase failure retains committed fence; restart retries only local erase and original GET',async()=>{
  const f=fixture();f.failErase()
  await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('SYNTHETIC_ERASE_FAILURE')
  expect(f.drafts.isPrivacyBlocked('a')).toBe(true)
  f.failErase(false)
  await new NativePrivacyCoordinator(f.journal,f.transport,new LocalDraftPrivacy(f.root,f.recovery)).recover('original-delete','parent-token')
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(1)
  expect(f.drafts.active()).toBeNull()
})
it('foreign committed receipt never erases another scope or releases its fence',async()=>{
  const f=fixture();f.badReceipt()
  await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('DRAFT_PRIVACY')
  expect(f.recovery.eraseChild).not.toHaveBeenCalled();expect(f.drafts.isPrivacyBlocked('a')).toBe(true)
})
it('knowledge retry with original operation=null uses only the exact authorized completed job',async()=>{
  const f=fixture();const command={path:'/v1/parent/deletions/job-a/retry',body:{confirmation:'确认删除'}}
  await f.make().execute(command,'parent-token')
  expect(f.recovery.eraseChild).toHaveBeenCalledWith('a');expect(f.drafts.active()).toBeNull()
  expect(f.transport.permit).not.toHaveBeenCalled()
})
it('lost knowledge retry response retains block and permits only same original job replay',async()=>{
  const f=fixture();f.lose();const command={path:'/v1/parent/deletions/job-a/retry',body:{confirmation:'确认删除'}}
  await expect(f.make().execute(command,'parent-token')).rejects.toThrow('LOST_RESPONSE')
  await expect(f.make().execute({...command,path:'/v1/parent/deletions/foreign-job/retry'},'parent-token')).rejects.toThrow('DRAFT_PRIVACY')
  expect(new LocalDraftPrivacy(f.root,f.recovery).active()?.deletion_id).toBe('job-a')
  f.lose(false);await f.make().execute(command,'parent-token')
  expect(f.drafts.active()).toBeNull();expect(existsSync(join(f.capsules,'a-capture'))).toBe(false)
})
it('failed original knowledge operation can resume only its authorized original deletion job',async()=>{
  const f=fixture();f.setStatus('failed');await f.make().execute(f.command,'parent-token')
  await f.make().execute({path:'/v1/parent/deletions/job-a/retry',body:{confirmation:'确认删除'}},'parent-token')
  expect(f.drafts.active()).toBeNull();expect(f.recovery.eraseChild).toHaveBeenCalledWith('a')
  expect(f.transport.permit).not.toHaveBeenCalled()
})
it('another job reference cannot resume a failed knowledge operation',async()=>{
  const f=fixture();f.lose();await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('LOST_RESPONSE');f.setStatus('failed');f.wrongJob()
  await expect(f.make().execute({path:'/v1/parent/deletions/job-a/retry',body:{confirmation:'确认删除'}},'parent-token')).rejects.toThrow('DRAFT_PRIVACY_SCOPE_CHANGED')
  expect(f.drafts.active()?.deletion_id).toBeNull();expect(f.recovery.eraseChild).not.toHaveBeenCalled()
})
it('job-only retry never interprets a knowledge-source deletion as child-scope deletion',async()=>{
  const f=fixture(),call=f.transport.call.getMockImplementation()!
  f.transport.call.mockImplementation(async(...args)=>{
    const result=await call(...args)
    return args[0].endsWith('/retry')?{status:200,body:{...(result.body as object),target_type:'knowledge_source'}}:result
  })
  await expect(f.make().execute({path:'/v1/parent/deletions/job-a/retry',body:{confirmation:'确认删除'}},'parent-token')).rejects.toThrow('DRAFT_PRIVACY_RECEIPT_MISMATCH')
  expect(f.recovery.eraseChild).not.toHaveBeenCalled();expect(f.drafts.isBlocked('a')).toBe(true)
})
it('legacy job without operation identity cannot erase current work after a generation change',async()=>{
  const f=fixture(),call=f.transport.call.getMockImplementation()!;let generations=0
  f.transport.call.mockImplementation(async(...args)=>{
    if(args[0].endsWith('/generation')&&++generations>1)return {status:200,body:{generation:'restored-generation'}}
    return call(...args)
  })
  await expect(f.make().execute({path:'/v1/parent/deletions/job-a/retry',body:{confirmation:'确认删除'}},'parent-token')).rejects.toThrow('NATIVE_PRIVACY_GENERATION_CHANGED')
  expect(f.recovery.eraseChild).not.toHaveBeenCalled();expect(f.drafts.isBlocked('a')).toBe(true)
})
it('committed all_history replay erases local cache without touching a new controlled intent',async()=>{
  const f=fixture('all_history');f.setStatus('committed')
  const scope={childId:'a',sessionId:'new-session',kind:'video' as const,requestId:'new-resource'}
  const intent=f.journal.prepare(scope)
  await f.make().execute(f.command,'parent-token')
  expect(f.journal.list(scope)[0]).toEqual(intent);expect(f.transport.closeLegacy).not.toHaveBeenCalled()
  expect(f.recovery.eraseChild).toHaveBeenCalledWith('a')
})
it('native committed cleanup failure leaves both barriers for restart, with no duplicate DELETE',async()=>{
  const f=fixture('all_history');f.failErase()
  await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('SYNTHETIC_ERASE_FAILURE')
  expect(f.journal.activePrivacy()).not.toBeNull();expect(f.drafts.isBlocked('a')).toBe(true)
  f.failErase(false);await f.make().recover('original-delete','parent-token')
  expect(f.journal.activePrivacy()).toBeNull();expect(f.drafts.active()).toBeNull()
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(1)
})
it('mismatched scope cannot use recover to clear another operation fence',async()=>{
  const f=fixture();f.lose();await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('LOST_RESPONSE')
  await expect(f.make().recover('foreign-request','parent-token')).rejects.toThrow('DRAFT_PRIVACY_SCOPE_CHANGED')
  expect(f.drafts.isBlocked('a')).toBe(true);expect(f.recovery.eraseChild).not.toHaveBeenCalled()
})
it.each(['all_history','restore','portable_restore'])('%s erases only after the original native flow commits',async(kind)=>{
  const f=fixture(kind==='all_history'?kind:'knowledge')
  if(kind!=='all_history'){f.command.path=kind==='restore'?'/v1/parent/backups/restore':'/v1/parent/backups/portable/restore';f.command.body.scope=null as any}
  await f.make().execute(f.command,'parent-token')
  expect(f.transport.permit).toHaveBeenCalled()
  if(kind==='all_history'){expect(f.recovery.eraseChild).toHaveBeenCalledWith('a');expect(existsSync(join(f.capsules,'b-capture'))).toBe(true)}
  else{expect(f.recovery.eraseAll).toHaveBeenCalledOnce();expect(existsSync(join(f.capsules,'b-capture'))).toBe(false)}
  expect(f.drafts.active()).toBeNull()
})
it('all_history finalization_required keeps both native and local barriers until committed',async()=>{
  const f=fixture('all_history');f.finalize(true)
  await f.make().execute(f.command,'parent-token')
  expect(f.recovery.eraseChild).not.toHaveBeenCalled();expect(f.journal.activePrivacy()).not.toBeNull();expect(f.drafts.isPrivacyBlocked('a')).toBe(true)
  f.finalize(false);await f.make().recover('original-delete','parent-token')
  expect(f.journal.activePrivacy()).toBeNull();expect(f.drafts.active()).toBeNull()
})
it('unrelated learning/media scopes preserve local draft capsules',async()=>{
  const f=fixture('learning')
  f.transport.call.mockResolvedValue({status:200,body:{status:'completed'}})
  await f.make().execute(f.command,'parent-token')
  expect(f.recovery.eraseChild).not.toHaveBeenCalled();expect(f.recovery.revokeChild).not.toHaveBeenCalled()
})
it('durable fence commit failure prevents destructive POST and old writer revival',async()=>{
  const f=fixture(),late=f.lease();const broken=new LocalDraftPrivacy(f.root,f.recovery,()=>{throw Error('SYNTHETIC_DISK_FULL')})
  await expect(new NativePrivacyCoordinator(f.journal,f.transport,broken).execute(f.command,'parent-token')).rejects.toThrow('DRAFT_PRIVACY_IO')
  expect(f.transport.call).not.toHaveBeenCalled();expect(()=>late()).toThrow('REVOKED')
  expect(existsSync(join(f.capsules,'a-capture'))).toBe(true)
})
it('lost local COMMIT acknowledgment retains a durable barrier without sending backend mutation',async()=>{
  const f=fixture(),broken=new LocalDraftPrivacy(f.root,f.recovery,point=>{if(point==='after-commit')throw Error('SYNTHETIC_ACK_LOSS')})
  await expect(new NativePrivacyCoordinator(f.journal,f.transport,broken).execute(f.command,'parent-token')).rejects.toThrow('DRAFT_PRIVACY_IO')
  expect(f.transport.call).not.toHaveBeenCalled();expect(new LocalDraftPrivacy(f.root,f.recovery).isBlocked('a')).toBe(true)
  expect(existsSync(join(f.capsules,'a-capture'))).toBe(true)
})
it('fence rejects symlink/hardlink aliases and corrupt stored metadata',()=>{
  const f=fixture(),file=join(f.root,'parent-draft-privacy','barrier.sqlite')
  linkSync(file,join(f.root,'alias'));expect(()=>f.drafts.active()).toThrow('DRAFT_PRIVACY_UNSAFE_PATH')
  rmSync(join(f.root,'alias'));rmSync(file);writeFileSync(file,'not-a-sqlite-file')
  expect(()=>new LocalDraftPrivacy(f.root,f.recovery)).toThrow('DRAFT_PRIVACY_IO')
  rmSync(file);symlinkSync(join(f.root,'other'),file);expect(()=>new LocalDraftPrivacy(f.root,f.recovery)).toThrow('DRAFT_PRIVACY')
})
