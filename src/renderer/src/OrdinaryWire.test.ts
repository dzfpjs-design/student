import {afterEach,expect,it,vi} from 'vitest'
import {localApi,resetRuntimeCache} from './api'
afterEach(()=>{vi.unstubAllGlobals();resetRuntimeCache()})
it('real transcribe transport preserves complete refs and strict local marker',async()=>{
 vi.stubGlobal('desktop',{getRuntimeConfig:vi.fn(async()=>({status:'ready',localApiUrl:'http://127.0.0.1:8080',sessionToken:'synthetic-test'}))})
 const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({status:'completed'}),{status:200}));vi.stubGlobal('fetch',fetch)
 const ref={child_id:'a',question_reference:{kind:'document',binding:{revision_id:'r',artifact_id:'a',content_sha256:'c',page_sha256:'p'}},activity_reference:{activity_id:'a',version:'v',last_turn_id:'t'}}as const
 const marker={version:'ordinary_prompt_response_v1',session_state:'study',source_agent_turn_id:'actual-turn'}as const
 await localApi.transcribe('s','agent_command','public-synthetic-wav',new AbortController().signal,'key',ref,true,marker)
 const body=JSON.parse(fetch.mock.calls[0][1].body);expect(body).toEqual({wav_base64:'public-synthetic-wav',purpose:'agent_command',client_action_id:'key',capture_reference:ref,local_only:true,ordinary_response_binding:marker})
})
it('no-board transport routes all four operations with actual frozen tuple',async()=>{
 vi.stubGlobal('desktop',{getRuntimeConfig:vi.fn(async()=>({status:'ready',localApiUrl:'http://127.0.0.1:8080',sessionToken:'synthetic-test'}))})
 const fetch=vi.fn().mockImplementation(async()=>new Response('{}',{status:200}));vi.stubGlobal('fetch',fetch)
 for(const op of ['reserve','start','natural-end','cancel']as const)await localApi.tutorVoicePlayback('s',op,{playback_id:'p',lease_id:'lease',lease_generation:4,lease_kind:'managed_audio',client_action_id:'key'},new AbortController().signal)
 expect(fetch.mock.calls.map(call=>new URL(call[0]).pathname)).toEqual(['reserve','start','natural-end','cancel'].map(op=>'/v1/sessions/s/tutor-voice-playback/'+op))
 for(const call of fetch.mock.calls)expect(JSON.parse(call[1].body)).toMatchObject({lease_id:'lease',lease_generation:4,lease_kind:'managed_audio'})
})
