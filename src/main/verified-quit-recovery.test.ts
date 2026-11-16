// @vitest-environment node
import{EventEmitter}from'node:events'
import{readFileSync}from'node:fs'
import{resolve}from'node:path'
import{transformSync}from'esbuild'
import{beforeEach,expect,it,vi}from'vitest'
const windows=vi.hoisted(()=>[]as any[])
vi.mock('electron',()=>({BrowserWindow:class extends EventEmitter{
 options:any;url='';destroyed=false;focused=0;restore=vi.fn();show=vi.fn();setMenu=vi.fn();isMinimized=()=>false;isDestroyed=()=>this.destroyed;focus=()=>this.focused++
 webContents:any=new EventEmitter()
 constructor(options:any){super();this.options=options;windows.push(this);this.webContents.session={setPermissionRequestHandler:vi.fn(),setPermissionCheckHandler:vi.fn(),webRequest:{onBeforeRequest:vi.fn()}};this.webContents.setWindowOpenHandler=vi.fn()}
 async loadURL(url:string){this.url=url}
}}))
import{createVerifiedQuitRecovery}from'./verified-quit-recovery'
import{createVerifiedQuit}from'./verified-quit'
const turns=()=>new Promise<void>(resolve=>setImmediate(resolve))
function link(w:any,kind='retry'){return decodeURIComponent(w.url).match(new RegExp(`href="(hsc-verified-quit:${kind}/[0-9]+)"`))?.[1]}
function navigate(w:any,url:string|undefined){if(typeof url!=='string')throw new Error('PUBLIC_RECOVERY_ACTION_MISSING');const e={preventDefault:vi.fn()};w.webContents.emit('will-navigate',e,url);expect(e.preventDefault).toHaveBeenCalledOnce()}
beforeEach(()=>{windows.length=0})

it('the recovery surface exposes no service bridge and rejects remote, stale and wrong-window actions',()=>{
 let canExit=false;const retry=vi.fn(),ack=vi.fn(()=>true)
 const recovery=createVerifiedQuitRecovery({canExit:()=>canExit,retry,loadFailed:vi.fn()})
 recovery.show('backend_pending');const first=windows[0],old=link(first)
 expect(first.options.webPreferences).toMatchObject({sandbox:true,contextIsolation:true,nodeIntegration:false,webSecurity:true})
 expect(first.options.webPreferences.preload).toBeUndefined();expect(link(first,'acknowledge')).toBeUndefined()
 const request=first.webContents.session.webRequest.onBeforeRequest.mock.calls[0][1],result=vi.fn()
 request({url:'https://private.invalid'},result);expect(result).toHaveBeenCalledWith({cancel:true})
 navigate(first,'https://private.invalid');expect(retry).not.toHaveBeenCalled()
 recovery.show('backend_abnormal',ack);navigate(first,old);expect(retry).not.toHaveBeenCalled()
 const bad=link(first,'acknowledge');ack.mockReturnValueOnce(false);navigate(first,bad);expect(retry).not.toHaveBeenCalled()
 navigate(first,bad);expect(retry).toHaveBeenCalledOnce()
 first.destroyed=true;first.emit('closed');recovery.focusIfActive();expect(windows).toHaveLength(2)
 navigate(first,bad);expect(retry).toHaveBeenCalledOnce()
 const e={preventDefault:vi.fn()};windows[1].emit('close',e);expect(e.preventDefault).toHaveBeenCalledOnce();expect(retry).toHaveBeenCalledTimes(2)
 canExit=true;const final={preventDefault:vi.fn()};windows[1].emit('close',final);expect(final.preventDefault).not.toHaveBeenCalled()
})

function originalMainFixture(mode:'pending'|'abnormal',failFlush:'once'|'always'|undefined=undefined,externalBlocked=false){
 const path=resolve('src/main/index.ts'),source=readFileSync(path,'utf8')
 const helperStart=source.indexOf('let acknowledgedAbnormalBackend:')
 const helpers=helperStart<0?'':source.slice(helperStart,source.indexOf('let draftPrivacy:',helperStart))
 const section=source.slice(source.indexOf('  const verifiedQuit = createVerifiedQuit(['),source.lastIndexOf('\n}'))
 let valid=true,observed:any=null,closed=false,stopCalls=0
 const records:any[]=[],order:string[]=[],app:any=new EventEmitter()
 let finished=0
 app.quit=()=>{
  const e={prevented:false,preventDefault(){this.prevented=true}};app.emit('before-quit',e);if(e.prevented)return
  const will={prevented:false,preventDefault(){this.prevented=true}};app.emit('will-quit',will);if(!will.prevented){finished++;order.push('exit')}
 }
 const localService={
  async stop(){stopCalls++;order.push('stop');if(mode==='abnormal'||!closed)throw Object.assign(Error('public shutdown blocked'),{code:mode==='abnormal'?'LOCAL_SERVICE_SHUTDOWN_ABNORMAL':'LOCAL_SERVICE_SHUTDOWN_PENDING'})},
  captureAbnormalShutdownAcknowledgement(){return mode==='abnormal'?()=>valid:null},
  observeShutdownClosure(callback:any){observed=callback;return()=>{observed=null}},
 }
 const env:any={app,localService,createVerifiedQuit,createVerifiedQuitRecovery,
  runtimeAwake:{clear:vi.fn()},pilotTestEmail:{stop:vi.fn()},draftPreparation:{quiesce:vi.fn(async()=>undefined)},draftRecovery:{quiesce:vi.fn()},
  diagnosticRecorder:{async record(x:any){records.push(x)},async flush(){order.push('flush');if(failFlush){if(failFlush==='once')failFlush=undefined;throw Error('PUBLIC_FLUSH_RETRY')}}},
  controlledGame:{close:vi.fn(async()=>({closed:!externalBlocked}))},closeControlledMedia:vi.fn(async()=>({closed:true})),ownedLaunches:()=>({unresolved:()=>false}),
  dialog:{showMessageBox:vi.fn(async()=>({response:0}))},setImmediate,
 }
 const prelude="let quitAfterServiceStop=false,draftQuitDraining=false,draftShutdownFailed=false,draftCodecClosing=null;let draftCodecDispose=async()=>{order.push('codec')};function resumeDraftPreparation(){draftQuitDraining=false};function retryDraftShutdown(){};function showDraftShutdownFailure(){};"
 const text=prelude+helpers+section+'\nreturn {state:()=>({quitAfterServiceStop,draftQuitDraining})}'
 const js=transformSync(text,{loader:'ts',target:'es2022'}).code
 const keys=Object.keys(env),factory=new Function(...keys,'order',js),main=factory(...keys.map(k=>env[k]),order)
 return{app,records,order,localService,main,dialog:env.dialog,get stopCalls(){return stopCalls},get finished(){return finished},valid:(value:boolean)=>valid=value,
  async finishOriginal(){closed=true;observed?.({graceful:true,isCurrent:()=>valid});await turns();await turns();await turns()}}
}

