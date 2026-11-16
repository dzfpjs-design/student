// @vitest-environment node
import {mkdtemp,rm} from 'node:fs/promises'
import {join} from 'node:path'
import {tmpdir} from 'node:os'
import {it,expect,vi} from 'vitest'
import {ControlledLaunchIntents} from './controlled-launch-intent'

it('exact request cancellation survives lost outcome and restart, while unknown and different scope stay fenced',async()=>{
 const root=await mkdtemp(join(tmpdir(),'hsc-cancel-replay-'))
 const scope={childId:'child',sessionId:'session',kind:'video' as const,requestId:'original'}
 let available=false
 const cancelMissing=vi.fn(async()=>available)
 const drivers={read:async()=>null,cancelMissing,hasResource:async()=>false,close:vi.fn(async()=>({closed:false}))}
 try{
  const first=new ControlledLaunchIntents(root,drivers),intent=first.prepare(scope)
  expect(first.list({...scope,requestId:'wrong-original'})).toEqual([])
  expect(first.list({...scope,requestId:'wrong-original',includeSettled:true})).toEqual([])
  expect(first.list(scope).map(r=>r.intentId)).toEqual([intent.intentId])
  expect(await first.cancel(intent.intentId)).toEqual({closed:false,proof:'unknown'})
  expect(()=>first.prepare({...scope,requestId:'new'})).toThrow('UNRESOLVED')
  available=true
  expect(await first.cancel(intent.intentId)).toMatchObject({closed:true,noActivity:true,proof:'cancelled_before_start'})
  const restart=new ControlledLaunchIntents(root,drivers)
  expect(restart.list({...scope,requestId:undefined})).toEqual([])
  expect(restart.list(scope)).toEqual([])
  expect(restart.list({...scope,includeSettled:true})).toHaveLength(1)
  expect(restart.list({...scope,requestId:'wrong-original',includeSettled:true})).toEqual([])
  expect(restart.list({...scope,requestId:'wrong-original'})).toEqual([])
  expect(restart.list({...scope,childId:'other'})).toEqual([])
  expect(await restart.cancel(intent.intentId)).toMatchObject({closed:true,noActivity:true,proof:'cancelled_before_start'})
  available=false
  expect(await restart.cancel(intent.intentId)).toEqual({closed:false,proof:'unknown'})
  expect(drivers.close).not.toHaveBeenCalled()
 }finally{await rm(root,{recursive:true,force:true})}
})
