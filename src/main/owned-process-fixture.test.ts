// @vitest-environment node
import {spawn, type ChildProcess} from 'node:child_process'
import {mkdtemp,rm,writeFile} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {performance} from 'node:perf_hooks'
import {expect,it} from 'vitest'
import {cleanupOwnedProcesses,ownedIdentity,waitForOwnedReady} from './test/owned-process-fixture'
import {queryProcessIdentity} from './game-process-identity'

async function fixture(delay:number){
  const root=await mkdtemp(join(tmpdir(),'hsc-ready-budget-')),ready=join(root,'ready')
  const child=spawn(process.execPath,['-e',`const fs=require('node:fs');process.on('SIGTERM',()=>{});setTimeout(()=>fs.writeFileSync(${JSON.stringify(ready)},'ready'),${delay});setInterval(()=>{},1000);process.send('booted')`],{stdio:['ignore','ignore','pipe','ipc']})
  let stderr='';child.stderr!.on('data',v=>stderr+=v)
  const booted=await new Promise<boolean>((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('FIXTURE_BOOT_TIMEOUT '+stderr)),5000)
    child.once('message',()=>{clearTimeout(timer);resolve(true)})
    child.once('exit',()=>{clearTimeout(timer);reject(Error('FIXTURE_BOOT_EXIT '+stderr))})
  }).catch(async error=>{child.kill('SIGKILL');await exit(child);await rm(root,{recursive:true,force:true});throw error})
  expect(booted).toBe(true)
  const identity=await ownedIdentity(child.pid!)
  return{root,ready,child,identity,cleanup:async()=>{
    await cleanupOwnedProcesses([identity]);await exit(child)
    expect(await queryProcessIdentity(identity.pid)).toBeNull()
    await rm(root,{recursive:true,force:true})
  }}
}
async function exit(child:ChildProcess){
  if(child.exitCode!==null||child.signalCode!==null)return
  await new Promise<void>((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('FIXTURE_EXIT_TIMEOUT')),3000);child.once('exit',()=>{clearTimeout(timer);resolve()})})
}

it.skipIf(process.platform==='win32')('same real delayed child fails the old 3s budget then becomes ready within 7s, without a relaunch',async()=>{
  const f=await fixture(3600)
  try{
    await expect(waitForOwnedReady(f.ready,f.identity,3000)).rejects.toThrow('OWNED_FIXTURE_READY_TIMEOUT')
    await waitForOwnedReady(f.ready,f.identity)
    expect(await queryProcessIdentity(f.identity.pid)).toEqual(f.identity)
  }finally{await f.cleanup()}
},12000)

it.skipIf(process.platform==='win32')('a genuinely hung owned process fails bounded readiness and is fully reaped even when SIGTERM is ignored',async()=>{
  const f=await fixture(60000),start=performance.now()
  try{
    await expect(waitForOwnedReady(f.ready,f.identity,150)).rejects.toThrow(/OWNED_FIXTURE_READY_TIMEOUT.*alive=true.*same_identity=true/)
    expect(performance.now()-start).toBeLessThan(2000)
  }finally{await f.cleanup()}
},10000)

it.skipIf(process.platform==='win32')('an exited child is an explicit failure, never a successful or endlessly retried ready read',async()=>{
  const f=await fixture(60000)
  try{
    f.child.kill('SIGKILL');await exit(f.child)
    await expect(waitForOwnedReady(f.ready,f.identity)).rejects.toThrow('OWNED_FIXTURE_EXITED_BEFORE_READY')
  }finally{await f.cleanup()}
},10000)

it.skipIf(process.platform==='win32').each(['','rea'])('waits within the original budget for an incomplete ready sentinel (%j), never accepts it',async content=>{
  const f=await fixture(60000)
  try{
    await writeFile(f.ready,content)
    await expect(waitForOwnedReady(f.ready,f.identity,100)).rejects.toThrow('OWNED_FIXTURE_READY_TIMEOUT')
  }finally{await f.cleanup()}
},10000)

it.skipIf(process.platform==='win32')('accepts the same owned process only after the full ready sentinel arrives',async()=>{
  const f=await fixture(60000)
  let writer:ReturnType<typeof setTimeout>|undefined
  try{
    await writeFile(f.ready,'rea')
    const completed=new Promise<void>((resolve,reject)=>{writer=setTimeout(()=>{void writeFile(f.ready,'ready').then(resolve,reject)},50)})
    await Promise.all([waitForOwnedReady(f.ready,f.identity),completed])
    expect(await queryProcessIdentity(f.identity.pid)).toEqual(f.identity)
  }finally{clearTimeout(writer);await f.cleanup()}
},10000)

it.skipIf(process.platform==='win32')('rejects a corrupt sentinel rather than treating arbitrary content as readiness',async()=>{
  const f=await fixture(60000)
  try{
    await writeFile(f.ready,'corrupt')
    await expect(waitForOwnedReady(f.ready,f.identity)).rejects.toThrow('OWNED_FIXTURE_INVALID_READY')
  }finally{await f.cleanup()}
},10000)
