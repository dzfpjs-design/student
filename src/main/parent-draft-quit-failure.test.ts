// @vitest-environment node
// Exact Main callback bodies; public synthetic helpers, no product profile or network.
import {readFileSync} from 'node:fs'
import {EventEmitter} from 'node:events'
import * as vm from 'node:vm'
import {createRequire} from 'node:module'
import {transformSync} from 'esbuild'
import {expect,it,vi} from 'vitest'
const parser=createRequire(import.meta.url)('@babel/parser')
const source=readFileSync(new URL('./index.ts',import.meta.url),'utf8')
const tree=parser.parse(source,{sourceType:'module',plugins:['typescript']})
const functions=new Map<string,string>(),callbacks=new Map<string,string>()
function visit(n:any){if(!n||typeof n!=='object')return
 if(n.type==='FunctionDeclaration')functions.set(n.id.name,source.slice(n.start,n.end))
 if(n.type==='CallExpression'&&n.callee?.object?.name==='app'&&n.callee?.property?.name==='on'&&n.arguments[0]?.type==='StringLiteral')callbacks.set(n.arguments[0].value,source.slice(n.arguments[1].start,n.arguments[1].end))
 for(const v of Object.values(n)){if(Array.isArray(v))v.forEach(visit);else if(v&&typeof v==='object')visit(v)}
}visit(tree)
function fixture(dispose=vi.fn(async()=>{})){
 const windows:any[]=[],requests:any[]=[],permissions:any[]=[]
 class Window extends EventEmitter{
  destroyed=false; minimized=false; options:any;webContents:any;url=''
  show=vi.fn();focus=vi.fn();restore=vi.fn();setMenu=vi.fn()
  constructor(options:any){super();this.options=options;windows.push(this);this.webContents=new EventEmitter();Object.assign(this.webContents,{setWindowOpenHandler:vi.fn(),session:{setPermissionRequestHandler:(v:any)=>permissions.push(v),setPermissionCheckHandler:(v:any)=>permissions.push(v),webRequest:{onBeforeRequest:(f:any,v:any)=>requests.push(v)}}})}
  static getAllWindows(){return windows.filter(w=>!w.destroyed)}
  isDestroyed(){return this.destroyed}isMinimized(){return this.minimized}
  loadURL(url:string){this.url=url;return Promise.resolve()}
 }
 const state:any={BrowserWindow:Window,app:{quit:vi.fn()},draftShutdownFailed:false,draftShutdownFailureWindow:null,draftCodecClosing:null,draftCodecDispose:dispose,draftQuitDraining:true,quitAfterServiceStop:true,draftRecovery:{quiesce:vi.fn(),resume:vi.fn()},draftPreparation:{resume:vi.fn()},resumeDraftPreparation:vi.fn(),localService:{start:vi.fn()},createWindow:vi.fn(),diagnosticRecorder:{record:vi.fn(async()=>{})},windows,requests,permissions}
 const names=['retryDraftShutdown','showDraftShutdownFailure'];const methods=names.map(n=>functions.get(n)||'').join('\n')
 const selected=['will-quit','before-quit','second-instance','activate'].map(n=>'globalThis['+JSON.stringify(n)+']='+callbacks.get(n)).join('\n')
 vm.runInNewContext(transformSync(methods+'\n'+selected,{loader:'ts',format:'cjs'}).code,state)
 return state
}
it('a rejected original disposal leaves a visible restricted HOLD instead of a windowless single-instance process',async()=>{
 const f=fixture(vi.fn(async()=>{throw Error('synthetic private detail')}));f['will-quit']({preventDefault:vi.fn()});await f.draftCodecClosing
 expect(f.windows).toHaveLength(1);expect(f.draftShutdownFailed).toBe(true);expect(f.draftQuitDraining).toBe(true);expect(f.quitAfterServiceStop).toBe(false)
 expect(f.windows[0].show).toHaveBeenCalledOnce();expect(f.windows[0].focus).toHaveBeenCalledOnce();expect(f.resumeDraftPreparation).not.toHaveBeenCalled();expect(f.localService.start).not.toHaveBeenCalled()
 expect(decodeURIComponent(f.windows[0].url)).toContain('重启电脑后重新打开');expect(f.windows[0].url).not.toContain('synthetic')
})
it('the failure window has no preload, Node, IPC, media, network or navigation authority',async()=>{
 const f=fixture(vi.fn(async()=>{throw Error('private')}));f['will-quit']({preventDefault:vi.fn()});await f.draftCodecClosing;const w=f.windows[0]
 expect(w.options.webPreferences).toMatchObject({sandbox:true,contextIsolation:true,nodeIntegration:false,webSecurity:true});expect(w.options.webPreferences.preload).toBeUndefined()
 expect(w.webContents.setWindowOpenHandler.mock.calls[0][0]()).toEqual({action:'deny'})
 const cancel=vi.fn();f.requests[0]({url:'https://example.invalid/'},cancel);expect(cancel).toHaveBeenLastCalledWith({cancel:true})
 const permission=vi.fn();f.permissions[0](null,'media',permission);expect(permission).toHaveBeenCalledWith(false);expect(f.permissions[1]()).toBe(false)
 const event={preventDefault:vi.fn()};w.webContents.emit('will-navigate',event,'https://example.invalid/');expect(event.preventDefault).toHaveBeenCalledOnce();expect(f.app.quit).not.toHaveBeenCalled()
 expect(decodeURIComponent(w.url)).toContain('<a href="hsc-draft-quit:retry"');expect(decodeURIComponent(w.url)).toContain('DRAFT_RECOVERY_IO')
})
it('second launch and activate focus the existing HOLD window, without normal window or service start',async()=>{
 const f=fixture(vi.fn(async()=>{throw Error('private')}));f['will-quit']({preventDefault:vi.fn()});await f.draftCodecClosing;const w=f.windows[0];w.minimized=true
 f['second-instance']();f.activate();expect(f.windows).toHaveLength(1);expect(w.restore).toHaveBeenCalledTimes(2);expect(w.focus).toHaveBeenCalledTimes(3);expect(f.createWindow).not.toHaveBeenCalled();expect(f.localService.start).not.toHaveBeenCalled()
})
it('the keyboard reachable retry reuses only the original disposer; pending and repeated clicks cannot replace or multiply it',async()=>{
 let finish!:()=>void;const pending=new Promise<void>(r=>{finish=r}),dispose=vi.fn<()=>Promise<void>>().mockRejectedValueOnce(Error('public failure')).mockImplementation(()=>pending),f=fixture(dispose)
 f['will-quit']({preventDefault:vi.fn()});await f.draftCodecClosing;const w=f.windows[0],retry=()=>w.webContents.emit('will-navigate',{preventDefault:vi.fn()},'hsc-draft-quit:retry')
 retry();expect(f.app.quit).toHaveBeenCalledOnce();expect(f.quitAfterServiceStop).toBe(true);f['will-quit']({preventDefault:vi.fn()});await Promise.resolve();retry();expect(f.app.quit).toHaveBeenCalledOnce();expect(dispose).toHaveBeenCalledTimes(2)
 finish();await f.draftCodecClosing;expect(f.draftCodecDispose).toBeUndefined();expect(f.draftShutdownFailed).toBe(false);expect(f.app.quit).toHaveBeenCalledTimes(2);expect(f.resumeDraftPreparation).not.toHaveBeenCalled()
})
it('repeated forced, nonzero or unknown original close failures remain HOLD and never grant a fresh service or codec',async()=>{
 for(const code of ['DRAFT_RECOVERY_ORIGINAL_CLOSE_UNCONFIRMED','DRAFT_RECOVERY_IO']){
  const dispose=vi.fn(async()=>{throw Error(code)}),f=fixture(dispose);f['will-quit']({preventDefault:vi.fn()});await f.draftCodecClosing
  f['will-quit']({preventDefault:vi.fn()});await f.draftCodecClosing
  expect(f.draftCodecDispose).toBe(dispose);expect(f.windows).toHaveLength(1);expect(f.app.quit).not.toHaveBeenCalled();expect(f.draftShutdownFailed).toBe(true);expect(f.draftQuitDraining).toBe(true);expect(f.localService.start).not.toHaveBeenCalled();expect(f.resumeDraftPreparation).not.toHaveBeenCalled()
 }
})
it('closing the HOLD window or using the app quit action retries safe disposal without running normal shutdown or authorization again',async()=>{
 const f=fixture(vi.fn(async()=>{throw Error('private')}));f['will-quit']({preventDefault:vi.fn()});await f.draftCodecClosing
 const event={preventDefault:vi.fn()};f.windows[0].emit('close',event);expect(event.preventDefault).toHaveBeenCalledOnce();expect(f.app.quit).toHaveBeenCalledOnce()
 f.quitAfterServiceStop=false;f['before-quit']({preventDefault:vi.fn()});expect(f.app.quit).toHaveBeenCalledTimes(2);expect(f.quitAfterServiceStop).toBe(true);expect(f.localService.start).not.toHaveBeenCalled();expect(f.resumeDraftPreparation).not.toHaveBeenCalled()
})
