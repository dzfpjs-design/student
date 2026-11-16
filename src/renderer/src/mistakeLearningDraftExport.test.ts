import{afterEach,beforeEach,expect,it,vi}from'vitest'
import{collectMistakeLearningDrafts}from'./mistakeLearningDraftExport'
import{localApi,resetRuntimeCache}from'./api'
beforeEach(()=>{localStorage.clear();resetRuntimeCache()})
afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals();delete window.desktop;resetRuntimeCache()})
it('preserves original raw draft, archived draft, pending and intake text for only the exact encoded child',()=>{
 const child='child:四',e=encodeURIComponent(child)
 const entries=[{key:`hsc-cloud-mistake-draft-v1:${e}:case`,value:' { "coach" : "公开原话" }\n'},{key:`hsc-cloud-mistake-draft-v1:${e}:case:source-old`,value:'{old archived text without parse}'},{key:`hsc-cloud-mistake-pending-v1:${e}:case:action`,value:'{"status":"unknown", "utterance":"公开未确认原话"}'},{key:`hsc-cloud-mistake-intake-v1:${e}`,value:'{"question":"公开新题"}'}]
 for(const entry of entries)localStorage.setItem(entry.key,entry.value)
 localStorage.setItem(`hsc-cloud-mistake-draft-v1:${e}0:case`,'other child');localStorage.setItem(`hsc-cloud-mistake-intake-v1:${e}0`,'other child');localStorage.setItem(`hsc-cloud-mistake-intake-v1:${e}:unexpected`,'not the exact intake key');localStorage.setItem('unrelated-private-key','not exported')
 expect(collectMistakeLearningDrafts(child)).toEqual([...entries].sort((a,b)=>a.key.localeCompare(b.key)))
 localStorage.clear();localStorage.setItem('hsc-cloud-mistake-draft-v1:child40:case','other');localStorage.setItem('hsc-cloud-mistake-draft-v1:child4:case','ours')
 expect(collectMistakeLearningDrafts('child4')).toEqual([{key:'hsc-cloud-mistake-draft-v1:child4:case',value:'ours'}])
})
it('fails rather than silently omitting a matching unreadable or disappearing record',()=>{
 localStorage.setItem('hsc-cloud-mistake-pending-v1:child4:case:call','raw')
 vi.spyOn(Storage.prototype,'getItem').mockImplementation(()=>{throw Error('public storage denial')})
 expect(()=>collectMistakeLearningDrafts('child4')).toThrow(/完整读取/)
 vi.mocked(Storage.prototype.getItem).mockReturnValue(null)
 expect(()=>collectMistakeLearningDrafts('child4')).toThrow(/完整读取/)
})
it('rejects more than 4096 matching entries or more than 8 MiB including UTF-8 JSON escaping',()=>{
 const storage={length:4097,key:(i:number)=>`hsc-cloud-mistake-pending-v1:child4:case:${i}`,getItem:()=>''}
 vi.stubGlobal('localStorage',storage)
 expect(()=>collectMistakeLearningDrafts('child4')).toThrow(/容量/)
 storage.length=1;storage.getItem=()=> 'a'.repeat(8*1024*1024)
 expect(()=>collectMistakeLearningDrafts('child4')).toThrow(/容量/)
})
it('uses the original parent/session authenticated export request and sends no request when collection fails',async()=>{
 const key='hsc-cloud-mistake-intake-v1:child4',value=' {"question":"公开导出原文"} '
 localStorage.setItem(key,value)
 window.desktop={getRuntimeConfig:async()=>({status:'ready',localApiUrl:'http://127.0.0.1:40499',sessionToken:'public-session-token'})}as never
 const fetcher=vi.fn().mockImplementation(async(url:string)=>new Response(JSON.stringify(url.endsWith('/unlock')?{parent_token:'public-parent-token'}:{path:'/public/export.json',size_bytes:1,sha256:'a'.repeat(64)}),{status:200}));vi.stubGlobal('fetch',fetcher)
 await localApi.parentUnlock('public-pin',false);await localApi.exportChild('child4')
 const [url,init]=fetcher.mock.calls[1] as [string,RequestInit]
 expect(url).toBe('http://127.0.0.1:40499/v1/parent/children/child4/export');expect(new Headers(init.headers).get('X-HSC-Parent')).toBe('public-parent-token');expect(new Headers(init.headers).get('X-HSC-Session')).toBe('public-session-token')
 expect(JSON.parse(init.body as string)).toEqual({mistake_learning_renderer_drafts:[{key,value}]})
 vi.spyOn(Storage.prototype,'getItem').mockImplementation(()=>{throw Error('public storage denial')})
 expect(()=>localApi.exportChild('child4')).toThrow(/完整读取/);expect(fetcher).toHaveBeenCalledTimes(2)
})
