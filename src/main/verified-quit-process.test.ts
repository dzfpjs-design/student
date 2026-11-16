// @vitest-environment node
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { expect, it, vi } from 'vitest'
import { ControlledGameProcessController } from './controlled-game'
import { createVerifiedQuit } from './verified-quit'

it.skipIf(process.platform === 'win32')('real refused process stop blocks quit; explicit retry after actual exit permits it', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hsc-quit-owned-')), file = join(root, 'synthetic.exe'), ready = join(root, 'ready')
  await writeFile(file, `#!${process.execPath}\nconst fs=require('node:fs');let n=0;process.on('SIGTERM',()=>{if(++n===2)process.exit(0)});fs.writeFileSync(${JSON.stringify(ready)},'ready');setInterval(()=>{},1000);\n`, { mode: 0o700 })
  const onClosed = vi.fn(), controller = new ControlledGameProcessController(onClosed, { closeTimeoutMs: 80 })
  let pid: number | undefined
  let exited = false
  try {
    pid = (await controller.open({ path: file, title: 'synthetic quit signal', maxSeconds: 30, gameSessionId: 'original' })).processId
    // Parallel native fixture launches can queue; the product's 80ms stop
    // deadline and refusal-to-quit assertions below remain unchanged.
    await vi.waitFor(async () => expect(await readFile(ready, 'utf8')).toBe('ready'), { timeout: 10000, interval: 25 })
    const shutdown = vi.fn(async () => {}), blocked = vi.fn(async () => {})
    const quit = createVerifiedQuit([() => controller.close('app_quit'), async () => ({ closed: true })], shutdown, blocked)
    await quit()
    expect(shutdown).not.toHaveBeenCalled(); expect(blocked).toHaveBeenCalledOnce()
    expect(onClosed).not.toHaveBeenCalled(); expect(() => process.kill(pid!, 0)).not.toThrow()
    await quit()
    expect(onClosed).toHaveBeenCalledExactlyOnceWith('app_quit', 'original')
    exited = true
    expect(shutdown).toHaveBeenCalledOnce()
    expect(await controller.close()).toEqual({ closed: true })
  } finally {
    if (pid && !exited) { try { process.kill(pid, 'SIGKILL') } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error } }
    await new Promise(resolve => setTimeout(resolve, 25))
    await rm(root, { recursive: true, force: true })
  }
}, 15000)
