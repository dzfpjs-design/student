import { createHash, createHmac } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { describe, expect, it, vi } from 'vitest'
import { NativeRealtimeCapabilityGateway, signNativeRealtimeCapability } from './native-realtime-capabilities'
import { nativeRealtimeCapabilityDomain, nativeRealtimeCapabilityProofFields } from '../shared/nativeRealtimeCapability'

const key = 'ab'.repeat(32)
const sender = { sender: { id: 7 } }
const body = { version: 1, voice_input_id: 'voice-public', supplier_session_id: 'supplier-public',
  input_item_id: 'input-public', response_id: 'response-public', call_id: 'call-public',
  capability_name: 'learning.prepare_request', args: { subject: '地理' } }
function fixture() {
  const process = {}
  let owner = { status: 'ready', localApiUrl: 'http://127.0.0.1:8765', sessionToken: 'synthetic-session-token', runtimeEpoch: 3, child: process, revision: 2 }
  let configured = true, now = Date.now()
  const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response(JSON.stringify({ version: 1,
    execution: { status: 'completed' }, public_result: { request_saved: true } })))
  const gateway = new NativeRealtimeCapabilityGateway({ allowed: event => event.sender.id === 7,
    current: () => owner, policy: async () => ({ capture: configured ? 'configured' : 'bounded_segmented', runtimeEpoch: owner.runtimeEpoch }),
    sign: proof => signNativeRealtimeCapability(key, proof), fetch, now: () => now })
  const id = gateway.register(sender)
  return { gateway, id, fetch, owner: (change: Partial<typeof owner>) => { owner = { ...owner, ...change } },
    revoke: () => { configured = false }, advance: (ms: number) => { now += ms } }
}
describe('Main-owned native capability requests', () => {
  it('extends only a validated Qwen 3.8 socket beyond six minutes and retains a bounded authority lifetime', () => {
    const f = fixture()
    f.gateway.confirmSocketModel(sender, f.id, 'session-public', 'qwen3.8-omni-flash-realtime')
    f.advance(360_001)
    expect(f.gateway.isCurrent(sender, f.id, 'session-public')).toBe(true)
    f.advance(115 * 60_000 - 360_001)
    expect(f.gateway.isCurrent(sender, f.id, 'session-public')).toBe(false)
  })
  it('keeps unknown models at the original six-minute bound', () => {
    const f = fixture()
    f.gateway.confirmSocketModel(sender, f.id, 'session-public', 'unrecognized-public-model')
    f.advance(360_001)
    expect(f.gateway.isCurrent(sender, f.id, 'session-public')).toBe(false)
  })
  it.each(['epoch', 'process', 'revision', 'close', 'replacement', 'navigation', 'session'] as const)('still revokes a Qwen 3.8 connection on %s', kind => {
    const f = fixture()
    f.gateway.confirmSocketModel(sender, f.id, 'session-public', 'qwen3.8-omni-flash-realtime')
    if (kind === 'epoch') f.owner({ runtimeEpoch: 4 })
    if (kind === 'process') f.owner({ child: {} })
    if (kind === 'revision') f.owner({ revision: 3 })
    if (kind === 'close') f.gateway.close(sender, f.id)
    if (kind === 'replacement') f.gateway.register(sender)
    if (kind === 'navigation') f.gateway.retireOwner(7)
    expect(f.gateway.isCurrent(sender, f.id, kind === 'session' ? 'session-other' : 'session-public')).toBe(false)
  })
  it('signs the exact forwarded bytes, keeping the connection ID out of model/HTTP arguments', async () => {
    const f = fixture()
    await f.gateway.forward(sender, { session_id: 'session-public', host_connection_id: f.id,
      operation: 'proposal', body: { ...body, host_connection_id: f.id } })
    const [url, init] = f.fetch.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8765/v1/sessions/session-public/voice/realtime/capability-proposals')
    const raw = init!.body as string
    expect(JSON.parse(raw)).toEqual(body)
    const headers = init!.headers as Record<string, string>
    const proof = JSON.parse(Buffer.from(headers['X-HSC-Native-Voice-Proof'], 'base64url').toString('utf8'))
    expect(proof.connection_id).toBe(f.id)
    expect(proof.body_sha256).toBe(createHash('sha256').update(raw).digest('hex'))
    const expected = createHmac('sha256', Buffer.from(key, 'hex'))
      .update(JSON.stringify([nativeRealtimeCapabilityDomain, ...nativeRealtimeCapabilityProofFields.map(k => proof[k])])).digest('hex')
    expect(proof.signature).toBe(expected)
    expect(headers['X-HSC-Session']).toBe('synthetic-session-token')
    const python = execFileSync('.venv/bin/python', ['-c', [
      'import json,sys',
      'from home_study_agent.realtime_capability_http import verify_native_realtime_request',
      'x=json.load(sys.stdin)',
      "p=verify_native_realtime_request('ab'*32,x['header'],'session-public','proposal',x['raw'].encode())",
      "print(json.dumps({'accepted':p['operation']=='proposal'}))",
    ].join(';')], { input: JSON.stringify({ header: headers['X-HSC-Native-Voice-Proof'], raw }),
      env: { ...process.env, PYTHONPATH: 'services/local_agent/src' }, encoding: 'utf8', timeout: 5000 })
    expect(JSON.parse(python)).toEqual({ accepted: true })
  })
  it('a wrong sender cannot use or retire the actual owner connection', async () => {
    const f = fixture()
    await expect(f.gateway.forward({ sender: { id: 9 } }, { session_id: 'session-public', host_connection_id: f.id,
      operation: 'proposal', body })).rejects.toThrow('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
    await expect(f.gateway.forward(sender, { session_id: 'session-public', host_connection_id: f.id,
      operation: 'proposal', body })).resolves.toMatchObject({ version: 1 })
    expect(f.fetch).toHaveBeenCalledTimes(1)
  })
  it.each(['epoch', 'process', 'revision', 'expiry', 'close', 'replacement', 'navigation'] as const)('rejects a retired %s owner before dispatch', async kind => {
    const f = fixture()
    if (kind === 'epoch') f.owner({ runtimeEpoch: 4 })
    if (kind === 'process') f.owner({ child: {} })
    if (kind === 'revision') f.owner({ revision: 3 })
    if (kind === 'expiry') f.advance(360_001)
    if (kind === 'close') f.gateway.close(sender, f.id)
    if (kind === 'replacement') f.gateway.register(sender)
    if (kind === 'navigation') f.gateway.retireOwner(7)
    await expect(f.gateway.forward(sender, { session_id: 'session-public', host_connection_id: f.id,
      operation: 'proposal', body })).rejects.toThrow('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
    expect(f.fetch).not.toHaveBeenCalled()
  })
  it('does not bind the same connection to a different session', async () => {
    const f = fixture()
    await f.gateway.forward(sender, { session_id: 'session-public', host_connection_id: f.id, operation: 'catalog' })
    await expect(f.gateway.forward(sender, { session_id: 'session-other', host_connection_id: f.id,
      operation: 'proposal', body })).rejects.toThrow('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
    expect(f.fetch).toHaveBeenCalledTimes(1)
  })
  it('withholds tool facts when consent changes after dispatch, while the original receipt remains readable', async () => {
    const f = fixture()
    f.fetch.mockImplementationOnce(async () => { f.revoke(); return new Response(JSON.stringify({
      version: 1, execution: { status: 'completed' }, public_result: { request_saved: true },
    })) })
    await expect(f.gateway.forward(sender, { session_id: 'session-public', host_connection_id: f.id,
      operation: 'proposal', body })).rejects.toThrow('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
    const result = await f.gateway.forward(sender, { session_id: 'session-public', host_connection_id: f.id,
      operation: 'receipt', body })
    expect(result).toMatchObject({ execution: { status: 'completed' }, public_result: { status: 'withheld' },
      public_projection: { eligible: false } })
    expect(result).not.toHaveProperty('public_result.request_saved')
  })
})

const clientIdentity={version:1,voice_input_id:'voice-public',supplier_session_id:'supplier-public',input_item_id:'input-public',response_id:'response-public',call_id:'call-public',dispatch_id:'dispatch-public'}
function claimResult(capability='profile.switch') {return {version:1,status:'claimed',client_dispatch:{version:1,dispatch_id:'dispatch-public',action_id:'action-public',status:'pending',turn:{agent_turn_id:'turn-public',runtime_capability:capability,action:{type:'switch_profile'}}}}}
async function claim(f:ReturnType<typeof fixture>,capability='profile.switch') {
 f.fetch.mockResolvedValueOnce(new Response(JSON.stringify(claimResult(capability))))
 return await f.gateway.forward(sender,{session_id:'session-public',host_connection_id:f.id,operation:'client',body:{...clientIdentity,phase:'claim'}}) as {completion_lease?:string}
}
describe('native client completion authority',()=>{
 it.each(['profile.switch','privacy.pause_all','mistake.open','reflection.complete'])('mints a bounded host-only completion lease for %s from the canonical claimed dispatch',async capability=>{
  const f=fixture();const r=await claim(f,capability);expect(r.completion_lease).toMatch(/^[A-Za-z0-9_-]{1,160}$/)
  const [url,init]=f.fetch.mock.calls[0];expect(url).toContain('/voice/realtime/native-client');expect(JSON.parse(String(init?.body))).toMatchObject({...clientIdentity,phase:'claim'})
 })
 it('ordinary operations receive no retirement lease',async()=>{const f=fixture();expect((await claim(f,'device.connect')).completion_lease).toBeUndefined()})
 it('records only the original completion after the source closes and a new host exists, with no cloud facts',async()=>{
  const f=fixture();const r=await claim(f);f.gateway.close(sender,f.id);f.gateway.register(sender);f.revoke()
  const out=await f.gateway.forward(sender,{session_id:'session-public',host_connection_id:f.id,operation:'client',completion_lease:r.completion_lease,body:{...clientIdentity,phase:'complete',final_response:'done',tool_status:'completed'}})
  expect(out).toMatchObject({execution:{status:'completed'},public_result:{status:'withheld'},public_projection:{eligible:false}})
  const sent=JSON.parse(String(f.fetch.mock.calls[1][1]?.body));expect(sent.completion_after_retirement).toBe(true);expect(sent.completion_lease).toBeUndefined()
  await expect(f.gateway.forward(sender,{session_id:'session-public',host_connection_id:f.id,operation:'client',completion_lease:r.completion_lease,body:{...clientIdentity,phase:'complete',tool_status:'completed'}})).rejects.toThrow('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
 })
 it.each(['execute','claim','proposal','other_voice','other_dispatch','other_session','other_host','sender','expiry','epoch','process','revision','navigation'] as const)('does not extend a completion lease into %s authority',async kind=>{
  const f=fixture();const r=await claim(f);f.gateway.close(sender,f.id)
  let session='session-public',host=f.id,event=sender,operation:'client'|'proposal'='client';const data:Record<string,unknown>={...clientIdentity,phase:'complete',tool_status:'completed'}
  if(kind==='execute'||kind==='claim')data.phase=kind
  if(kind==='proposal')operation='proposal'
  if(kind==='other_voice')data.voice_input_id='another-voice'
  if(kind==='other_dispatch')data.dispatch_id='another-dispatch'
  if(kind==='other_session')session='another-session'
  if(kind==='other_host')host='another-host'
  if(kind==='sender')event={sender:{id:9}}
  if(kind==='expiry')f.advance(180001)
  if(kind==='epoch')f.owner({runtimeEpoch:4})
  if(kind==='process')f.owner({child:{}})
  if(kind==='revision')f.owner({revision:3})
  if(kind==='navigation')f.gateway.retireOwner(7)
  await expect(f.gateway.forward(event,{session_id:session,host_connection_id:host,operation,completion_lease:r.completion_lease,body:data})).rejects.toThrow('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
  expect(f.fetch).toHaveBeenCalledTimes(1)
 })
 it('strips renderer-supplied retirement authority instead of signing it',async()=>{
  const f=fixture();await f.gateway.forward(sender,{session_id:'session-public',host_connection_id:f.id,operation:'client',body:{...clientIdentity,phase:'complete',completion_after_retirement:true}})
  expect(JSON.parse(String(f.fetch.mock.calls[0][1]?.body))).not.toHaveProperty('completion_after_retirement')
 })
})

it.each(['navigation','epoch','expiry'] as const)('rechecks completion authority after a deferred response and rejects changed %s',async kind=>{
 const f=fixture();const r=await claim(f);f.gateway.close(sender,f.id)
 let release!:(value:Response)=>void
 f.fetch.mockImplementationOnce(()=>new Promise<Response>(resolve=>{release=resolve}))
 const pending=f.gateway.forward(sender,{session_id:'session-public',host_connection_id:f.id,operation:'client',completion_lease:r.completion_lease,body:{...clientIdentity,phase:'complete',tool_status:'completed'}})
 expect(f.fetch).toHaveBeenCalledTimes(2)
 if(kind==='navigation')f.gateway.retireOwner(7)
 if(kind==='epoch')f.owner({runtimeEpoch:4})
 if(kind==='expiry')f.advance(180001)
 release(new Response(JSON.stringify({version:1,execution:{status:'completed'},public_result:{should_not_escape:true}})))
 await expect(pending).rejects.toThrow('NATIVE_REALTIME_REQUEST_UNCONFIRMED')
})
