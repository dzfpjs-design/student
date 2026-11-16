// @vitest-environment node
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { request } from 'node:http'
import { build } from 'esbuild'
import { expect, it, vi } from 'vitest'
import { DurableGameController } from './controlled-game-durable'
import { GameProcessJournal } from './game-process-journal'
import { queryProcessIdentity, type ProcessIdentity } from './game-process-identity'
import {cleanupOwnedProcesses,waitForOwnedReady} from './test/owned-process-fixture'

const probe=(port:number,bearer:string|undefined)=>new Promise<number>((resolve,reject)=>{
  const req=request({host:'127.0.0.1',port,path:'/status',method:'GET',headers:bearer===undefined?{}:{Authorization:bearer},timeout:2000},res=>{res.resume();res.on('end',()=>resolve(res.statusCode!))})
  req.on('error',reject);req.on('timeout',()=>req.destroy(Error('PROBE_TIMEOUT')));req.end()
})
it.skipIf(process.platform==='win32')('malformed Latin1 bearer gets 403 without killing the real supervisor or its owned game; authorized close still persists',async()=>{
  const root=await mkdtemp(join(tmpdir(),'hsc-supervisor-auth-')),directory=join(root,'journal'),supervisorPath=join(root,'supervisor.cjs'),path=join(root,'owned.exe'),ready=join(root,'ready'),signals=join(root,'signals')
  let controller:DurableGameController|undefined,owned:ProcessIdentity|null=null,supervisor:ProcessIdentity|null=null
  try{
    await build({entryPoints:['src/main/controlled-game-supervisor.ts'],bundle:true,platform:'node',format:'cjs',outfile:supervisorPath,logLevel:'silent'})
    await writeFile(path,`#!${process.execPath}\nconst fs=require('node:fs');process.on('SIGTERM',()=>{fs.appendFileSync(${JSON.stringify(signals)},'term\\n');process.exit(0)});fs.writeFileSync(${JSON.stringify(ready)},'ready');setInterval(()=>{},1000);\n`,{mode:0o700})
    controller=new DurableGameController(vi.fn(),{directory,supervisorPath,runtimeExecutable:process.execPath})
    await controller.open({path,title:'owned auth probe',maxSeconds:60,gameSessionId:'exact-original'})
    const row=new GameProcessJournal(directory).read('exact-original')!
    owned=row.process;supervisor=row.supervisor
    if(!owned||!supervisor)throw Error('OWNED_FIXTURE_MISSING_IDENTITY')
    await waitForOwnedReady(ready,owned)
    expect(row.token).toHaveLength(64)
    for(const bearer of [`Bearer ${'é'.repeat(64)}`,`Bearer ${'é'.repeat(32)}`,`Bearer ${'x'.repeat(64)}`,`Bearer short`,undefined]){
      expect(await probe(row.port!,bearer)).toBe(403)
      expect(await queryProcessIdentity(supervisor!.pid)).toEqual(supervisor)
      expect(await queryProcessIdentity(owned!.pid)).toEqual(owned)
    }
    expect(await probe(row.port!,`Bearer ${row.token}`)).toBe(200)
    await expect(readFile(signals)).rejects.toMatchObject({code:'ENOENT'})
    expect(await controller.close('child_requested','exact-original')).toEqual({closed:true,sessionId:'exact-original',proof:'observed_closed'})
    expect(await readFile(signals,'utf8')).toBe('term\n')
    expect(new GameProcessJournal(directory).confirmClosed('exact-original').exitProof).toBe('actual_exit')
  }catch(error){
    console.error(JSON.stringify({probeFailed:true,supervisorAlive:supervisor?!!await queryProcessIdentity(supervisor.pid):null,ownedGameAlive:owned?!!await queryProcessIdentity(owned.pid):null}))
    throw error
  }finally{
    const row=new GameProcessJournal(directory).read('exact-original')
    owned??=row?.process??null;supervisor??=row?.supervisor??null
    await controller?.close('test_cleanup').catch(()=>{});controller?.detach()
    // Test cleanup only: these identities came from this freshly spawned fixture.
    // Production recovery never uses this PID signaling fallback.
    await cleanupOwnedProcesses([owned,supervisor].filter((value):value is ProcessIdentity=>value!==null))
    await rm(root,{recursive:true,force:true})
  }
},20000)
