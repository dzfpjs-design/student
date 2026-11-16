// @vitest-environment node
// Real ChildProcess regression. Only the service executable is a synthetic HTTP
// child; the normal LocalServiceManager, options, timers and lifecycle are real.
import{afterEach,beforeEach,expect,it,vi}from'vitest'
import{mkdtempSync,rmSync,readFileSync,existsSync}from'node:fs'
import{tmpdir}from'node:os'
import{join}from'node:path'
const f=vi.hoisted(()=>({children:[]as any[],ports:[]as number[],starts:0,root:'',mode:''}))
vi.mock('electron',()=>({app:{getVersion:()=> '0.1.4',isPackaged:false,getAppPath:()=>process.cwd(),getPath:()=>f.root}}))
vi.mock('node:child_process',async()=>{const actual=await vi.importActual<any>('node:child_process');return{...actual,spawn:(_exe:string,args:string[],options:any)=>{
 const port=Number(args[args.indexOf('--port')+1]);f.ports.push(port);f.starts++
 const first=f.starts===1,late=f.mode==='startup'&&first,forced=f.mode==='forced'&&first,normalDelay=f.mode==='normal-delay'&&first,durable=f.mode==='durable-finalizer'&&first
 const marker=JSON.stringify(join(f.root,'committed-finalizer.txt')),requests=JSON.stringify(join(f.root,'shutdown-count.txt'))
 const script=`const http=require('node:http'),fs=require('node:fs'),{spawn}=require('node:child_process');let held=false;const server=http.createServer((req,res)=>{res.writeHead(req.url==='/v1/runtime/shutdown'?202:200);res.end('{"build_component":"home-study-local-agent","build_version":"0.1.4"}');if(req.url==='/crash'){spawn(process.execPath,['-e','setTimeout(()=>{},1800)'],{stdio:['ignore','inherit','inherit']});setTimeout(()=>process.exit(7),15)}else if(req.url==='/v1/runtime/shutdown'){if(${durable}){fs.appendFileSync(${requests},'1');setTimeout(()=>{fs.writeFileSync(${marker},'committed');server.closeAllConnections();server.close(()=>process.exit(0))},3300)}else if(${late}){if(!held){held=true;spawn(process.execPath,['-e','setTimeout(()=>{},${LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS+3000})'],{stdio:['ignore','inherit','inherit']})}}else if(${durable}){}else if(${normalDelay}){spawn(process.execPath,['-e','setTimeout(()=>{},450)'],{stdio:['ignore','inherit','inherit']});setTimeout(()=>process.exit(0),20)}else if(!${forced})setTimeout(()=>process.exit(0),10)}}).listen(${port},'127.0.0.1');`
 const child=actual.spawn(process.execPath,['-e',script],options)
 child.once('exit',(code:number|null,signal:string|null)=>{child.__testExit={code,signal}})
 child.once('close',(code:number|null,signal:string|null)=>{child.__testClose={code,signal}})
 f.children.push(child);return child
}}})
import{LocalServiceManager,LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS}from'./local-service'
let manager:LocalServiceManager|null=null
const closed=(c:any)=>new Promise<void>(r=>{if(c.__testClose)r();else c.once('close',()=>r())})
beforeEach(()=>{f.children=[];f.ports=[];f.starts=0;f.root=mkdtempSync(join(tmpdir(),'synthetic-original-child-'));vi.stubEnv('HSC_LOCAL_AGENT_PYTHON',process.execPath);vi.stubEnv('HSC_LOCAL_API_URL','');vi.stubEnv('HSC_RUNTIME_MODE','formal')})
afterEach(async()=>{if(manager)await manager.stop().catch((error:any)=>{if(!['LOCAL_SERVICE_SHUTDOWN_PENDING','LOCAL_SERVICE_SHUTDOWN_ABNORMAL'].includes(error?.code))throw error});for(const c of f.children)if(!c.__testClose){if(c.exitCode===null&&c.signalCode===null)c.kill('SIGKILL');await closed(c)}manager=null;vi.restoreAllMocks();vi.unstubAllEnvs();rmSync(f.root,{recursive:true,force:true})})
function fresh(){manager=new LocalServiceManager();manager.setDataEncryptionKey(Buffer.alloc(32,5).toString('base64'));return manager}
it('quit metadata process_closed follows the real original stdio close rather than exit',async()=>{
 f.mode='normal-delay';const events:any[]=[];manager=new LocalServiceManager(event=>events.push(event));manager.setDataEncryptionKey(Buffer.alloc(32,5).toString('base64'));await manager.start();const child=f.children[0]
 const exited=new Promise<void>(resolve=>child.once('exit',()=>resolve())),stopping=manager.stop();await exited
 expect(events.some(event=>event.event==='process_exited')).toBe(true);expect(events.some(event=>event.event==='process_closed')).toBe(false)
 await stopping;await closed(child);expect(events.filter(event=>event.event==='process_closed')).toEqual([{event:'process_closed',level:'info',message:undefined,details:{exitCode:0,exitSignal:null,expected:true}}]);expect(child.__testClose).toEqual({code:0,signal:null});expect(f.starts).toBe(1)
},4000)
it('recovers with original finite backoff after an unexpected old exit and late stdio close',async()=>{
 f.mode='unexpected';const m=fresh();await m.start();const old=f.children[0]
 await fetch(`http://127.0.0.1:${f.ports[0]}/crash`,{method:'POST'})
 await new Promise(r=>setTimeout(r,1250));expect(old.__testExit.code).toBe(7);expect(old.__testClose).toBeUndefined();expect(f.starts).toBe(1)
 await closed(old);expect(f.starts).toBe(1)
 await vi.waitFor(()=>{expect(f.starts).toBe(2);expect(m.getRuntime().status).toBe('ready')},{timeout:5000,interval:50})
},9000)
it('preserves startup-failure recovery responsibility across unknown stopChild(true)',async()=>{
 f.mode='startup';const m=fresh(),original=(m as any).waitUntilHealthy.bind(m)
 vi.spyOn(m as any,'waitUntilHealthy').mockImplementationOnce(async(...args:any[])=>{await original(...args);throw Error('synthetic health-phase failure')})
 await expect(m.start()).rejects.toThrow('尚未确认后台保存并退出');const old=f.children[0]
 expect(m.getRuntime().status).toBe('error');expect(f.starts).toBe(1);expect(old.__testClose).toBeUndefined()
 await closed(old);expect(f.starts).toBe(1)
 await vi.waitFor(()=>{expect(f.starts).toBe(2);expect(m.getRuntime().status).toBe('ready')},{timeout:5000,interval:50})
},LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS+10000)
it('explicit stop cancels the same startup recovery responsibility without replacing before or after close',async()=>{
 f.mode='startup';const m=fresh(),original=(m as any).waitUntilHealthy.bind(m)
 vi.spyOn(m as any,'waitUntilHealthy').mockImplementationOnce(async(...args:any[])=>{await original(...args);throw Error('synthetic health-phase failure')})
 await expect(m.start()).rejects.toThrow('尚未确认后台保存并退出');const old=f.children[0]
 await expect(m.stop()).rejects.toThrow('尚未确认后台保存并退出')
 expect(f.starts).toBe(1);await closed(old);await expect(m.stop()).rejects.toMatchObject({code:'LOCAL_SERVICE_SHUTDOWN_ABNORMAL'});await new Promise(r=>setTimeout(r,1250))
 expect(f.starts).toBe(1);expect(m.getRuntime().status).toBe('error');expect(m.captureAbnormalShutdownAcknowledgement()?.()).toBe(true)
},LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS+9000)

