// @vitest-environment node
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ControlledGameProcessController } from './controlled-game'
import {cleanupOwnedProcesses, ownedIdentity, waitForOwnedReady} from './test/owned-process-fixture'
import type {ProcessIdentity} from './game-process-identity'

const roots: string[] = []
const owned: ProcessIdentity[] = []
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))
async function ready(pid:number,path:string){
  const identity=await ownedIdentity(pid);owned.push(identity)
  await waitForOwnedReady(path,identity)
}
afterEach(async () => {
  await cleanupOwnedProcesses(owned.splice(0))
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })))
})

async function game(body: string, session = 'original') {
  const root = await mkdtemp(join(tmpdir(), 'hsc-owned-game-')); roots.push(root)
  const path = join(root, 'synthetic.exe'), ready = join(root, 'ready'), signals = join(root, 'signals')
  await writeFile(path, `#!${process.execPath}\nconst fs=require('node:fs');\nconst signals=${JSON.stringify(signals)};\n${body}\nfs.writeFileSync(${JSON.stringify(ready)},'ready');\nsetInterval(()=>{},1000);\n`, { mode: 0o700 })
  return { values: { path, title: 'synthetic signal test, not a Windows binary', maxSeconds: 30, gameSessionId: session },
    ready, signals }
}

describe.skipIf(process.platform === 'win32')('actual POSIX child exit receipts, not game/task-tree qualification', () => {
  it('waits for the real exit after a successful kill request; lost-response retry is true', async () => {
    const fixture = await game(`process.on('SIGTERM',()=>{fs.appendFileSync(signals,'term\\n');setTimeout(()=>process.exit(0),180)});`)
    const onClosed = vi.fn(), controller = new ControlledGameProcessController(onClosed)
    const launched = await controller.open(fixture.values)
    await ready(launched.processId,fixture.ready)
    let settled = false
    const closing = Promise.resolve(controller.close()).then(value => { settled = true; return value })
    await sleep(40)
    expect(settled).toBe(false); expect(onClosed).not.toHaveBeenCalled()
    expect(await closing).toEqual({ closed: true })
    expect(onClosed).toHaveBeenCalledExactlyOnceWith('child_requested', 'original')
    expect(await controller.close()).toEqual({ closed: true })
    expect(await readFile(fixture.signals, 'utf8')).toBe('term\n')
  },15000)

  it('does not accept child.killed when SIGTERM was ignored; failed stop can retry', async () => {
    const fixture = await game(`let count=0;process.on('SIGTERM',()=>{fs.appendFileSync(signals,'term\\n');if(++count===2)setTimeout(()=>process.exit(0),10)});`)
    // Exercise the production deadline. An 80ms OS scheduling assumption can
    // coalesce the two real signals under full-suite load; unit clocks cover
    // short deadline boundaries separately, not as an exit acknowledgement.
    const onClosed = vi.fn(), controller = new ControlledGameProcessController(onClosed)
    const launched = await controller.open(fixture.values); await ready(launched.processId,fixture.ready)
    expect(await controller.close()).toEqual({ closed: false })
    expect(onClosed).not.toHaveBeenCalled(); expect(() => process.kill(launched.processId, 0)).not.toThrow()
    expect(await controller.close()).toEqual({ closed: true })
    expect(onClosed).toHaveBeenCalledExactlyOnceWith('child_requested', 'original')
    expect(await readFile(fixture.signals, 'utf8')).toBe('term\nterm\n')
  },15000)

  it('does not launch a replacement while the real original refuses to exit', async () => {
    const fixture = await game(`process.on('SIGTERM',()=>fs.appendFileSync(signals,'term\\n'));`)
    const replacement = await game('', 'replacement'), onClosed = vi.fn()
    const controller = new ControlledGameProcessController(onClosed, { closeTimeoutMs: 80 })
    const launched = await controller.open(fixture.values); await ready(launched.processId,fixture.ready)
    await expect(controller.open(replacement.values).then(async result => { owned.push(await ownedIdentity(result.processId)); return result })).rejects.toThrow('关闭')
    await expect(readFile(replacement.ready)).rejects.toMatchObject({code:'ENOENT'}); expect(onClosed).not.toHaveBeenCalled()
  },15000)

  it('coalesces repeated close requests for the same real process', async () => {
    const fixture = await game(`process.on('SIGTERM',()=>{fs.appendFileSync(signals,'term\\n');setTimeout(()=>process.exit(0),100)});`)
    const onClosed = vi.fn(), controller = new ControlledGameProcessController(onClosed)
    const launched = await controller.open(fixture.values); await ready(launched.processId,fixture.ready)
    const first = controller.close(), repeated = controller.close()
    expect(await first).toEqual({ closed: true }); expect(await repeated).toEqual({ closed: true })
    expect(onClosed).toHaveBeenCalledOnce(); expect(await readFile(fixture.signals, 'utf8')).toBe('term\n')
  },15000)

  it('returns true without a tracked process', async () => {
    const closed = vi.fn(), controller = new ControlledGameProcessController(closed)
    expect(await controller.close()).toEqual({ closed: true }); expect(closed).not.toHaveBeenCalled()
  })
})
