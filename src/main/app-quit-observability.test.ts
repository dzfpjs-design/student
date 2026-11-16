// @vitest-environment node
// Executes original index.ts callback bodies, not Electron or an actual window.
import {readFileSync}from'node:fs'
import {expect,it,vi}from'vitest'
import {parse}from'@babel/parser'
import {stripTypeScriptTypes}from'node:module'
const text=readFileSync(new URL('./index.ts',import.meta.url),'utf8')
const tree=parse(text,{sourceType:'module',plugins:['typescript']})
function callback(event:string){let selected:any;function walk(n:any){if(!n||typeof n!=='object')return
 if(n.type==='CallExpression'){
  const callee=text.slice(n.callee.start,n.callee.end)
  if(event==='confirmed'&&callee==='createVerifiedQuit')selected=n.arguments[1]
  if(n.arguments[0]?.type==='StringLiteral'&&n.arguments[0].value===event&&['app.on','mainWindow.webContents.on'].includes(callee))selected=n.arguments[1]
 }
 for(const[key,value]of Object.entries(n)){if(['loc','start','end','comments','tokens'].includes(key))continue;if(Array.isArray(value))for(const child of value)walk(child);else if(value&&typeof value==='object')walk(value)}
 }walk(tree);expect(selected,'original callback '+event).toBeDefined();return text.slice(selected.start,selected.end)}