it('actual Main failure callback releases the old quit promise and retains a usable same-process retry surface',async()=>{
 const f=originalMainFixture('pending');f.app.quit();await turns();await turns()
 expect(windows).toHaveLength(1);expect(f.finished).toBe(0);expect(f.main.state().draftQuitDraining).toBe(true)
 expect(f.stopCalls).toBe(1);expect(f.dialog.showMessageBox).not.toHaveBeenCalled()
 await f.finishOriginal()
 expect(f.stopCalls).toBe(2);expect(f.finished).toBe(1)
 expect(f.order.indexOf('codec')).toBeGreaterThan(f.order.indexOf('flush'));expect(f.order.at(-1)).toBe('exit')
})

it('actual Main retry button runs a fresh verified quit after the original failed promise settles',async()=>{
 const f=originalMainFixture('pending');f.app.quit();await turns();await turns()
 expect(windows).toHaveLength(1);navigate(windows[0],link(windows[0]));await turns();await turns()
 expect(f.stopCalls).toBe(2);expect(f.finished).toBe(0)
 await f.finishOriginal();expect(f.stopCalls).toBe(3);expect(f.finished).toBe(1)
})

it('a stale natural-close receipt cannot finish an unrelated current owner',async()=>{
 const f=originalMainFixture('pending');f.app.quit();await turns();await turns()
 expect(windows).toHaveLength(1)
 f.valid(false);await f.finishOriginal()
 expect(f.finished).toBe(0);expect(f.stopCalls).toBe(1)
})

it('abnormal acknowledgement is explicit, rechecks original ownership and never records normal service save',async()=>{
 const f=originalMainFixture('abnormal');f.app.quit();await turns();await turns()
 expect(windows).toHaveLength(1);const url=link(windows[0],'acknowledge');expect(url).toBeTruthy()
 f.valid(false);navigate(windows[0],url);await turns();expect(f.finished).toBe(0)
 f.valid(true);navigate(windows[0],url);await turns();await turns();await turns()
 expect(f.finished).toBe(1);expect(f.stopCalls).toBe(1)
 expect(f.records.filter(x=>x.event==='app_quit_service_closed')).toHaveLength(0)
 expect(f.records.filter(x=>x.event==='app_quit_backend_abnormal_acknowledged')).toHaveLength(1)
 expect(f.order).toContain('codec')
})

it('unexpected quit callback rejection preserves draining and the same usable retry entry',async()=>{
 const f=originalMainFixture('pending','once');f.app.quit();await turns();await turns()
 await f.finishOriginal()
 expect(f.finished).toBe(0);expect(f.main.state().draftQuitDraining).toBe(true);expect(windows).toHaveLength(1)
 navigate(windows[0],link(windows[0]));await turns();await turns();await turns()
 expect(f.finished).toBe(1);expect(f.order).toContain('codec')
})

it('persistent diagnostics failure after natural closure waits for manual retry without an auto-quit loop',async()=>{
 const f=originalMainFixture('pending','always');f.app.quit();await turns();await turns()
 await f.finishOriginal();const calls=f.stopCalls
 for(let i=0;i<8;i++)await turns()
 expect(f.finished).toBe(0);expect(f.stopCalls).toBe(calls);expect(calls).toBe(2);expect(windows).toHaveLength(1)
 navigate(windows[0],link(windows[0]));await turns();await turns()
 expect(f.stopCalls).toBe(calls+1);expect(f.finished).toBe(0)
})
it('an external activity receipt blocks headless quit with a restricted and live retry surface',async()=>{
 const f=originalMainFixture('pending',undefined,true);f.app.quit();await turns();await turns()
 expect(f.finished).toBe(0);expect(f.stopCalls).toBe(0);expect(windows).toHaveLength(1)
 expect(decodeURIComponent(windows[0].url)).toContain('还有外部活动尚未确认关闭')
 navigate(windows[0],link(windows[0]));await turns();await turns()
 expect(f.stopCalls).toBe(0);expect(f.finished).toBe(0);expect(windows).toHaveLength(1)
})
