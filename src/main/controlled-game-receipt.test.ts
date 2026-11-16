// @vitest-environment node
import { EventEmitter } from 'node:events'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
const mocked = vi.hoisted(() => ({ spawn: vi.fn(), stat: vi.fn() }))
vi.mock('node:child_process', () => ({ spawn: mocked.spawn }))
vi.mock('node:fs/promises', () => ({ stat: mocked.stat }))
import { ControlledGameProcessController } from './controlled-game'

class Child extends EventEmitter {
  pid = 501
  killed = false
  exitCode: number | null = null
  signalCode: string | null = null
  kill = vi.fn(() => { this.killed = true; return true })
  exit(code = 0) { this.exitCode = code; this.emit('exit', code, null) }
}
const games: Child[] = [], killers: Child[] = []
const values = (session = 'original') => ({ path: '/synthetic/game.exe', title: 'fixture', maxSeconds: 30, gameSessionId: session })
beforeEach(() => {
  games.length = 0; killers.length = 0
  mocked.stat.mockResolvedValue({ isFile: () => true })
  mocked.spawn.mockImplementation((path: string) => {
    const child = new Child()
    if (path === 'taskkill') killers.push(child)
    else { child.pid += games.length; games.push(child); queueMicrotask(() => child.emit('spawn')) }
    return child
  })
})
afterEach(() => {
  for (const child of killers) child.exit()
  for (const child of games) child.exit()
  vi.resetAllMocks()
})

describe('Windows closure protocol (simulated OS; not a task-tree device test)', () => {
  it('requires both successful taskkill and actual original process exit', async () => {
    const notified = vi.fn(), c = new ControlledGameProcessController(notified, { platform: 'win32', closeTimeoutMs: 100 })
    await c.open(values())
    let settled = false
    const closing = c.close().then(value => { settled = true; return value })
    expect(mocked.spawn).toHaveBeenLastCalledWith('taskkill', ['/pid', '501', '/t', '/f'], expect.objectContaining({ shell: false }))
    killers[0].exit(0); await Promise.resolve()
    expect(settled).toBe(false); expect(notified).not.toHaveBeenCalled()
    games[0].exit()
    expect(await closing).toEqual({ closed: true })
    expect(notified).toHaveBeenCalledExactlyOnceWith('child_requested', 'original')
    expect(await c.close()).toEqual({ closed: true })
  })
  it('does not finalize root exit before a failed tree request or reuse its exited PID', async () => {
    const notified = vi.fn(), c = new ControlledGameProcessController(notified, { platform: 'win32', closeTimeoutMs: 100 })
    await c.open(values()); const closing = c.close()
    games[0].exit(); killers[0].exit(5)
    expect(await closing).toEqual({ closed: false }); expect(notified).not.toHaveBeenCalled()
    expect(await c.close()).toEqual({ closed: false }); expect(killers).toHaveLength(1)
    expect(games[0].kill).not.toHaveBeenCalled()
  })
  it('does not substitute child.kill for taskkill failure; permits retry while original lives', async () => {
    const c = new ControlledGameProcessController(vi.fn(), { platform: 'win32', closeTimeoutMs: 100 })
    await c.open(values()); const first = c.close()
    killers[0].emit('error', new Error('taskkill unavailable'))
    expect(await first).toEqual({ closed: false }); expect(games[0].kill).not.toHaveBeenCalled()
    const retried = c.close(); killers[1].exit(); games[0].exit()
    expect(await retried).toEqual({ closed: true })
  })
})

describe('captured process and launch ownership', () => {
  it('does not finalize a still-running process on signal error', async () => {
    const notified = vi.fn(), c = new ControlledGameProcessController(notified, { platform: 'darwin', closeTimeoutMs: 100 })
    await c.open(values()); const stop = c.close()
    games[0].emit('error', new Error('signal refused'))
    expect(await stop).toEqual({ closed: false }); expect(notified).not.toHaveBeenCalled()
    const retry = c.close(); games[0].exit()
    expect(await retry).toEqual({ closed: true }); expect(notified).toHaveBeenCalledOnce()
  })
  it('keeps old events bound to the old session and preserves the replacement', async () => {
    const notified = vi.fn(), c = new ControlledGameProcessController(notified, { platform: 'darwin', closeTimeoutMs: 100 })
    await c.open(values()); games[0].exit()
    await c.open(values('replacement'))
    games[0].emit('error', new Error('late old event')); games[0].emit('exit', 0, null)
    expect(notified).toHaveBeenCalledExactlyOnceWith('process_exited', 'original')
    const close = c.close(); expect(games[1].kill).toHaveBeenCalledOnce(); games[1].exit()
    expect(await close).toEqual({ closed: true })
    expect(notified).toHaveBeenLastCalledWith('child_requested', 'replacement')
  })
  it('cancels an unspawned validation so stop cannot be followed by a late new game', async () => {
    let release!: (value: { isFile(): boolean }) => void
    mocked.stat.mockReturnValueOnce(new Promise(resolve => { release = resolve }))
    const c = new ControlledGameProcessController(vi.fn())
    const opening = c.open(values())
    expect(await c.close()).toEqual({ closed: true })
    release({ isFile: () => true })
    await expect(opening).rejects.toThrow('取消')
    expect(mocked.spawn).not.toHaveBeenCalled()
  })
  it('rejects concurrent opens rather than racing two new processes into one owner slot', async () => {
    let release!: (value: { isFile(): boolean }) => void
    mocked.stat.mockReturnValueOnce(new Promise(resolve => { release = resolve }))
    const c = new ControlledGameProcessController(vi.fn()), first = c.open(values())
    await expect(c.open(values('other'))).rejects.toThrow('尚未结束')
    release({ isFile: () => true }); await first
    expect(games).toHaveLength(1)
  })
})
