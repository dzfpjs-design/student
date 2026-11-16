// @vitest-environment node
import { EventEmitter } from 'node:events'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { transformSync } from 'esbuild'
import { afterEach, expect, it, vi } from 'vitest'
const windows=vi.hoisted(()=>[] as any[])
vi.mock('electron',()=>({BrowserWindow:class extends EventEmitter {
 options:any;url='';destroyed=false;restore=vi.fn();show=vi.fn();setMenu=vi.fn();isMinimized=()=>false;isDestroyed=()=>this.destroyed;focus=vi.fn();webContents:any=new EventEmitter()
 constructor(options:any){super();this.options=options;windows.push(this);this.webContents.session={setPermissionRequestHandler:vi.fn(),setPermissionCheckHandler:vi.fn(),webRequest:{onBeforeRequest:vi.fn()}};this.webContents.setWindowOpenHandler=vi.fn()}
 async loadURL(url:string){this.url=url}
}}))
import { DiagnosticRecorder } from './diagnostics'
import { createVerifiedQuit } from './verified-quit'
import { createVerifiedQuitRecovery } from './verified-quit-recovery'
const turn=()=>new Promise<void>(resolve=>setImmediate(resolve))
const cleanup:Array<()=>Promise<void>>=[]
afterEach(async()=>{for(const done of cleanup.splice(0))await done();windows.length=0;vi.restoreAllMocks()})
function fixture(options:{codec?:boolean;holdTail?:boolean;disposeFailsOnce?:boolean;flushFails?:'once'|'always'}={}) {
 const source=readFileSync(resolve('src/main/index.ts'),'utf8')
 const helpers=source.slice(source.indexOf('let acknowledgedAbnormalBackend:'),source.indexOf('let draftPrivacy:'))
 const section=source.slice(source.indexOf('  const verifiedQuit = createVerifiedQuit(['),source.lastIndexOf('\n}'))
 const retry=source.slice(source.indexOf('function retryDraftShutdown()'),source.indexOf('function showDraftShutdownFailure()'))
 const root=mkdtempSync(join(tmpdir(),'hsc-public-real-quit-tail-')),recorder=new DiagnosticRecorder(root)
 const originalFlush=recorder.flush.bind(recorder),originalAppend=(recorder as any).append.bind(recorder)
 let release!:()=>void,tailStarted=false,hold=options.holdTail===true,flushFailures=options.flushFails
 const gate=new Promise<void>(resolve=>{release=resolve})
 vi.spyOn(recorder as any,'append').mockImplementation(async(line:any)=>{
  if(hold&&String(line).includes('app_quit_diagnostics_flushed')){tailStarted=true;await gate}
  await originalAppend(line)
 })
 let flushCalls=0
 vi.spyOn(recorder,'flush').mockImplementation(async()=>{flushCalls++;await originalFlush();if(flushCalls>1&&flushFailures){if(flushFailures==='once')flushFailures=undefined;throw Error('PUBLIC_FINAL_FLUSH_FAILED')}})
 const rows=()=>existsSync(recorder.logPath)?readFileSync(recorder.logPath,'utf8').trim().split('\n').filter(Boolean).map(line=>JSON.parse(line)):[]
 const order:string[]=[];let disposeFailures=options.disposeFailsOnce===true
 const dispose=options.codec===false?undefined:vi.fn(async()=>{order.push('codec');if(disposeFailures){disposeFailures=false;throw Error('PUBLIC_CODEC_DISPOSE_FAILED')}})
 let finished=0,attempts=0,stopCalls=0,rendererAlive=true,snapshot:any[]=[]
 const app:any=new EventEmitter()
 app.quit=()=>{
  attempts++;const before={prevented:false,preventDefault(){this.prevented=true}};app.emit('before-quit',before);if(before.prevented)return
  if(rendererAlive){order.push('renderer_final_sync');rendererAlive=false}
  const will={prevented:false,preventDefault(){this.prevented=true}};app.emit('will-quit',will)
  if(!will.prevented){finished++;snapshot=rows();order.push('exit')}
 }
 const env:any={app,diagnosticRecorder:recorder,createVerifiedQuit,createVerifiedQuitRecovery,order,dispose,setImmediate,
  localService:{async stop(){stopCalls++},captureAbnormalShutdownAcknowledgement:()=>null,observeShutdownClosure:vi.fn(()=>null)},
  runtimeAwake:{clear:vi.fn()},pilotTestEmail:{stop:vi.fn()},draftPreparation:{quiesce:vi.fn(async()=>undefined)},draftRecovery:{quiesce:vi.fn()},
  controlledGame:{close:vi.fn(async()=>({closed:true}))},closeControlledMedia:vi.fn(async()=>({closed:true})),ownedLaunches:()=>({unresolved:()=>false}),dialog:{showMessageBox:vi.fn(async()=>({response:0}))}}
 const prelude=`let quitAfterServiceStop=false,draftQuitDraining=false,draftShutdownFailed=false,draftCodecClosing=null;let draftCodecDispose=dispose;function resumeDraftPreparation(){draftQuitDraining=false};function showDraftShutdownFailure(){order.push('draft_failure_ui')};${retry}`
 const code=transformSync(prelude+helpers+section+`;return {state:()=>({quitAfterServiceStop,draftQuitDraining,draftShutdownFailed,closing:!!draftCodecClosing}),retryDraftShutdown}`,{loader:'ts',target:'es2022'}).code
 const keys=Object.keys(env),main=new Function(...keys,code)(...keys.map(key=>env[key]))
 cleanup.push(async()=>{hold=false;release();await originalFlush();rmSync(root,{recursive:true,force:true})})
 return {app,order,dispose,recorder,main,rows,release:()=>{hold=false;release()},get tailStarted(){return tailStarted},get finished(){return finished},get snapshot(){return snapshot},get stopCalls(){return stopCalls},get attempts(){return attempts}}
}
function retryLink(window:any):string {const link=decodeURIComponent(window.url).match(/href="(hsc-verified-quit:retry\/[0-9]+)"/)?.[1];if(!link)throw Error('PUBLIC_RECOVERY_ACTION_MISSING');return link}
function retry(window:any){const event={preventDefault:vi.fn()};window.webContents.emit('will-navigate',event,retryLink(window));expect(event.preventDefault).toHaveBeenCalledOnce()}

