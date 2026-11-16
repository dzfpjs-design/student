// @vitest-environment node
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { build } from 'esbuild'
import { expect, it, vi } from 'vitest'
import { DurableGameController } from './controlled-game-durable'

it.skipIf(process.platform === 'win32')('cold host initialization finds and actually closes its original live process without backend active GET', async () => {
  const root=await mkdtemp(join(tmpdir(),'hsc-durable-game-'))
  const supervisorPath=join(root,'supervisor.cjs'),executable=join(root,'synthetic.exe'),ready=join(root,'ready'),signals=join(root,'signals')
  await build({entryPoints:['src/main/controlled-game-supervisor.ts'],bundle:true,platform:'node',format:'cjs',outfile:supervisorPath,logLevel:'silent'})
  await writeFile(executable,`#!${process.execPath}\nconst fs=require('node:fs');process.on('SIGTERM',()=>{fs.appendFileSync(${JSON.stringify(signals)},'term\\n');process.exit(0)});fs.writeFileSync(${JSON.stringify(ready)},'ready');setInterval(()=>{},1000);\n`,{mode:0o700})
  const options={directory:join(root,'journal'),supervisorPath,runtimeExecutable:process.execPath}
  const first=new DurableGameController(vi.fn(),options)
  let recovered:DurableGameController|undefined
  try{
    const launched=await first.open({path:executable,title:'synthetic',maxSeconds:60,gameSessionId:'original'})
    await vi.waitFor(async()=>expect(await readFile(ready,'utf8')).toBe('ready'),{timeout:5000})
    first.detach()
    recovered=new DurableGameController(vi.fn(),options)
    expect((await recovered.initialize()).unresolved).toBe(1)
    await expect(recovered.open({path:executable,title:'new',maxSeconds:60,gameSessionId:'new'})).rejects.toThrow('关闭')
    expect(await recovered.close('renderer_reloaded','wrong')).toMatchObject({closed:false})
    process.kill(launched.processId,0)
    expect(await recovered.close('app_quit')).toMatchObject({closed:true})
    expect(await readFile(signals,'utf8')).toBe('term\n')
    expect(await new DurableGameController(vi.fn(),options).close('renderer_reloaded','original')).toMatchObject({closed:true,sessionId:'original',proof:'observed_closed'})
    await expect(recovered.open({path:executable,title:'old',maxSeconds:60,gameSessionId:'original'})).rejects.toThrow()
  } finally {
    await recovered?.close('test_cleanup').catch(()=>{})
    await first.close('test_cleanup').catch(()=>{})
    recovered?.detach();first.detach()
    await rm(root,{recursive:true,force:true})
  }
},20000)
