// @vitest-environment node
import {mkdtemp,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {expect,it} from 'vitest'
import {ControlledLaunchIntents} from './controlled-launch-intent'
const scope={childId:'a',sessionId:'session-a',kind:'video' as const,requestId:'request-a'}
const metadata={nonce:'native-nonce',request_id:'parent-operation',expected_generation:'generation',kind:'delete' as const,
  child_id:'a',scope:'all_history',command_sha256:'a'.repeat(64),cleanup_manifest_sha256:'b'.repeat(64)}
const drivers={read:async()=>null,close:async()=>({closed:false}),hasResource:async()=>false,cancelMissing:async()=>true}
it('persists barrier across restart and blocks both new prepare and already-prepared dispatch',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-privacy-barrier-'))
  try{
    const journal=new ControlledLaunchIntents(root,drivers)
    const original=journal.prepare(scope)
    journal.beginPrivacy(metadata)
    const restarted=new ControlledLaunchIntents(root,drivers)
    expect(restarted.activePrivacy()).toEqual(metadata)
    expect(()=>restarted.prepare({...scope,childId:'b',requestId:'new'})).toThrow('PRIVACY')
    await expect(restarted.dispatch(original.intentId,'original',async()=>true)).rejects.toThrow('PRIVACY')
    expect(()=>restarted.beginPrivacy({...metadata,nonce:'other'})).toThrow('PRIVACY')
    await restarted.cancel(original.intentId)
    restarted.finishPrivacy(metadata,true)
    expect(restarted.activePrivacy()).toBeNull()
    expect(()=>restarted.assertLegacyDispatch('delayed-open-with-marker-removed')).toThrow('INTENT_REQUIRED')
    expect(restarted.list(scope)).toEqual([])
    expect(()=>restarted.prepare(scope)).toThrow('CANCELLED')
    expect(restarted.prepare({...scope,childId:'b',sessionId:'session-b',requestId:'new'}).state).toBe('prepared')
  }finally{await rm(root,{recursive:true,force:true})}
})
it('cannot clear an unresolved intent after deletion; failed operation keeps original settled history',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-privacy-barrier-'))
  try{
    const journal=new ControlledLaunchIntents(root,drivers)
    const original=journal.prepare(scope)
    journal.beginPrivacy(metadata)
    expect(()=>journal.finishPrivacy(metadata,true)).toThrow('UNSETTLED')
    await journal.cancel(original.intentId)
    journal.finishPrivacy(metadata,false)
    expect(journal.prepare(scope).intentId).toBe(original.intentId)
    expect(journal.prepare(scope).state).toBe('settled')
  }finally{await rm(root,{recursive:true,force:true})}
})
