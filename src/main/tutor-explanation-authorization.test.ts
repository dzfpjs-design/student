import { expect, it, vi } from 'vitest'
import { createTutorExplanationAuthorizer } from './tutor-explanation-authorization'
const base={status:'ready',localApiUrl:'http://127.0.0.1:12345',sessionToken:'synthetic-local',runtimeEpoch:4}
it('validates parent token against exact local backend, no redirects and current frame',async()=>{
 const fetchImpl=vi.fn().mockResolvedValue(Response.json({authorized:true}))
 const authorize=createTutorExplanationAuthorizer({senderId:7,current:()=>true,runtime:()=>base,externalBackend:false,fetchImpl})
 await authorize(7,'synthetic-parent')
 expect(fetchImpl).toHaveBeenCalledWith('http://127.0.0.1:12345/v1/parent/tutor-explanation/authorization',expect.objectContaining({redirect:'error',headers:{'X-HSC-Session':'synthetic-local','X-HSC-Parent':'synthetic-parent'}}))
})
it('rejects a different sender, nonlocal backend and declined parent before accepting settings',async()=>{
 const fetchImpl=vi.fn().mockResolvedValue(new Response('',{status:401}))
 const authorize=createTutorExplanationAuthorizer({senderId:7,current:()=>true,runtime:()=>base,externalBackend:false,fetchImpl})
 await expect(authorize(8,'parent')).rejects.toThrow();expect(fetchImpl).not.toHaveBeenCalled()
 await expect(authorize(7,'parent')).rejects.toThrow('TUTOR_EXPLANATION_PARENT_REQUIRED')
 const offsite=createTutorExplanationAuthorizer({senderId:7,current:()=>true,runtime:()=>({...base,localApiUrl:'http://remote.example.test:1234'}),externalBackend:false,fetchImpl})
 await expect(offsite(7,'parent')).rejects.toThrow('TUTOR_EXPLANATION_RUNTIME_UNAVAILABLE')
})
it('rejects success from a stale service epoch or changed owner frame',async()=>{
 let state={...base},current=true
 const fetchImpl=vi.fn(async()=>{state={...state,runtimeEpoch:5};return Response.json({authorized:true})})
 const authorize=createTutorExplanationAuthorizer({senderId:7,current:()=>current,runtime:()=>state,externalBackend:false,fetchImpl})
 await expect(authorize(7,'parent')).rejects.toThrow('TUTOR_EXPLANATION_PARENT_REQUIRED')
 state={...base};fetchImpl.mockImplementationOnce(async()=>{current=false;return Response.json({authorized:true})})
 await expect(authorize(7,'parent')).rejects.toThrow('TUTOR_EXPLANATION_PARENT_REQUIRED')
})
