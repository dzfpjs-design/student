// @vitest-environment node
import {mkdtempSync,realpathSync,rmSync,writeFileSync,readFileSync} from 'node:fs'
import {join} from 'node:path'
import {tmpdir} from 'node:os'
import {DatabaseSync} from 'node:sqlite'
import {afterEach,expect,it,vi} from 'vitest'
import {ControlledLaunchIntents} from './controlled-launch-intent'
import {LocalDraftPrivacy} from './local-draft-privacy'
import {NativePrivacyCoordinator} from './native-privacy'
const roots:string[]=[]
afterEach(()=>{for(const root of roots.splice(0))rmSync(root,{recursive:true,force:true})})
function fixture(scope='knowledge'){
  const root=realpathSync(mkdtempSync(join(tmpdir(),'hsc-privacy-recover-')));roots.push(root)
  const capsule=join(root,'synthetic-human-work');writeFileSync(capsule,'KEEP-ORIGINAL-WORK')
  const recovery={revokeChild:vi.fn(),revokeAll:vi.fn(),eraseChild:vi.fn(()=>{rmSync(capsule,{force:true})}),eraseAll:vi.fn(()=>{rmSync(capsule,{force:true})})}
  const local=new LocalDraftPrivacy(root,recovery)
  const journal=new ControlledLaunchIntents(root,{read:async()=>null,close:async()=>({closed:false}),hasResource:async()=>false,cancelMissing:async()=>true})
  const operation={request_id:'original-request',expected_generation:'generation-1',cleanup_manifest_sha256:'a'.repeat(64)}
  const command={path:'/v1/parent/children/a/delete',body:{scope,operation,client_action_id:operation.request_id,confirmation:'确认删除'}}
  let generation='generation-1',original='missing',genStatus=200,lose=false,permitStatus='missing',erasing=false
  const receipt=()=>({request_id:operation.request_id,kind:'delete',child_id:'a',scope,origin_generation:'generation-1',current_generation:generation,result_generation:original==='committed'?generation:null,cleanup_manifest_sha256:operation.cleanup_manifest_sha256,status:original,cleanup_allowed:original==='committed',reference:null})
  const call=vi.fn(async(path:string,body:unknown,_token:string):Promise<{status:number;body:unknown}>=>{
    if(path.endsWith('/generation'))return {status:genStatus,body:{generation}}
    if(path.startsWith('/v1/parent/privacy-operations/'))return {status:original==='missing'?404:200,body:receipt()}
    if(path.endsWith('/native-context'))return {status:200,body:{child_id:'a',scope,native_required:scope==='all_history',operation:scope==='all_history'?operation:null}}
    expect(body).toEqual(command.body);original='committed';permitStatus='finished'
    if(lose)throw Error('SYNTHETIC_RESPONSE_LOST')
    return {status:200,body:{operation_receipt:receipt()}}
  })
  const transport={call,permit:vi.fn(async(m:any,_t:string,fence:boolean)=>{
    if(fence&&permitStatus==='missing'){permitStatus='cancelled';original='failed'}
    return {...m,status:permitStatus}
  }),end:vi.fn(async()=>undefined),closeLegacy:vi.fn(async()=>{if(erasing)throw Error('SYNTHETIC_CLOSE_FAILED')})}
  const make=(fence=local)=>new NativePrivacyCoordinator(journal,transport,fence)
  return {root,capsule,recovery,local,journal,command,transport,make,receipt,setGeneration:(v:string)=>{generation=v},genStatus:(v:number)=>{genStatus=v},setOriginal:(v:string)=>{original=v},lose:()=>{lose=true},failClose:()=>{erasing=true}}
}
it('generation mismatch before dispatch releases fence without erasing original human work',async()=>{
  const f=fixture();f.setGeneration('new-generation')
  await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('GENERATION_CHANGED')
  await f.make().recover('original-request','fresh-parent-token')
  expect(f.local.isBlocked('a')).toBe(false);expect(readFileSync(f.capsule,'utf8')).toBe('KEEP-ORIGINAL-WORK')
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(0)
})
it('401 cannot release; a fresh authenticated prepared 404 can recover after restart',async()=>{
  const f=fixture();f.genStatus(401);await f.make().execute(f.command,'expired-token')
  const restarted=new LocalDraftPrivacy(f.root,f.recovery)
  expect(restarted.isBlocked('a')).toBe(true)
  f.genStatus(200);await f.make(restarted).recover('original-request','fresh-parent-token')
  expect(restarted.isBlocked('a')).toBe(false);expect(f.recovery.eraseChild).not.toHaveBeenCalled()
})
it('fresh auth errors retain even a known pre-dispatch fence',async()=>{
  const f=fixture();f.genStatus(401);await f.make().execute(f.command,'expired-token')
  await f.make().recover('original-request','still-expired-token')
  expect(f.local.isBlocked('a')).toBe(true);expect(f.recovery.eraseChild).not.toHaveBeenCalled()
})
it('possibly dispatched 404 is ambiguous and must not release or erase after restart',async()=>{
  const f=fixture();f.lose();await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('RESPONSE_LOST')
  f.setOriginal('missing')
  const restarted=new LocalDraftPrivacy(f.root,f.recovery)
  await f.make(restarted).recover('original-request','fresh-parent-token')
  expect(restarted.isBlocked('a')).toBe(true);expect(f.recovery.eraseChild).not.toHaveBeenCalled()
})
it('matching original failed knowledge receipt releases without deleting drafts',async()=>{
  const f=fixture();f.setOriginal('failed');await f.make().execute(f.command,'parent-token')
  expect(f.local.isBlocked('a')).toBe(false);expect(f.recovery.eraseChild).not.toHaveBeenCalled()
  expect(readFileSync(f.capsule,'utf8')).toBe('KEEP-ORIGINAL-WORK')
})
it('original native failed/cancelled permit releases both barriers without local erase',async()=>{
  const f=fixture('all_history');f.failClose()
  await expect(f.make().execute(f.command,'parent-token')).rejects.toThrow('SYNTHETIC_CLOSE_FAILED')
  expect(f.journal.activePrivacy()).toBeNull();expect(f.local.isBlocked('a')).toBe(false)
  expect(f.recovery.eraseChild).not.toHaveBeenCalled();expect(readFileSync(f.capsule,'utf8')).toBe('KEEP-ORIGINAL-WORK')
})
it('R1 rows without dispatch state migrate conservatively to possibly-dispatched',async()=>{
  const f=fixture();f.genStatus(401);await f.make().execute(f.command,'expired-token')
  const path=join(f.root,'parent-draft-privacy','barrier.sqlite'),c=new DatabaseSync(path)
  try{c.exec('ALTER TABLE draft_privacy_fence RENAME TO old_fence; CREATE TABLE draft_privacy_fence(singleton INTEGER PRIMARY KEY CHECK(singleton=1),binding TEXT NOT NULL); INSERT INTO draft_privacy_fence SELECT singleton,binding FROM old_fence; DROP TABLE old_fence')}finally{c.close()}
  const restarted=new LocalDraftPrivacy(f.root,f.recovery);f.genStatus(200)
  await f.make(restarted).recover('original-request','fresh-parent-token')
  expect(restarted.isBlocked('a')).toBe(true);expect(f.recovery.eraseChild).not.toHaveBeenCalled()
})
