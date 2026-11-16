// @vitest-environment node
import {mkdtemp,rm} from 'node:fs/promises'
import {join} from 'node:path'
import {tmpdir} from 'node:os'
import {expect,it} from 'vitest'
import {ControlledLaunchIntents} from './controlled-launch-intent'

it('out-of-protocol owner deletion is not closure evidence and still blocks unverified native reuse',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-launch-delete-'))
  const scope={childId:'a',sessionId:'session-a',kind:'video' as const,requestId:'original'}
  let binding:any=null
  try{
    const journal=new ControlledLaunchIntents(root,{read:async()=>binding,close:async()=>({closed:false}),hasResource:async()=>false,cancelMissing:async()=>{throw Error('OWNER_ALREADY_DELETED')}})
    const intent=journal.prepare(scope)
    binding={...scope,intentId:intent.intentId,activityId:'original-activity',status:'active'}
    // This bypass deliberately removes the owner without the new host preflight.
    // Current actual-host coverage instead proves settlement BEFORE deletion.
    binding=null
    await expect(journal.cancel(intent.intentId)).rejects.toThrow('OWNER_ALREADY_DELETED')
    expect(()=>journal.prepare({...scope,childId:'other',sessionId:'other-session',requestId:'new'})).toThrow('UNRESOLVED')
  }finally{await rm(root,{recursive:true,force:true})}
})
