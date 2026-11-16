// @vitest-environment node
import {createServer} from 'node:http'
import dns from 'node:dns'
import {afterEach,expect,it,vi} from 'vitest'
import {captureNativeWriting} from './native-writing'

afterEach(()=>vi.restoreAllMocks())
const input={child_id:'synthetic-child',session_id:'synthetic-session',prompt_id:'synthetic-prompt',
  item_version:'synthetic-version',diagnostic_item_id:'synthetic-item',text:'Private synthetic manuscript',client_action_id:'synthetic-action'}
async function listen(server:ReturnType<typeof createServer>):Promise<number>{
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve))
  return (server.address() as {port:number}).port
}
async function close(server:ReturnType<typeof createServer>):Promise<void>{
  server.closeAllConnections();await new Promise<void>(resolve=>server.close(()=>resolve()))
}
it('real loopback 307 cannot forward a manuscript or either credential to an external hostname',async()=>{
  let externalDns=0;const leaked:Array<{body:string;session:string|string[]|undefined;signature:string|string[]|undefined}>=[]
  const target=createServer((request,response)=>{
    let body='';request.on('data',chunk=>body+=chunk);request.on('end',()=>{
      leaked.push({body,session:request.headers['x-hsc-session'],signature:request.headers['x-hsc-native-writing']})
      response.writeHead(200,{'Content-Type':'application/json'});response.end('{}')
    })
  })
  const targetPort=await listen(target)
  const originalLookup=dns.lookup
  // The forbidden hostname maps ONLY to a second owned loopback sink. No internet.
  vi.spyOn(dns,'lookup').mockImplementation(((host:string,options:any,callback:any)=>{
    if(host==='writing-external.invalid'){
      externalDns++
      const done=typeof options==='function'?options:callback
      return done(null,options?.all?[{address:'127.0.0.1',family:4}]:'127.0.0.1',4)
    }
    return (originalLookup as any)(host,options,callback)
  }) as typeof dns.lookup)
  const origin=createServer((_request,response)=>{
    response.writeHead(307,{Location:`http://writing-external.invalid:${targetPort}/collect`});response.end()
  })
  const originPort=await listen(origin)
  try{
    await captureNativeWriting(input,{status:'ready',localApiUrl:`http://127.0.0.1:${originPort}`,sessionToken:'synthetic-session-secret'},()=> 'synthetic-signature').catch(()=>{})
    expect({externalDns,leaked}).toEqual({externalDns:0,leaked:[]})
  }finally{await close(origin);await close(target)}
})
it.each(['http://user:password@127.0.0.1:1234','http://127.0.0.1:1234/?secret=x','http://127.0.0.1:1234/#secret',
  'http://127.0.0.1:1234/subpath','https://127.0.0.1:1234','http://external.invalid:1234'])('rejects decorated/nonlocal runtime URL %s before transport',async localApiUrl=>{
  const fetch=vi.spyOn(globalThis,'fetch').mockRejectedValue(Error('network should not run'))
  await expect(captureNativeWriting(input,{status:'ready',localApiUrl,sessionToken:'secret'},()=> 'signature')).rejects.toThrow()
  expect(fetch).not.toHaveBeenCalled()
})
it('a real loopback 503 after capture is reported as an unknown receipt, not a rejected manuscript or a retry',async()=>{
  let posts=0
  const server=createServer((request,response)=>{posts++;request.resume();response.writeHead(503);response.end()})
  const port=await listen(server)
  try{
    await expect(captureNativeWriting(input,{status:'ready',localApiUrl:`http://127.0.0.1:${port}`,sessionToken:'synthetic'},()=> 'synthetic-signature'))
      .rejects.toThrow('原稿采集回执未确认')
    expect(posts).toBe(1)
  }finally{await close(server)}
})
