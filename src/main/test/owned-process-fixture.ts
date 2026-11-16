import {readFile} from 'node:fs/promises'
import {performance} from 'node:perf_hooks'
import {queryProcessIdentity, sameProcessIdentity, type ProcessIdentity} from '../game-process-identity'

const sleep=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms))
const alive=(pid:number)=>{try{process.kill(pid,0);return true}catch(error){if((error as NodeJS.ErrnoException).code==='ESRCH')return false;throw error}}

export async function ownedIdentity(pid:number):Promise<ProcessIdentity>{
  const identity=await queryProcessIdentity(pid)
  if(!identity)throw Error(`OWNED_FIXTURE_EXITED_BEFORE_TRACKING pid=${pid}`)
  return identity
}

export async function waitForOwnedReady(path:string,identity:ProcessIdentity,timeoutMs=7000):Promise<number>{
  const start=performance.now()
  while(true){
    try{
      const content=await readFile(path,'utf8')
      if(content==='ready'){
        if(!alive(identity.pid))throw Error(`OWNED_FIXTURE_EXITED_BEFORE_READY pid=${identity.pid}`)
        return performance.now()-start
      }
      // Creation/truncation is visible before a non-atomic sentinel write ends.
      if(!'ready'.startsWith(content))throw Error(`OWNED_FIXTURE_INVALID_READY pid=${identity.pid}`)
    }catch(error){if((error as NodeJS.ErrnoException).code!=='ENOENT')throw error}
    if(!alive(identity.pid))throw Error(`OWNED_FIXTURE_EXITED_BEFORE_READY pid=${identity.pid}`)
    const remaining=timeoutMs-(performance.now()-start)
    if(remaining<=0){
      const current=await queryProcessIdentity(identity.pid)
      throw Error(`OWNED_FIXTURE_READY_TIMEOUT pid=${identity.pid} elapsed_ms=${Math.round(performance.now()-start)} alive=${!!current} same_identity=${!!current&&sameProcessIdentity(current,identity)}`)
    }
    await sleep(Math.min(25,remaining))
  }
}

export async function cleanupOwnedProcesses(identities:ReadonlyArray<ProcessIdentity>):Promise<void>{
  // Test-only force cleanup, never a production close receipt. Do not signal a
  // PID that has since been reused; wait for every exact owned process to exit.
  const errors:unknown[]=[]
  for(const identity of identities){
    try{
      const current=await queryProcessIdentity(identity.pid)
      if(!current||!sameProcessIdentity(current,identity))continue
      try{process.kill(identity.pid,'SIGKILL')}catch(error){if((error as NodeJS.ErrnoException).code!=='ESRCH')throw error}
      const deadline=performance.now()+3000
      while(true){
        const after=await queryProcessIdentity(identity.pid)
        if(!after||!sameProcessIdentity(after,identity))break
        if(performance.now()>=deadline)throw Error(`OWNED_FIXTURE_CLEANUP_TIMEOUT pid=${identity.pid}`)
        await sleep(25)
      }
    }catch(error){errors.push(error)}
  }
  if(errors.length)throw new AggregateError(errors,'Owned process cleanup failed; fixture retained')
}
