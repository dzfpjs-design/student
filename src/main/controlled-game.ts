import { spawn, type ChildProcess } from 'node:child_process'
import { stat } from 'node:fs/promises'
import { dirname, extname, resolve } from 'node:path'
import { ControlledResourceScope, type ControlledCloseReceipt } from './controlled-resource-scope'
import { DurableGameController, type DurableGameOptions } from './controlled-game-durable'

export type ControlledGameLaunch = {
  path: string
  title: string
  maxSeconds: number
  gameSessionId: string
}

export type ControlledGameLaunchResult = { opened: true; processId: number }

type GameRun = {
  child: ChildProcess
  sessionId: string
  reason: string
  timer: ReturnType<typeof setTimeout> | null
  spawned: boolean
  exited: boolean
  launchFailed: boolean
  notified: boolean
  stopRequested: boolean
  treeConfirmed: boolean
  closing: Promise<boolean> | null
}

export async function validateControlledGameLaunch(values: ControlledGameLaunch): Promise<ControlledGameLaunch> {
  if (!values || typeof values.path !== 'string' || typeof values.title !== 'string' || typeof values.gameSessionId !== 'string') {
    throw new Error('受控游戏参数无效。')
  }
  const path = resolve(values.path)
  if (extname(path).toLowerCase() !== '.exe') throw new Error('只允许启动家长许可的 Windows .exe 游戏。')
  const file = await stat(path)
  if (!file.isFile()) throw new Error('受控游戏程序不可用。')
  return {
    path,
    title: values.title.trim().slice(0, 120),
    gameSessionId: values.gameSessionId,
    maxSeconds: Math.min(3600, Math.max(30, Math.floor(values.maxSeconds))),
  }
}

export class ControlledGameProcessController {
  private current: GameRun | null = null
  private opening = false
  private launchGeneration = 0
  private readonly platform: NodeJS.Platform
  private readonly closeTimeoutMs: number
  private readonly scope = new ControlledResourceScope()
  private readonly durable?: DurableGameController
  private readonly windowsStop?: (pid:number) => Promise<boolean>

  constructor(
    private readonly onClosed: (reason: string, gameSessionId: string) => void,
    options: { closeTimeoutMs?: number; platform?: NodeJS.Platform; durable?:DurableGameOptions; windowsStop?:(pid:number)=>Promise<boolean> } = {},
  ) {
    this.platform = options.platform ?? process.platform
    this.closeTimeoutMs = options.closeTimeoutMs ?? 2000
    this.windowsStop = options.windowsStop
    if(options.durable)this.durable = new DurableGameController(onClosed,options.durable)
  }

  async initialize():Promise<{unresolved:number}>{return this.durable?this.durable.initialize():{unresolved:0}}
  pendingIds():string[]{if(!this.durable)throw Error('GAME_DURABLE_IDENTITY_UNAVAILABLE');return this.durable.pendingIds()}
  hasDurableRecord(sessionId:string):boolean{
    if(!this.durable)throw Error('GAME_DURABLE_JOURNAL_REQUIRED')
    return this.durable.hasRecord(sessionId)
  }

  async open(values: ControlledGameLaunch): Promise<ControlledGameLaunchResult> {
    if(this.durable)return this.durable.open(await validateControlledGameLaunch(values))
    if (this.opening) throw new Error('另一个游戏启动请求尚未结束。')
    this.opening = true
    const generation = ++this.launchGeneration
    try {
      const launch = await validateControlledGameLaunch(values)
      if (!launch.gameSessionId.trim() || launch.gameSessionId.length > 128 || this.scope.wasClosed(launch.gameSessionId)) throw new Error('游戏记录无效或已经关闭，没有重新启动。')
      if (this.current?.sessionId === launch.gameSessionId) throw new Error('原游戏已经启动，没有重复启动。')
      if (generation !== this.launchGeneration) throw new Error('游戏启动已取消。')
      if (this.current && !await this.stopRun(this.current, 'replaced')) throw new Error('原游戏尚未确认关闭，没有启动新游戏。')
      if (generation !== this.launchGeneration) throw new Error('游戏启动已取消。')
      const child = spawn(launch.path, [], {
        cwd: dirname(launch.path), detached: false, shell: false, stdio: 'ignore', windowsHide: false,
      })
      const run: GameRun = {
        child, sessionId: launch.gameSessionId, reason: 'process_exited', timer: null,
        spawned: false, exited: false, launchFailed: false, notified: false,
        stopRequested: false, treeConfirmed: this.platform !== 'win32', closing: null,
      }
      this.current = run
      return await new Promise<ControlledGameLaunchResult>((resolveLaunch, rejectLaunch) => {
        child.once('spawn', () => {
          run.spawned = true
          if (!child.pid || generation !== this.launchGeneration) {
            rejectLaunch(new Error('游戏启动已取消或进程编号不可用。'))
            void this.stopRun(run, 'launch_cancelled')
            return
          }
          run.timer = setTimeout(() => { void this.stopRun(run, 'time_limit') }, launch.maxSeconds * 1000)
          resolveLaunch({ opened: true, processId: child.pid })
        })
        // A signal error on a running process does not prove it exited.
        child.on('error', () => {
          if (!run.spawned) {
            run.launchFailed = true
            this.finalize(run, 'launch_failed')
            rejectLaunch(new Error('游戏程序启动失败。'))
          }
        })
        child.once('exit', () => {
          run.exited = true
          if (!run.stopRequested || run.treeConfirmed) this.finalize(run, run.reason)
        })
      })
    } finally { this.opening = false }
  }

