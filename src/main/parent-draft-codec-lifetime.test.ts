// @vitest-environment node
// Executes only exact index.ts shutdown callbacks; no Electron or product startup.
import * as fs from 'node:fs'
import * as vm from 'node:vm'
import {createRequire} from 'node:module'
import {describe,expect,it,vi} from 'vitest'
const parser=createRequire(import.meta.url)('@babel/parser')
const source=fs.readFileSync(new URL('./index.ts',import.meta.url),'utf8')
const parsed=parser.parse(source,{sourceType:'module',plugins:['typescript']})
let expression='',willQuit=''
function visit(node:any){
 if(!node||typeof node!=='object')return
 if(node.type==='VariableDeclarator'&&node.id.name==='verifiedQuit')expression=source.slice(node.init.arguments[1].start,node.init.arguments[1].end)
 if(node.type==='CallExpression'&&node.callee.type==='MemberExpression'&&node.callee.object.name==='app'&&node.callee.property.name==='on'&&node.arguments[0].value==='will-quit')willQuit=source.slice(node.arguments[1].start,node.arguments[1].end)
 for(const value of Object.values(node)){if(Array.isArray(value))value.forEach(visit);else if(value&&typeof value==='object')visit(value)}
}
visit(parsed)
function fixture(dispose=vi.fn(),stop=vi.fn(async()=>{})){
 const state:any={runtimeAwake:{clear:vi.fn()},diagnosticRecorder:{record:vi.fn(async()=>{}),flush:vi.fn(async()=>{})},pilotTestEmail:{stop:vi.fn()},localService:{stop},draftCodecDispose:dispose,draftCodecClosing:null,draftPreparation:undefined,resumeDraftPreparation:vi.fn(),showDraftShutdownFailure:vi.fn(),draftShutdownFailed:false,quitAfterServiceStop:false,app:{quit:vi.fn()}}
 vm.runInNewContext('globalThis.confirmed = '+expression+';globalThis.willQuit = '+willQuit,state)
 return state
}
describe('index-owned will-quit disposal after renderer shutdown',()=>{
 it('keeps the codec through service close and final renderer unload checkpoint; closes before final exit',async()=>{
  let finish!:()=>void;const order:string[]=[],dispose=vi.fn(()=>order.push('dispose')),f=fixture(dispose,vi.fn(()=>new Promise<void>(r=>{finish=r})))
  const closing=f.confirmed();expect(dispose).not.toHaveBeenCalled();await Promise.resolve();finish();await closing
  expect(f.app.quit).toHaveBeenCalledTimes(1);expect(f.quitAfterServiceStop).toBe(true);expect(dispose).not.toHaveBeenCalled()
  // Electron closes renderer windows/unload before emitting will-quit.
  order.push('final-checkpoint');const event={preventDefault:vi.fn()};f.willQuit(event);await f.draftCodecClosing;order.push('final-exit')
  expect(order).toEqual(['final-checkpoint','dispose','final-exit']);expect(event.preventDefault).toHaveBeenCalledTimes(1);expect(f.draftCodecDispose).toBeUndefined();const final={preventDefault:vi.fn()};f.willQuit(final);expect(final.preventDefault).not.toHaveBeenCalled()
 })
 it('missing original child close cancels will-quit with finite diagnostic, retaining disposal for retry',async()=>{
  const dispose=vi.fn<()=>void>(()=>{throw Error('synthetic close missing')}),f=fixture(dispose),event={preventDefault:vi.fn()};f.quitAfterServiceStop=true;f.willQuit(event);await f.draftCodecClosing
  expect(event.preventDefault).toHaveBeenCalledTimes(1);expect(f.quitAfterServiceStop).toBe(false);expect(f.draftCodecDispose).toBe(dispose)
  expect(f.showDraftShutdownFailure).toHaveBeenCalledOnce();expect(f.resumeDraftPreparation).not.toHaveBeenCalled();expect(f.draftShutdownFailed).toBe(true)
  expect(f.diagnosticRecorder.record).toHaveBeenCalledWith(expect.objectContaining({event:'draft_codec_dispose_failed',details:{error_code:'DRAFT_RECOVERY_IO'}}))
  dispose.mockImplementation(()=>{});f.willQuit({preventDefault:vi.fn()});await f.draftCodecClosing;expect(f.draftCodecDispose).toBeUndefined()
 })
 it('preserves original verified-service close/quit callback bytes and permits no early disposal',()=>{
  expect(expression).not.toContain('draftCodecDispose');expect(willQuit).toContain('event.preventDefault()')
 })
})