it.each([true,false])('real delayed file appends all final tail records before exit, codec=%s',async codec=>{
 const f=fixture({codec,holdTail:true});f.app.quit()
 await vi.waitFor(()=>expect(f.tailStarted).toBe(true))
 try {
  expect(f.finished).toBe(0)
  f.app.quit();f.app.quit();await turn();expect(f.finished).toBe(0)
  f.release();await vi.waitFor(()=>expect(f.finished).toBe(1))
  const events=f.snapshot.map(row=>row.event)
  expect(events).toEqual(['app_quit_requested','app_quit_service_closed','app_quit_diagnostics_flushed','app_quit_final_requested','app_quit_will_quit',...(codec?['app_quit_codec_disposed']:[])])
  expect(f.stopCalls).toBe(1);if(codec){expect(f.dispose).toHaveBeenCalledOnce();expect(f.order.indexOf('codec')).toBeGreaterThan(f.order.indexOf('renderer_final_sync'))}
  const count=f.rows().length,event={preventDefault:vi.fn()};f.app.emit('will-quit',event);await turn()
  expect(event.preventDefault).not.toHaveBeenCalled();expect(f.rows()).toHaveLength(count)
 }finally{f.release()}
})
it('a final flush rejection keeps a manual retry surface and does not misreport or repeat successful codec disposal',async()=>{
 const f=fixture({flushFails:'once'});f.app.quit()
 await vi.waitFor(()=>expect(windows).toHaveLength(1));await turn()
 expect(f.finished).toBe(0);expect(f.main.state().draftShutdownFailed).toBe(false)
 expect(decodeURIComponent(windows[0].url)).toContain('运行日志尚未确认写入完成')
 expect(f.rows().filter(row=>row.event==='draft_codec_dispose_failed')).toHaveLength(0)
 const attempts=f.attempts;for(let i=0;i<5;i++)await turn();expect(f.attempts).toBe(attempts)
 retry(windows[0]);await vi.waitFor(()=>expect(f.finished).toBe(1))
 expect(f.stopCalls).toBe(1);expect(f.dispose).toHaveBeenCalledOnce()
 expect(f.snapshot.map(row=>row.event)).toContain('app_quit_codec_disposed')
})
it('persistent final flush failure has no automatic retry loop',async()=>{
 const f=fixture({codec:false,flushFails:'always'});f.app.quit()
 await vi.waitFor(()=>expect(windows).toHaveLength(1));await turn()
 const attempts=f.attempts;for(let i=0;i<8;i++)await turn()
 expect(f.finished).toBe(0);expect(f.attempts).toBe(attempts);expect(f.stopCalls).toBe(1)
 retry(windows[0]);await turn();await turn();expect(f.finished).toBe(0);expect(f.stopCalls).toBe(1)
})
it('codec failure still uses the original draft recovery retry and never a diagnostic-failure page',async()=>{
 const f=fixture({disposeFailsOnce:true});f.app.quit()
 await vi.waitFor(()=>expect(f.main.state().draftShutdownFailed).toBe(true));await turn()
 expect(f.order).toContain('draft_failure_ui');expect(windows).toHaveLength(0);expect(f.finished).toBe(0)
 f.main.retryDraftShutdown();await vi.waitFor(()=>expect(f.finished).toBe(1))
 expect(f.dispose).toHaveBeenCalledTimes(2);expect(f.stopCalls).toBe(1)
 expect(f.snapshot.filter(row=>row.event==='draft_codec_dispose_failed')).toHaveLength(1)
})
