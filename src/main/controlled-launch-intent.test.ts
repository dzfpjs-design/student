// @vitest-environment node
import {mkdtemp,rm} from 'node:fs/promises'
import {join} from 'node:path'
import {tmpdir} from 'node:os'
import {expect,it,vi} from 'vitest'
import {ControlledLaunchIntents} from './controlled-launch-intent'
import {queryProcessIdentity} from './game-process-identity'
import {spawn} from 'node:child_process'
import {buildSync} from 'esbuild'
import {DatabaseSync} from 'node:sqlite'

const scope={childId:'child-a',sessionId:'session-a',kind:'video' as const,requestId:'request-original'}
it('lost response and main restart cancel only a pre-dispatch original intent; late open cannot revive',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-start-intent-'))
  let receipt:any=null
  const read=vi.fn(async()=>receipt),open=vi.fn(async()=>({opened:true})),close=vi.fn(async()=>({closed:false,proof:'unknown'}))
  const drivers={read,close,hasResource:async()=>false}
  try{
    const first=new ControlledLaunchIntents(root,drivers),intent=first.prepare(scope)
    expect(first.prepare(scope).intentId).toBe(intent.intentId)
    receipt={...scope,intentId:intent.intentId,activityId:'activity-original',status:'active'}
    const restarted=new ControlledLaunchIntents(root,drivers)
    const proof=await restarted.cancel(intent.intentId)
    expect(proof).toMatchObject({closed:true,sessionId:'activity-original',proof:'cancelled_before_dispatch'})
    await expect(first.dispatch(intent.intentId,'activity-original',open)).rejects.toThrow('CANCELLED')
    expect(open).not.toHaveBeenCalled();expect(close).not.toHaveBeenCalled()
    expect(await restarted.closeActivity('video','invented')).toBeNull()
    expect(restarted.list({...scope,childId:'other'})).toEqual([])
    receipt={...receipt,status:'stopped'};await restarted.finish(intent.intentId)
    expect(restarted.list(scope)).toEqual([])
    await expect(restarted.dispatch(intent.intentId,'activity-original',open)).rejects.toThrow()
  }finally{await rm(root,{recursive:true,force:true})}
})
it('cancelling before backend response keeps tombstone; a later exact commit is recoverable without launch',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-start-late-'));let receipt:any=null
  const drivers={read:async()=>receipt,close:async()=>({closed:false}),hasResource:async()=>false}
  try{
    const ledger=new ControlledLaunchIntents(root,drivers),intent=ledger.prepare(scope)
    expect(await ledger.cancel(intent.intentId)).toMatchObject({closed:false,proof:'unknown'})
    receipt={...scope,intentId:intent.intentId,activityId:'late-original',status:'active'}
    expect(await new ControlledLaunchIntents(root,drivers).cancel(intent.intentId)).toMatchObject({closed:true,sessionId:'late-original'})
    expect(()=>ledger.prepare({...scope,requestId:'different'})).toThrow('UNRESOLVED')
  }finally{await rm(root,{recursive:true,force:true})}
})
it('dispatch claimed before cancellation needs actual resource close, never absence or a live-owner guess',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-start-race-'));let receipt:any=null,release!:()=>void
  const close=vi.fn(async()=>({closed:false,proof:'unknown'}))
  const ledger=new ControlledLaunchIntents(root,{read:async()=>receipt,close,hasResource:async()=>false})
  try{
    const intent=ledger.prepare(scope);receipt={...scope,intentId:intent.intentId,activityId:'a',status:'active'}
    const started=ledger.dispatch(intent.intentId,'a',()=>new Promise<void>(resolve=>{release=resolve}))
    await vi.waitFor(()=>expect(release).toBeTypeOf('function'))
    expect(await ledger.cancel(intent.intentId)).toMatchObject({closed:false,proof:'unknown'})
    release();await started
    await expect(ledger.dispatch(intent.intentId,'a',async()=>{})).rejects.toThrow()
    const owner=(await queryProcessIdentity(process.pid))!
    const recovered=new ControlledLaunchIntents(root,{read:async()=>receipt,close,hasResource:async()=>false,queryOwner:async()=>({...owner,birth:owner.birth+'-reused'})})
    expect(await recovered.cancel(intent.intentId)).toMatchObject({closed:true,proof:'owner_exit_before_registration'})
  }finally{await rm(root,{recursive:true,force:true})}
})
it('foreign or malformed backend receipt and source unknown never produce cancellation proof',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-start-scope-'));let receipt:any=null
  const ledger=new ControlledLaunchIntents(root,{read:async()=>receipt,close:async()=>({closed:false}),hasResource:async()=>false})
  try{
    const intent=ledger.prepare(scope)
    receipt={...scope,childId:'other',intentId:intent.intentId,activityId:'a',status:'active'}
    await expect(ledger.cancel(intent.intentId)).rejects.toThrow('BINDING')
    expect(ledger.list(scope)[0].activityId).toBeNull()
  }finally{await rm(root,{recursive:true,force:true})}
})
it('real owner dies between durable dispatch claim and resource registration; original scope recovers',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-start-owner-'))
  buildSync({entryPoints:[join(process.cwd(),'src/main/controlled-launch-intent.ts')],bundle:true,platform:'node',format:'esm',outfile:join(root,'intent.mjs')})
  const code=`import{ControlledLaunchIntents}from ${JSON.stringify('file://'+join(root,'intent.mjs'))};
    const scope=${JSON.stringify(scope)};let receipt=null;
    const c=new ControlledLaunchIntents(${JSON.stringify(root)},{read:async()=>receipt,close:async()=>({closed:false}),hasResource:async()=>false});
    const intent=c.prepare(scope);receipt={...scope,intentId:intent.intentId,activityId:'owned-crash',status:'active'};
    await c.dispatch(intent.intentId,'owned-crash',()=>new Promise(()=>{process.send({intentId:intent.intentId});setInterval(()=>{},1000)}));`
  const child=spawn(process.execPath,['--input-type=module','-e',code],{stdio:['ignore','ignore','pipe','ipc']})
  try{
    const {intentId}=await new Promise<{intentId:string}>((resolve,reject)=>{child.once('message',value=>resolve(value as {intentId:string}));child.once('exit',code=>reject(Error('owner exited '+code)))})
    const binding={...scope,intentId,activityId:'owned-crash',status:'active'}
    const recovered=new ControlledLaunchIntents(root,{read:async()=>binding,close:async()=>({closed:false}),hasResource:async()=>false})
    expect(await recovered.cancel(intentId)).toMatchObject({closed:false})
    child.kill('SIGKILL');await new Promise(resolve=>child.once('exit',resolve))
    expect(await recovered.cancel(intentId)).toMatchObject({closed:true,sessionId:'owned-crash',proof:'owner_exit_before_registration'})
    await expect(recovered.dispatch(intentId,'owned-crash',async()=>{})).rejects.toThrow('CANCELLED')
  }finally{if(child.exitCode===null&&child.signalCode===null){child.kill('SIGKILL');await new Promise(resolve=>child.once('exit',resolve))}await rm(root,{recursive:true,force:true})}
})
it('failed SQLite cancellation rolls back, same intent retries; confirmed backend no-start releases reservation',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-start-rollback-'));const cancelMissing=vi.fn().mockResolvedValue(true)
  const c=new ControlledLaunchIntents(root,{read:async()=>null,cancelMissing,close:async()=>({closed:false}),hasResource:async()=>false})
  try{
    const intent=c.prepare(scope),db=new DatabaseSync(join(root,'intents.sqlite'))
    try{
      db.exec("CREATE TRIGGER deny BEFORE UPDATE ON launch_intent BEGIN SELECT RAISE(ABORT,'INJECT_WRITE_FAILURE'); END")
      await expect(c.cancel(intent.intentId)).rejects.toThrow('INJECT_WRITE_FAILURE')
      expect(c.list(scope)[0].state).toBe('prepared');expect(cancelMissing).not.toHaveBeenCalled()
      db.exec('DROP TRIGGER deny')
      expect(await c.cancel(intent.intentId)).toMatchObject({closed:true,noActivity:true,proof:'cancelled_before_start'})
      expect(c.unresolved()).toBe(false)
      expect(c.prepare({...scope,requestId:'new-request'}).intentId).not.toBe(intent.intentId)
    }finally{db.close()}
  }finally{await rm(root,{recursive:true,force:true})}
})
