// @vitest-environment node
// Executes original index callback and real LocalServiceManager with owned synthetic HTTP children.
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import * as vm from 'node:vm'
import {createRequire} from 'node:module'
import {transformSync} from 'esbuild'
import {ParentDraftRecoveryCoordinator,ParentDraftRecoveryStore} from './parent-draft-recovery'
const fixture=vi.hoisted(()=>({root:'',children:[] as any[]}))
vi.mock('electron',()=>({app:{getVersion:()=> '0.1.4',isPackaged:false,getAppPath:()=>process.cwd(),getPath:()=>fixture.root}}))
vi.mock('node:child_process',async()=>{const real=await vi.importActual<any>('node:child_process');return {...real,spawn:(_exe:string,args:string[],options:any)=>{
 const port=Number(args[args.indexOf('--port')+1])
 const script=`const http=require('node:http');const server=http.createServer((req,res)=>{res.writeHead(req.url==='/v1/runtime/shutdown'?202:200);res.end('{"build_component":"home-study-local-agent","build_version":"0.1.4"}');if(req.url==='/v1/runtime/shutdown'){server.closeAllConnections();server.close(()=>process.exit(0))}}).listen(${port},'127.0.0.1');`
 const child=real.spawn(process.execPath,['-e',script],options);child.once('exit',(code:number,signal:string|null)=>{child.originalExit={code,signal}});child.once('close',(code:number,signal:string|null)=>{child.originalClose={code,signal}});fixture.children.push(child);return child
}}})
import {LocalServiceManager} from './local-service'
const parser=createRequire(import.meta.url)('@babel/parser'),source=fs.readFileSync(new URL('./index.ts',import.meta.url),'utf8'),parsed=parser.parse(source,{sourceType:'module',plugins:['typescript']})
let prevented='',resume='',create=''
function visit(n:any){if(!n||typeof n!=='object')return
 if(n.type==='FunctionDeclaration'&&n.id?.name==='resumeDraftPreparation')resume=source.slice(n.start,n.end)
 if(n.type==='FunctionDeclaration'&&n.id?.name==='createWindow')create=source.slice(n.start,n.end)
 if(n.type==='CallExpression'&&n.callee?.type==='MemberExpression'&&n.callee.property.name==='on'&&n.arguments[0]?.value==='will-prevent-unload')prevented=source.slice(n.arguments[1].start,n.arguments[1].end)
 for(const v of Object.values(n)){if(Array.isArray(v))v.forEach(visit);else if(v&&typeof v==='object')visit(v)}
}
visit(parsed)
let manager:LocalServiceManager
beforeEach(()=>{fixture.root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'public-blocked-quit-')));fixture.children=[];vi.stubEnv('HSC_LOCAL_AGENT_PYTHON',process.execPath);vi.stubEnv('HSC_LOCAL_API_URL','');vi.stubEnv('HSC_RUNTIME_MODE','formal');manager=new LocalServiceManager();manager.setDataEncryptionKey(Buffer.alloc(32,7).toString('base64'))})
afterEach(async()=>{await manager.stop();expect(fixture.children.every(c=>c.originalExit?.code===0&&c.originalClose?.code===0&&c.originalClose.signal===null)).toBe(true);vi.unstubAllEnvs();fs.rmSync(fixture.root,{recursive:true,force:true})})
it('a real blocked unload resumes a fresh turn and the original service can return ready after its prior child closed',async()=>{
 await manager.start();await manager.stop();expect(fixture.children[0].originalClose).toEqual({code:0,signal:null})
 let finish!:()=>void;const gate=new Promise<void>(r=>{finish=r}),store=new ParentDraftRecoveryStore({userDataPath:fixture.root,profileId:'public-profile',codec:{assertAvailable(){},encrypt:v=>Buffer.from(v),decrypt:v=>v.toString()}})
 const c=new ParentDraftRecoveryCoordinator({store,prepare:()=>gate,authorize:async()=>({profileId:'public-profile'})})
 const old=c.prepare(7,'child','capture','public-parent'),reject=expect(old).rejects.toThrow();await Promise.resolve();c.quiesce()
 const state:any={draftQuitDraining:true,quitAfterServiceStop:true,draftCodecClosing:null,draftRecovery:c,draftPreparation:undefined,localService:manager,diagnosticRecorder:{record:vi.fn(async()=>{})}}
 vm.runInNewContext(transformSync(resume+'\nglobalThis.prevented='+prevented,{loader:'ts',format:'cjs'}).code,state);state.prevented()
 finish();await reject;await vi.waitFor(()=>expect(manager.getRuntime().status).toBe('ready'),{timeout:4000})
 expect(state.draftQuitDraining).toBe(false);expect(state.quitAfterServiceStop).toBe(false);expect((await c.prepare(7,'child','capture','fresh-parent')).protection).toBe('native_encrypted');expect(fixture.children).toHaveLength(2)
 state.prevented();await Promise.resolve();expect(fixture.children).toHaveLength(2)
})
it('the actual createWindow body returns before constructing any window while quit drain owns admission',()=>{
 const state:any={draftQuitDraining:true};vm.runInNewContext(transformSync('globalThis.create='+create,{loader:'ts',format:'cjs'}).code,state);expect(()=>state.create()).not.toThrow()
})
