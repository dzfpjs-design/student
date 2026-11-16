// @vitest-environment node
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { expect, it, vi } from 'vitest'
import { ControlledGameProcessController } from './controlled-game'

it.skipIf(process.platform === 'win32')('same-process renderer recovery exits exact child; main restart cannot certify or signal it', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hsc-game-identity-'))
  const executable = join(root, 'synthetic.exe'), ready = join(root, 'ready'), signals = join(root, 'signals')
  await writeFile(executable, `#!${process.execPath}\nconst fs=require('node:fs');process.on('SIGTERM',()=>{fs.writeFileSync(${JSON.stringify(signals)},'term');process.exit(0)});fs.writeFileSync(${JSON.stringify(ready)},'ready');setInterval(()=>{},1000);\n`, {mode:0o700})
  const notification = vi.fn(), controller = new ControlledGameProcessController(notification)
  let pid: number | undefined
  try {
    pid = (await controller.open({path:executable,title:'synthetic',maxSeconds:60,gameSessionId:'new-game'})).processId
    await vi.waitFor(async () => expect(await readFile(ready,'utf8')).toBe('ready'), {timeout:3000})
    expect(await controller.close('renderer_reloaded','old-game')).toMatchObject({closed:false,proof:'identity_mismatch'})
    expect(await new ControlledGameProcessController(vi.fn()).close('renderer_reloaded','new-game')).toMatchObject({closed:false,proof:'unknown'})
    process.kill(pid, 0)
    await expect(readFile(signals)).rejects.toMatchObject({code:'ENOENT'})
    expect(notification).not.toHaveBeenCalled()
    expect(await controller.close('renderer_reloaded','new-game')).toMatchObject({closed:true,sessionId:'new-game'})
    expect(await controller.close('renderer_reloaded','new-game')).toMatchObject({closed:true,proof:'observed_closed'})
    expect(notification).toHaveBeenCalledExactlyOnceWith('renderer_reloaded','new-game')
    expect(await new ControlledGameProcessController(vi.fn()).close('renderer_reloaded','new-game')).toMatchObject({closed:false})
    await expect(controller.open({path:executable,title:'synthetic',maxSeconds:60,gameSessionId:'new-game'})).rejects.toThrow()
  } finally {
    if (pid) { try { process.kill(pid, 'SIGKILL') } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error } }
    await rm(root,{recursive:true,force:true})
  }
})
