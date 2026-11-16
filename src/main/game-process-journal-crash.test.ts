// @vitest-environment node
import { spawn, type ChildProcess } from 'node:child_process'
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomBytes, randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { build } from 'esbuild'
import { expect, it, vi } from 'vitest'
import { GameProcessJournal, type GameRecord } from './game-process-journal'
const record=():GameRecord=>({schema:1,revision:1,sessionId:'owned',instanceId:randomUUID(),token:randomBytes(32).toString('hex'),state:'reserved',launchIdentity:{pathHash:'a'.repeat(64),fileId:'fixture'},deadline:Date.now()+60000,supervisor:null,process:null,port:null,exitProof:null,reason:null})
const waitExit=(p:ChildProcess)=>new Promise<void>(resolve=>{if(p.exitCode!==null||p.signalCode!==null)resolve();else p.once('exit',()=>resolve())})
it.each(['node','electron'].flatMap(runtime=>['before_rename','after_rename','reserve_before_rename','reserve_after_rename'].map(point=>({runtime,point}))))('writer killed $point ($runtime) releases its OS lock without time-based stealing',async ({runtime,point})=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-journal-writer-crash-')),entry=join(root,'writer.cjs'),ready=join(root,'ready'),dir=join(root,'journal')
  let child:ChildProcess|undefined
  try{
    const journal=new GameProcessJournal(dir),original=record(),reserving=point.startsWith('reserve_')
    if(!reserving)journal.reserve(original)
    const bundled=join(root,'journal.cjs')
    await build({entryPoints:['src/main/game-process-journal.ts'],bundle:true,platform:'node',format:'cjs',outfile:bundled,logLevel:'silent'})
    // Fault injection stops the real writer while its native OS lock is held.
    // Only this owned child is killed; the production mutex itself is unchanged.
    await writeFile(entry,`const fs=require('node:fs');const {GameProcessJournal}=require(${JSON.stringify(bundled)});const j=new GameProcessJournal(${JSON.stringify(dir)});const rename=fs.renameSync;fs.renameSync=(a,b)=>{if(${JSON.stringify(point)}.endsWith('after_rename'))rename(a,b);fs.writeFileSync(${JSON.stringify(ready)},'locked');Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0);};${reserving?`j.reserve(${JSON.stringify(original)});`:"j.update(j.read('owned'),{state:'launching'});"}`)
    const binary=runtime==='electron'?createRequire(import.meta.url)('electron') as string:process.execPath
    child=spawn(binary,[entry],{stdio:'ignore',env:{PATH:process.env.PATH,SystemRoot:process.env.SystemRoot,ELECTRON_RUN_AS_NODE:'1'}})
    await vi.waitFor(async()=>expect(await readFile(ready,'utf8')).toBe('locked'),{timeout:5000})
    expect(journal.hasUnresolvedLease()).toBe(true)
    expect(()=>journal.update(original,{reason:'must_not_steal'})).toThrow('GAME_JOURNAL_BUSY')
    expect(journal.read('owned')?.reason??null).toBeNull()
    child.kill('SIGKILL');await waitExit(child)
    const current=new GameProcessJournal(dir)
    if(point==='reserve_before_rename'){
      expect(current.read('owned')).toBeNull()
      current.reserve(original)
    }else expect(current.read('owned')?.state).toBe(point==='after_rename'?'launching':'reserved')
    const recovered=current.update(current.read('owned')!,{state:'closed',token:null,exitProof:'launch_failed'})
    expect(recovered.state).toBe('closed')
    expect(current.hasUnresolvedLease()).toBe(false)
    expect(()=>current.update(original,{state:'launching'})).toThrow('GAME_JOURNAL_CONFLICT')
  }finally{if(child&&child.exitCode===null&&child.signalCode===null){child.kill('SIGKILL');await waitExit(child)}await rm(root,{recursive:true,force:true})}
},15000)
