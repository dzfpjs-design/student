// @vitest-environment node
import { mkdtemp, readFile, writeFile, rm, readdir, chmod } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { build } from 'esbuild'
import { describe, expect, it, vi } from 'vitest'
import { DurableGameController } from './controlled-game-durable'
import {GameProcessJournal} from './game-process-journal'
import type {ProcessIdentity} from './game-process-identity'
import {cleanupOwnedProcesses,waitForOwnedReady} from './test/owned-process-fixture'

describe.skipIf(process.platform==='win32')('POSIX owned executable recovery',()=>{
async function fixture(body="process.on('SIGTERM',()=>{fs.appendFileSync(signals,'term\\n');process.exit(0)});") {
  const root=await mkdtemp(join(tmpdir(),'hsc-game-recovery-failure-')),directory=join(root,'journal'),supervisorPath=join(root,'supervisor.cjs'),path=join(root,'synthetic.exe'),signals=join(root,'signals'),ready=join(root,'ready')
  await build({entryPoints:['src/main/controlled-game-supervisor.ts'],bundle:true,platform:'node',format:'cjs',outfile:supervisorPath,logLevel:'silent'})
  await writeFile(path,`#!${process.execPath}\nconst fs=require('node:fs');const signals=${JSON.stringify(signals)};${body}\nfs.writeFileSync(${JSON.stringify(ready)},'ready');setInterval(()=>{},1000);\n`,{mode:0o700})
  const options={directory,supervisorPath,runtimeExecutable:process.execPath},clients:DurableGameController[]=[]
  const client=()=>{const c=new DurableGameController(vi.fn(),options);clients.push(c);return c}
  const values={path,title:'synthetic',maxSeconds:60,gameSessionId:'original'}
  return {root,directory,signals,ready,options,client,values,
    waitReady:async()=>{const record=new GameProcessJournal(directory).read('original');if(!record?.process)throw Error('OWNED_FIXTURE_MISSING_IDENTITY');await waitForOwnedReady(ready,record.process)},
    cleanup:async()=>{
      await chmod(directory,0o700).catch(()=>{})
      const identities=new GameProcessJournal(directory).list().flatMap(row=>[row.process,row.supervisor]).filter((value):value is ProcessIdentity=>value!==null)
      try{for(const c of clients){await c.close('test_cleanup')}}finally{for(const c of clients)c.detach();await cleanupOwnedProcesses(identities)}
      await rm(root,{recursive:true,force:true})}}
}
it.each(['birth','executable','supervisor','json'])('cold recovery rejects %s mismatch and never signals the original or a reused PID',async kind=>{
  const f=await fixture(),first=f.client();let path:string|undefined,original:string|undefined
  try{
    const opened=await first.open(f.values)
    await f.waitReady()
    first.detach();path=join(f.directory,(await readdir(f.directory)).find(p=>p.endsWith('.json'))!);original=await readFile(path,'utf8')
    const record=JSON.parse(original)
    if(kind==='birth')record.process.birth='reused-process-birth'
    if(kind==='executable')record.process.executable.pathHash='b'.repeat(64)
    if(kind==='supervisor')record.supervisor.birth='reused-supervisor-birth'
    await writeFile(path,kind==='json'?'{bad':JSON.stringify(record))
    const recovered=f.client()
    expect((await recovered.initialize()).unresolved).toBe(1)
    expect(await recovered.close('app_quit')).toMatchObject({closed:false})
    expect(await recovered.close('renderer_reloaded','original')).toMatchObject({closed:false})
    await expect(recovered.open({...f.values,gameSessionId:'new'})).rejects.toThrow('关闭')
    process.kill(opened.processId,0)
    await expect(readFile(f.signals)).rejects.toMatchObject({code:'ENOENT'})
  }finally{if(path&&original)await writeFile(path,original);await f.cleanup()}
},15000)

it.skipIf(process.platform==='win32')('actual exit without durable write returns unknown, then same-ID retry persists proof without another signal',async()=>{
  const f=await fixture(),first=f.client()
  try{
    await first.open(f.values)
    await f.waitReady()
    await chmod(f.directory,0o500)
    expect(await first.close('child_requested','original')).toMatchObject({closed:false})
    expect(await readFile(f.signals,'utf8')).toBe('term\n')
    await chmod(f.directory,0o700)
    expect(await f.client().close('child_requested','original')).toMatchObject({closed:true,sessionId:'original'})
    expect(await readFile(f.signals,'utf8')).toBe('term\n')
    expect(await f.client().close('renderer_reloaded','original')).toMatchObject({closed:true})
  }finally{await f.cleanup()}
},15000)

it('a refused real signal remains pending across host restart, explicit same-ID retry observes actual exit',async()=>{
  const f=await fixture("let n=0;process.on('SIGTERM',()=>{fs.appendFileSync(signals,'term\\n');if(++n===2)process.exit(0)});"),first=f.client()
  try{
    const opened=await first.open(f.values)
    await f.waitReady()
    expect(await first.close('child_requested','original')).toMatchObject({closed:false})
    first.detach();process.kill(opened.processId,0)
    expect(await f.client().close('renderer_reloaded','original')).toMatchObject({closed:true,sessionId:'original'})
    expect(await readFile(f.signals,'utf8')).toBe('term\nterm\n')
  }finally{await f.cleanup()}
},15000)

it('competing host instances reserve one actual process, never two games',async()=>{
  const f=await fixture(),a=f.client(),b=f.client()
  try{
    const results=await Promise.allSettled([a.open(f.values),b.open({...f.values,gameSessionId:'second'})])
    expect(results.filter(result=>result.status==='fulfilled')).toHaveLength(1)
    const active=(await readdir(f.directory)).filter(name=>name.endsWith('.json')).map(async name=>JSON.parse(await readFile(join(f.directory,name),'utf8')))
    expect((await Promise.all(active)).filter(record=>record.state==='running')).toHaveLength(1)
  }finally{await f.cleanup()}
},15000)
})