  async close(reason = 'child_requested', expectedSessionId?: string): Promise<ControlledCloseReceipt> {
    if(this.durable)return this.durable.close(reason,expectedSessionId)
    if (expectedSessionId !== undefined) {
      const prior = this.scope.check(expectedSessionId, this.current ? [this.current.sessionId] : [])
      if (prior) return prior
    }
    ++this.launchGeneration
    const run = this.current
    if (!run) return { closed: true }
    const closed = await this.stopRun(run, reason)
    const confirmed = closed && (!this.current || this.current === run)
    return expectedSessionId === undefined ? { closed: confirmed } : {
      closed: confirmed, sessionId: expectedSessionId, proof: confirmed ? 'observed_closed' : 'unknown',
    }
  }

  private stopRun(run: GameRun, reason: string): Promise<boolean> {
    if (run.launchFailed || (run.exited && (!run.stopRequested || run.treeConfirmed))) return Promise.resolve(true)
    if (run.closing) return run.closing
    // Do not reuse an exited PID to kill a possibly unrelated process.
    if (run.exited) return Promise.resolve(false)
    run.stopRequested = true
    run.reason = reason.slice(0, 120)
    const { child } = run
    run.closing = new Promise<boolean>((resolveClosed) => {
      let settled = false
      const finish = (closed: boolean): void => {
        if (settled) return
        settled = true
        clearTimeout(timeout)
        child.removeListener('exit', check)
        child.removeListener('error', failed)
        child.removeListener('spawn', signal)
        resolveClosed(closed)
      }
      const check = (): void => {
        if (run.launchFailed || (run.exited && run.treeConfirmed)) {
          this.finalize(run, run.launchFailed ? 'launch_failed' : run.reason)
          finish(true)
        }
      }
      const failed = (): void => { if (run.launchFailed) check(); else finish(false) }
      const signal = (): void => {
        if (!child.pid || run.exited || run.launchFailed) { check(); return }
        try {
          if (this.platform === 'win32') {
            if(this.windowsStop){
              void this.windowsStop(child.pid).then(closed=>{if(!closed){finish(false);return}run.treeConfirmed=true;check()},()=>finish(false))
              return
            }
            const killer = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], {
              shell: false, stdio: 'ignore', windowsHide: true,
            })
            killer.once('error', () => finish(false))
            killer.once('exit', (code) => {
              if (code !== 0) { finish(false); return }
              run.treeConfirmed = true
              check()
            })
          } else if (!child.kill()) finish(false)
        } catch { finish(false) }
      }
      const timeout = setTimeout(() => finish(false), this.closeTimeoutMs)
      child.once('exit', check)
      child.once('error', failed)
      if (run.spawned) signal(); else child.once('spawn', signal)
    })
    const result = run.closing
    void result.then(() => { if (run.closing === result) run.closing = null })
    return result
  }

  private finalize(run: GameRun, reason: string): void {
    if (run.notified || (!run.exited && !run.launchFailed)) return
    run.notified = true
    if (run.timer) clearTimeout(run.timer)
    run.timer = null
    if (this.current === run) this.current = null
    this.scope.recordClosed(run.sessionId)
    this.onClosed(reason, run.sessionId)
  }
}