function runtime(event:string,overrides:Record<string,unknown>={}){
 const events:string[]=[],app={quit:vi.fn()},prevent=vi.fn(),dispose=vi.fn(async()=>{})
 const deps={diagnosticRecorder:{record:vi.fn((e:{event:string})=>{events.push(e.event);return Promise.resolve()}),flush:vi.fn(async()=>{})},localService:{stop:vi.fn(async()=>{}),start:vi.fn(async()=>{})},runtimeAwake:{clear:vi.fn()},pilotTestEmail:{stop:vi.fn()},dialog:{showMessageBox:vi.fn(async()=>{})},recovery:{resume:vi.fn()},preparation:{quiesce:vi.fn(async()=>{}),resume:vi.fn()},showDraftShutdownFailure:vi.fn(),app,dispose,...overrides}
 const names=['quitAfterServiceStop','draftRecovery','draftCodecDispose','draftCodecClosing','draftPreparation','draftQuitDraining','draftShutdownFailed']
 const bindings=names.map(name=>{const declaration=tree.program.body.find((n:any)=>n.type==='VariableDeclaration'&&n.declarations.some((d:any)=>d.id?.name===name));expect(declaration,'original state '+name).toBeDefined();return text.slice(declaration!.start!,declaration!.end!)}).join('\n')
 const resume=tree.program.body.find((n:any)=>n.type==='FunctionDeclaration'&&n.id?.name==='resumeDraftPreparation');expect(resume,'original resume function').toBeDefined()
 const js='const selected=('+callback(event)+');'
 const selected=new Function(...Object.keys(deps),stripTypeScriptTypes(bindings+'\n'+text.slice(resume!.start!,resume!.end!))+';draftRecovery=recovery;draftPreparation=preparation;draftCodecDispose=dispose;'+js+';return {invoke:selected,flag:()=>quitAfterServiceStop,pending:()=>draftCodecClosing,draining:()=>draftQuitDraining,codec:()=>draftCodecDispose,requestUnloadState:()=>{draftQuitDraining=true;quitAfterServiceStop=true}}')(...Object.values(deps))
 return{...selected,events,app,prevent,dispose,deps}
}
it('quit metadata waits for original preparation, stop and flush in order before final quit',async()=>{
 let quiesce!:()=>void,stop!:()=>void,flush!:()=>void
 const quiesced=new Promise<void>(r=>{quiesce=r}),stopped=new Promise<void>(r=>{stop=r}),flushed=new Promise<void>(r=>{flush=r})
 const stopService=vi.fn(()=>stopped),prepare=vi.fn(()=>quiesced)
 const r=runtime('confirmed',{localService:{stop:stopService},preparation:{quiesce:prepare,resume:vi.fn()}})
 r.deps.diagnosticRecorder.flush.mockImplementation(()=>flushed)
 const pending=r.invoke();expect(r.events).toEqual(['app_quit_requested']);expect(prepare).toHaveBeenCalledOnce();expect(stopService).not.toHaveBeenCalled();expect(r.app.quit).not.toHaveBeenCalled()
 quiesce();await vi.waitFor(()=>expect(stopService).toHaveBeenCalledOnce());expect(r.deps.diagnosticRecorder.flush).not.toHaveBeenCalled()
 stop();await vi.waitFor(()=>expect(r.events).toEqual(['app_quit_requested','app_quit_service_closed']));expect(r.app.quit).not.toHaveBeenCalled();expect(r.deps.diagnosticRecorder.flush).toHaveBeenCalledOnce()
 flush();await pending
 expect(r.events).toEqual(['app_quit_requested','app_quit_service_closed','app_quit_diagnostics_flushed','app_quit_final_requested']);expect(r.flag()).toBe(true);expect(r.app.quit).toHaveBeenCalledOnce()
})
it('quit metadata keeps unknown service closure blocked with original warning and no final quit',async()=>{
 const r=runtime('confirmed',{localService:{stop:async()=>{throw Error('synthetic unknown')}}});await r.invoke()
 expect(r.events).toEqual(['app_quit_requested','graceful_shutdown_failed']);expect(r.app.quit).not.toHaveBeenCalled();expect(r.flag()).toBe(false);expect(r.deps.dialog.showMessageBox).toHaveBeenCalledOnce()
 expect(r.deps.preparation.quiesce).toHaveBeenCalledOnce();expect(r.deps.preparation.resume).toHaveBeenCalledOnce();expect(r.deps.recovery.resume).toHaveBeenCalledOnce();expect(r.draining()).toBe(false)
})
it('quit metadata vetoes reentry until original async codec disposal completes exactly once',async()=>{
 let close!:()=>void;const closed=new Promise<void>(resolve=>{close=resolve}),dispose=vi.fn(()=>closed)
 const r=runtime('will-quit',{dispose});r.invoke({preventDefault:r.prevent});const pending=r.pending();r.invoke({preventDefault:r.prevent})
 expect(r.prevent).toHaveBeenCalledTimes(2);expect(r.pending()).toBe(pending);expect(r.app.quit).not.toHaveBeenCalled();expect(r.events).toEqual(['app_quit_will_quit','app_quit_will_quit'])
 await vi.waitFor(()=>expect(dispose).toHaveBeenCalledOnce());expect(r.app.quit).not.toHaveBeenCalled();close();await pending
 expect(r.events).toEqual(['app_quit_will_quit','app_quit_will_quit','app_quit_codec_disposed']);expect(r.codec()).toBeUndefined();expect(r.pending()).toBeNull();expect(r.app.quit).toHaveBeenCalledOnce()
 r.invoke({preventDefault:r.prevent});expect(r.prevent).toHaveBeenCalledTimes(2);expect(dispose).toHaveBeenCalledOnce();expect(r.app.quit).toHaveBeenCalledOnce()
})
it.each(['throw','reject'])('quit metadata preserves %s codec failure veto and records finite failure without automatic retry',async kind=>{
 const dispose=vi.fn(()=>{if(kind==='throw')throw Error('synthetic private failure');return Promise.reject(Error('synthetic private failure'))});const r=runtime('will-quit',{dispose});r.invoke({preventDefault:r.prevent});await r.pending()
 expect(dispose).toHaveBeenCalledOnce();expect(r.prevent).toHaveBeenCalledOnce();expect(r.flag()).toBe(false);expect(r.events).toEqual(['app_quit_will_quit','draft_codec_dispose_failed']);expect(r.app.quit).not.toHaveBeenCalled()
 expect(r.pending()).toBeNull();expect(r.codec()).toBe(dispose);expect(r.deps.preparation.resume).not.toHaveBeenCalled();expect(r.deps.recovery.resume).not.toHaveBeenCalled();expect(r.draining()).toBe(true);expect(r.deps.showDraftShutdownFailure).toHaveBeenCalledOnce();expect(r.deps.localService.start).not.toHaveBeenCalled()
 expect(r.deps.diagnosticRecorder.record).toHaveBeenLastCalledWith({source:'main',event:'draft_codec_dispose_failed',level:'error',details:{error_code:'DRAFT_RECOVERY_IO'}})
})
it('quit metadata unload listener never bypasses the original beforeunload veto',()=>{
 const r=runtime('will-prevent-unload');r.invoke({preventDefault:r.prevent});expect(r.prevent).not.toHaveBeenCalled();expect(r.events).toEqual(['app_quit_unload_prevented']);expect(r.app.quit).not.toHaveBeenCalled()
 expect(r.deps.localService.start).not.toHaveBeenCalled();r.requestUnloadState();r.invoke({preventDefault:r.prevent});expect(r.prevent).not.toHaveBeenCalled();expect(r.app.quit).not.toHaveBeenCalled();expect(r.deps.localService.start).toHaveBeenCalledOnce();expect(r.flag()).toBe(false);expect(r.draining()).toBe(false);expect(r.deps.preparation.resume).toHaveBeenCalledOnce();expect(r.deps.recovery.resume).toHaveBeenCalledOnce()
})
