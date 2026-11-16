import { afterEach,expect,it,vi } from 'vitest'
afterEach(()=>{vi.unstubAllGlobals();vi.resetModules();delete window.desktop})
const response=(body:unknown,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})
it('does not let an old 401 revoke a newer PIN, and sends only the current credential on a new request',async()=>{
 vi.resetModules();delete window.desktop
 let release!:(r:Response)=>void,unlocks=0;const headers:string[]=[]
 vi.stubGlobal('fetch',vi.fn(async(url:string,init?:RequestInit)=>{
  if(String(url).endsWith('/unlock'))return response({parent_token:++unlocks===1?'public-token-a':'public-token-b'})
  headers.push(new Headers(init?.headers).get('X-HSC-Parent')??'')
  if(String(url).endsWith('/old'))return new Promise<Response>(r=>{release=r})
  return response({ok:true})
 }))
 const {localApi}=await import('./api');const a=await import('./parentDraftAuthority')
 await localApi.parentUnlock('1234',false);const old=localApi.knowledgeGraphRequest('/v1/parent/knowledge-graph/old')
 await vi.waitFor(()=>expect(release).toBeTypeOf('function'));await localApi.parentUnlock('5678',false)
 const revision=a.parentDraftAuthorityVersion();release(response({detail:'expired old request'},401));await expect(old).rejects.toThrow()
 expect(a.parentDraftAuthorityVersion()).toBe(revision)
 await localApi.knowledgeGraphRequest('/v1/parent/knowledge-graph/new');expect(headers).toEqual(['public-token-a','public-token-b'])
})
it('revokes the current editor authority on current credential 401 and on service reset',async()=>{
 vi.resetModules();delete window.desktop
 vi.stubGlobal('fetch',vi.fn(async(url:string)=>String(url).endsWith('/unlock')?response({parent_token:'public-only-token'}):response({detail:'expired'},401)))
 const {localApi,resetRuntimeCache}=await import('./api');const a=await import('./parentDraftAuthority')
 await localApi.parentUnlock('1234',false);const unlocked=a.parentDraftAuthorityVersion()
 await expect(localApi.knowledgeGraphRequest('/v1/parent/knowledge-graph/current')).rejects.toThrow()
 expect(a.parentDraftAuthorityVersion()).toBe(unlocked+1);resetRuntimeCache();expect(a.parentDraftAuthorityVersion()).toBe(unlocked+2)
})
it('rejects a concurrent old unlock result after the first completed unlock changes authority',async()=>{
 vi.resetModules();delete window.desktop
 let first!:(r:Response)=>void,calls=0
 vi.stubGlobal('fetch',vi.fn(async()=>++calls===1?new Promise<Response>(r=>{first=r}):response({parent_token:'public-new-token'})))
 const {localApi}=await import('./api');await import('./parentDraftAuthority')
 const old=localApi.parentUnlock('1111',false);await vi.waitFor(()=>expect(first).toBeTypeOf('function'));await localApi.parentUnlock('2222',false)
 first(response({parent_token:'public-old-token'}));await expect(old).rejects.toThrow(/重新解锁/)
})
it('does not start a parent HTTP mutation after a lock during the original base URL await',async()=>{
 vi.resetModules();delete window.desktop
 const fetch=vi.fn(async()=>response({parent_token:'public-only-token'}));vi.stubGlobal('fetch',fetch)
 const {localApi}=await import('./api');const a=await import('./parentDraftAuthority')
 await localApi.parentUnlock('1234',false)
 const pending=localApi.knowledgeGraphRequest('/v1/parent/knowledge-graph/textbook-capture/a/drafts/capture',{method:'PUT',body:'{}'})
 a.changeParentDraftAuthority('locked');await expect(pending).rejects.toThrow(/家长身份已变化/);expect(fetch).toHaveBeenCalledTimes(1)
})
it('does not start an original-page fetch when the service lookup returns after lock',async()=>{
 vi.resetModules();let release!:(config:unknown)=>void
 window.desktop={getRuntimeConfig:()=>new Promise(r=>{release=r})}as unknown as Window['desktop']
 const fetch=vi.fn();vi.stubGlobal('fetch',fetch)
 const {requestParentTextbookSource}=await import('./api');const a=await import('./parentDraftAuthority')
 const pending=requestParentTextbookSource('public-a','public-page',new AbortController().signal)
 a.changeParentDraftAuthority('locked');release({status:'ready',localApiUrl:'http://127.0.0.1:8765',sessionToken:'public-session'})
 await expect(pending).rejects.toThrow(/家长身份已变化/);expect(fetch).not.toHaveBeenCalled()
})