it('never forces normal stop or credits a later external SIGKILL as saved',async()=>{
 f.mode='forced';const m=fresh();await m.start();const original=f.children[0]
 const results=await Promise.allSettled([m.stop(),m.stop()])
 expect(results.every(r=>r.status==='rejected'&&(r.reason as any).code==='LOCAL_SERVICE_SHUTDOWN_PENDING')).toBe(true)
 expect(original.__testExit).toBeUndefined();expect(original.__testClose).toBeUndefined()
 // Public fixture cleanup represents an external OS termination, not product success.
 original.kill('SIGKILL');await closed(original)
 await expect(m.stop()).rejects.toMatchObject({code:'LOCAL_SERVICE_SHUTDOWN_ABNORMAL'})
 expect(original.__testExit).toEqual({code:null,signal:'SIGKILL'})
 expect(original.__testClose).toEqual({code:null,signal:'SIGKILL'})
 expect(m.getRuntime().status).toBe('error');expect(f.starts).toBe(1)
},LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS+4000)
it('keeps stdio closure separate from an earlier normal exit of the same original child',async()=>{
 f.mode='normal-delay';const m=fresh();await m.start();const original=f.children[0],at=performance.now()
 await m.stop()
 expect(original.__testExit).toEqual({code:0,signal:null});expect(original.__testClose).toEqual({code:0,signal:null})
 expect(performance.now()-at).toBeGreaterThan(350);expect(m.getRuntime().status).toBe('stopped');expect(f.starts).toBe(1)
},4000)

// A real original child needs its durable finalizer and both exit/stdio-close.
it('lets the original child persist its 3.3s finalizer before natural close',async()=>{
 f.mode='durable-finalizer';const events:any[]=[];manager=new LocalServiceManager(value=>events.push(value));const m=manager;m.setDataEncryptionKey(Buffer.alloc(32,5).toString('base64'));await m.start();const original=f.children[0],at=performance.now()
 await Promise.all([m.stop(),m.stop()])
 expect(original.__testExit).toEqual({code:0,signal:null});expect(original.__testClose).toEqual({code:0,signal:null})
 expect(existsSync(join(f.root,'committed-finalizer.txt'))).toBe(true);expect(readFileSync(join(f.root,'committed-finalizer.txt'),'utf8')).toBe('committed')
 expect(readFileSync(join(f.root,'shutdown-count.txt'),'utf8')).toBe('1');expect(events.filter(e=>e.event==='force_stop')).toHaveLength(0)
 expect(performance.now()-at).toBeGreaterThanOrEqual(3200);expect(m.getRuntime().status).toBe('stopped');expect(f.starts).toBe(1)
},8000)
it('does not replace an old process whose shutdown cannot be confirmed after a tutor change',async()=>{
 f.mode='startup';const m=fresh();await m.start();const old=f.children[0]
 await expect(m.applyTutorExplanationConfig({apiKey:'synthetic-tutor-key-123',baseUrl:'https://new.example.test/v1',model:'deepseek-test'})).rejects.toThrow()
 expect(f.starts).toBe(1);expect(old.__testClose).toBeUndefined();expect(m.cloudProviderRevisionApplied(m.getCloudProviderRevision())).toBe(false)
 await expect(m.start()).rejects.toThrow()
 old.kill('SIGKILL');await closed(old)
},LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS+10000)
