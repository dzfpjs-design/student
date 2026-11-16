// @vitest-environment node
import {mkdtemp,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {afterEach,expect,it,vi} from 'vitest'
import {ControlledLaunchIntents} from './controlled-launch-intent'
import {NativePrivacyCoordinator} from './native-privacy'
import type {NativePrivacyMetadata} from '../shared/nativePrivacy'
const roots:string[]=[]
afterEach(async()=>{for(const root of roots.splice(0))await rm(root,{recursive:true,force:true})})
async function fixture(){
  const root=await mkdtemp(join(tmpdir(),'hsc-native-privacy-coordinator-'));roots.push(root)
  const scope={childId:'a',sessionId:'session-a',kind:'video' as const,requestId:'original-start'}
  const drivers={read:async()=>null,close:async()=>({closed:false}),hasResource:async()=>false,cancelMissing:async()=>true}
  const journal=new ControlledLaunchIntents(root,drivers)
  const operation={request_id:'original-delete',expected_generation:'generation',cleanup_manifest_sha256:'a'.repeat(64)}
  const command={path:'/v1/parent/children/a/delete',body:{scope:'all_history',confirmation:'确认删除',client_action_id:operation.request_id,operation}}
  let permitState='missing',originalState='missing',lose=false
  const call=vi.fn(async(path:string,body:unknown,token:string,metadata?:NativePrivacyMetadata)=>{
    expect(token).toBe('parent-token')
    if(path.endsWith('/generation'))return {status:200,body:{generation:'generation'}}
    if(path.includes('/privacy-operations/'))return {status:originalState==='missing'?404:200,body:{request_id:operation.request_id,status:originalState}}
    expect(path).toBe(command.path);expect(body).toEqual(command.body);expect(metadata?.request_id).toBe(operation.request_id)
    permitState='finished';originalState='committed'
    if(lose)throw Error('LOST_RESPONSE')
    return {status:200,body:{status:'completed'}}
  })
  const permit=vi.fn(async(metadata:NativePrivacyMetadata,_token:string,fence:boolean)=>{
    if(fence&&permitState==='missing'){permitState='cancelled';originalState='failed'}
    return {...metadata,status:permitState}
  })
  const closeLegacy=vi.fn(async()=>undefined),end=vi.fn(async()=>undefined)
  const transport={call,permit,closeLegacy,end}
  return {root,scope,drivers,journal,command,transport,lose:()=>{lose=true},setOriginal:(value:string)=>{originalState=value}}
}
it('settles original prepared intent before destructive POST and removes only the confirmed scope',async()=>{
  const f=await fixture();f.journal.prepare(f.scope)
  f.transport.closeLegacy.mockImplementation(async()=>{
    expect(f.journal.privacyIntents(f.journal.activePrivacy()!)).toEqual([])
    expect(()=>f.journal.prepare({...f.scope,childId:'b',requestId:'late'})).toThrow('PRIVACY')
  })
  const response=await new NativePrivacyCoordinator(f.journal,f.transport).execute(f.command,'parent-token')
  expect(response.status).toBe(200);expect(f.journal.activePrivacy()).toBeNull()
  expect(()=>f.journal.prepare(f.scope)).toThrow('CANCELLED')
  expect(f.journal.prepare({...f.scope,childId:'b',requestId:'new'}).state).toBe('prepared')
  expect(f.transport.end).not.toHaveBeenCalled()
})
it('lost response keeps barrier across main restart; recovery reads receipt, never POSTs again',async()=>{
  const f=await fixture();f.journal.prepare(f.scope);f.lose()
  await expect(new NativePrivacyCoordinator(f.journal,f.transport).execute(f.command,'parent-token')).rejects.toThrow('LOST_RESPONSE')
  const restarted=new ControlledLaunchIntents(f.root,f.drivers)
  expect(restarted.activePrivacy()?.request_id).toBe('original-delete')
  const result=await new NativePrivacyCoordinator(restarted,f.transport).recover('original-delete','parent-token')
  expect((result.body as {status:string}).status).toBe('committed')
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(1)
  expect(restarted.activePrivacy()).toBeNull()
})
it('unknown native close cancels only the destructive command; no delete or false closure',async()=>{
  const f=await fixture()
  f.transport.closeLegacy.mockRejectedValue(Error('NATIVE_PRIVACY_RESOURCE_UNCONFIRMED'))
  await expect(new NativePrivacyCoordinator(f.journal,f.transport).execute(f.command,'parent-token')).rejects.toThrow('UNCONFIRMED')
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(0)
  expect(f.transport.permit.mock.calls.some(c=>c[2]===true)).toBe(true)
  expect(f.transport.end).not.toHaveBeenCalled()
})
it('replaying a committed operation does not close a newly started resource',async()=>{
  const f=await fixture();f.setOriginal('committed');const original=f.journal.prepare(f.scope)
  await new NativePrivacyCoordinator(f.journal,f.transport).execute(f.command,'parent-token')
  expect(f.transport.closeLegacy).not.toHaveBeenCalled()
  expect(f.journal.list(f.scope)[0]).toEqual(original)
})
it('another child settled evidence survives a scoped deletion',async()=>{
  const f=await fixture(),other={...f.scope,childId:'b',requestId:'other'}
  const old=f.journal.prepare(other);await f.journal.cancel(old.intentId)
  await new NativePrivacyCoordinator(f.journal,f.transport).execute(f.command,'parent-token')
  expect(f.journal.prepare(other).intentId).toBe(old.intentId)
})
it('missing finalization retains native cleanup until the exact original receipt is committed',async()=>{
  const f=await fixture();f.journal.prepare(f.scope);f.lose()
  const coordinator=new NativePrivacyCoordinator(f.journal,f.transport)
  await expect(coordinator.execute(f.command,'parent-token')).rejects.toThrow('LOST_RESPONSE')
  f.setOriginal('finalization_required')
  expect((await coordinator.recover('original-delete','parent-token')).body).toMatchObject({status:'finalization_required'})
  expect(f.journal.activePrivacy()).not.toBeNull()
  f.setOriginal('committed')
  await coordinator.recover('original-delete','parent-token')
  expect(f.journal.activePrivacy()).toBeNull()
  expect(f.transport.call.mock.calls.filter(c=>c[0]===f.command.path)).toHaveLength(1)
})
it('bound prepared activity receives positive cancellation and exact settlement before its source disappears',async()=>{
  const f=await fixture()
  let binding:import('../shared/controlledLaunch').ControlledLaunchReceipt|null=null
  const journal=new ControlledLaunchIntents(f.root,{...f.drivers,read:async()=>binding,
    confirmNoDispatch:async(original,proof)=>{expect(proof).toBe('cancelled_before_dispatch');binding={...original,status:'stopped'};return binding}})
  const intent=journal.prepare(f.scope)
  binding={...f.scope,intentId:intent.intentId,activityId:'original-activity',status:'active'}
  f.transport.end.mockImplementation(async()=>{expect(binding?.status).toBe('stopped')})
  f.transport.closeLegacy.mockImplementation(async()=>{
    expect(f.transport.end).toHaveBeenCalledWith(expect.objectContaining({activityId:'original-activity',status:'stopped'}))
    expect(journal.privacyIntents(journal.activePrivacy()!)).toEqual([])
    // The real destructive transaction may now remove its FK-bound source.
    binding=null
  })
  await new NativePrivacyCoordinator(journal,f.transport).execute(f.command,'parent-token')
  expect(journal.prepare({...f.scope,childId:'b',requestId:'different'}).state).toBe('prepared')
  expect(()=>journal.prepare(f.scope)).toThrow('CANCELLED')
})
