// @vitest-environment node
import{afterEach,beforeEach,expect,it,vi}from'vitest'
import{mkdtempSync,readFileSync,rmSync,existsSync}from'node:fs'
import{join}from'node:path'
import{tmpdir}from'node:os'
const f=vi.hoisted(()=>({root:'',mode:'normal',starts:0,children:[]as any[],ports:[]as number[]}))
vi.mock('electron',()=>({app:{getVersion:()=> '0.1.4',isPackaged:false,getAppPath:()=>process.cwd(),getPath:()=>f.root}}))
vi.mock('node:child_process',async()=>{const actual=await vi.importActual<any>('node:child_process');return{...actual,spawn:(_e:string,args:string[],opts:any)=>{
 const port=Number(args[args.indexOf('--port')+1]);f.ports.push(port);f.starts++
 const mode=f.starts===1?f.mode:'normal',count=JSON.stringify(join(f.root,'shutdown-count')),saved=JSON.stringify(join(f.root,'saved'))
 const program=`const http=require('node:http'),fs=require('node:fs');let stopping=false;const finish=code=>{if(code===0)fs.writeFileSync(${saved},'committed');server.closeAllConnections();server.close(()=>process.exit(code))};const server=http.createServer((req,res)=>{res.end('{"build_component":"home-study-local-agent","build_version":"0.1.4"}');if(req.url==='/v1/runtime/shutdown'){fs.appendFileSync(${count},'1');stopping=true;if(${JSON.stringify(mode)}==='normal')setTimeout(()=>finish(0),20);else if(${JSON.stringify(mode)}==='abnormal')setTimeout(()=>finish(7),20)}else if(req.url==='/finish'&&stopping)setTimeout(()=>finish(0),20)}).listen(${port},'127.0.0.1')`
 const child=actual.spawn(process.execPath,['-e',program],opts);vi.spyOn(child,'kill');child.once('exit',(code:any,signal:any)=>child.actualExit={code,signal});child.once('close',(code:any,signal:any)=>child.actualClose={code,signal});f.children.push(child);return child
}}})
import{LocalServiceManager,LOCAL_SERVICE_GRACEFUL_SHUTDOWN_TIMEOUT_MS}from'./local-service'
let service:any
const close=(c:any)=>new Promise<void>(yes=>{if(c.actualClose)yes();else c.once('close',()=>yes())})
beforeEach(()=>{f.root=mkdtempSync(join(tmpdir(),'public-shutdown-real-child-'));f.starts=0;f.children=[];f.ports=[];f.mode='normal';vi.stubEnv('HSC_LOCAL_AGENT_PYTHON',process.execPath);vi.stubEnv('HSC_LOCAL_API_URL','');vi.stubEnv('HSC_RUNTIME_MODE','formal')})
afterEach(async()=>{for(const c of f.children)if(!c.actualClose){if(c.exitCode===null&&c.signalCode===null)c.kill('SIGKILL');await close(c)}service=undefined;vi.restoreAllMocks();vi.unstubAllEnvs();rmSync(f.root,{recursive:true,force:true})})
it('normal stop revokes an already waiting startup cleanup force and preserves the original durable finalizer',async()=>{
 f.mode='slow'
 const events:any[]=[];service=new LocalServiceManager((e:any)=>events.push(e));const m=service
 m.setDataEncryptionKey(Buffer.alloc(32,7).toString('base64'))
 const healthy=(m as any).waitUntilHealthy.bind(m)
 vi.spyOn(m as any,'waitUntilHealthy').mockImplementationOnce(async(...args:any[])=>{await healthy(...args);throw Error('PUBLIC_STARTUP_HEALTH_FAILURE')})
 const starting=m.start().then(()=>null,(error:any)=>error)
 await vi.waitFor(()=>expect(existsSync(join(f.root,'shutdown-count'))).toBe(true),{timeout:3000,interval:20})
 const child=f.children[0]
 const stopping=m.stop().then(()=>null,(error:any)=>error)
 const [startError,stopError]=await Promise.all([starting,stopping])
 expect(child.kill).not.toHaveBeenCalled()
 expect(events.filter((event:any)=>event.event==='force_stop')).toHaveLength(0)
 expect(child.actualExit).toBeUndefined();expect(child.actualClose).toBeUndefined()
 expect(startError).toMatchObject({code:'LOCAL_SERVICE_SHUTDOWN_PENDING'})
 expect(stopError).toMatchObject({code:'LOCAL_SERVICE_SHUTDOWN_PENDING'})
 expect(existsSync(join(f.root,'saved'))).toBe(false)
 expect(readFileSync(join(f.root,'shutdown-count'),'utf8')).toBe('1');expect(f.starts).toBe(1)
 const receipts:any[]=[];m.observeShutdownClosure((receipt:any)=>receipts.push(receipt))
 await fetch(`http://127.0.0.1:${f.ports[0]}/finish`);await close(child)
 expect(receipts).toHaveLength(1);expect(receipts[0].graceful).toBe(true);expect(receipts[0].isCurrent()).toBe(true)
 await m.stop()
 expect(readFileSync(join(f.root,'saved'),'utf8')).toBe('committed')
 expect(child.actualExit).toEqual({code:0,signal:null});expect(child.actualClose).toEqual(child.actualExit)
 await new Promise(resolve=>setTimeout(resolve,1250))
 expect(f.starts).toBe(1);expect(readFileSync(join(f.root,'shutdown-count'),'utf8')).toBe('1')
 expect(m.getRuntime().status).toBe('stopped')
},22000)
