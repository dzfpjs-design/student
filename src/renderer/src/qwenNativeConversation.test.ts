// @vitest-environment node
import { expect, it, vi } from 'vitest'
import { QwenNativeConversation, qwenNativePublicToolOutput } from './qwenNativeConversation'
import { parseQwenNativeProposal, qwenNativeInstructions, qwenRuntimeTools,
  type QwenNativeBoundRuntime, type QwenNativeToolReceipt } from './qwenNativeProtocol'

const catalog = { version: 1 as const, protocol: 'qwen_runtime_capability_v1' as const, capabilities: [
  { name: 'learning.prepare_request', tool_name: 'learning_prepare_request', description: 'Save a new learning request.',
    required_arguments: ['subject'], arguments_schema: { type: 'object', additionalProperties: false,
      required: ['subject'], properties: { subject: { type: 'string' } } } },
] }
const capture = { sessionId: 'synthetic-local-session', reference: { child_id: 'synthetic-child' },
  carryoverConfirmationId: null, localInputId: 'synthetic-local-input', capturedAt: 1, source: 'accepted_onset' as const }
const completed = (action = 'real-action'): QwenNativeToolReceipt => ({ version: 1, proposal_status: 'admitted', action_id: action,
  agent_turn_id: 'server-turn', execution: { status: 'completed', location: 'server' },
  public_result: { status: 'saved', needs_textbook: true } })
const args = JSON.stringify({ args: { subject: 'geography' } })
function fixture() {
  const runtime: QwenNativeBoundRuntime = { catalog,
    loadCatalog:vi.fn(async()=>catalog),loadContext:vi.fn(async()=>({version:1 as const,revision:0,recent_dialogue:[],runtime_facts:{},pending_calls:[]})),
    admit: vi.fn(async () => ({ voice_input_id: 'canonical-voice' })),
    propose: vi.fn(async () => completed()), recordDialogue: vi.fn(async () => undefined),
    readReceipt: vi.fn(async () => completed()), }
  let owned = true
  const ledger = new QwenNativeConversation('supplier-session', runtime, c => owned && c.reference.child_id === 'synthetic-child','host-connection')
  expect(ledger.startInput('input-item', capture)).toBe(true)
  return { ledger, runtime, retire: () => { owned = false } }
}
async function ready() {
  const f = fixture()
  await f.ledger.completeInput('input-item', '帮我复习初三地理第一课')
  expect(f.ledger.bindResponse('native-response', 'input-item')).toBe(true)
  return f
}

it('advertises concrete catalog tools without a generic utterance classifier or model supplied identity', () => {
  const tools = qwenRuntimeTools(catalog)
  expect(tools[0].function.name).toBe('learning_prepare_request')
  expect(JSON.stringify(tools)).not.toContain('handle_child_turn')
  expect(JSON.stringify(tools)).not.toContain('voice_input_id')
  expect(parseQwenNativeProposal(catalog, 'learning_prepare_request', args)).toEqual({ capability_name: 'learning.prepare_request', args: { subject: 'geography' } })
  expect(parseQwenNativeProposal(catalog, 'learning_prepare_request', JSON.stringify({ args: {}, voice_input_id: 'model-guessed' }))).toBeNull()
  expect(parseQwenNativeProposal(catalog, 'unknown_tool', args)).toBeNull()
  expect(qwenNativeInstructions()).toContain('不要求固定口令或固定问答')
})
it('ASR completion only saves the original; ordinary native dialogue is explicitly unverified without a semantic tool', async () => {
  const { ledger, runtime } = await ready()
  expect(runtime.admit).toHaveBeenCalledExactlyOnceWith('帮我复习初三地理第一课', capture,'host-connection')
  expect(runtime.propose).not.toHaveBeenCalled()
  expect(ledger.appendDialogue('native-response', '你用哪一版地理课本？')).toBe(true)
  expect(await ledger.finishDialogue('native-response', 'completed')).toBe(true)
  expect(runtime.recordDialogue).toHaveBeenCalledExactlyOnceWith({ version: 1,host_connection_id:'host-connection',
    supplier_session_id: 'supplier-session', input_item_id: 'input-item', response_id: 'native-response',
    voice_input_id: 'canonical-voice', text: '你用哪一版地理课本？', status: 'unverified', terminal: 'completed' })
  expect(runtime.propose).not.toHaveBeenCalled()
})
it('unknown or ambiguous response cannot borrow the latest transcript or an unrelated input', async () => {
  const { ledger, runtime } = await ready()
  expect(ledger.bindResponse('late-old-response', 'unknown-item')).toBe(false)
  expect((await ledger.propose('late-old-response', 'call', 'learning_prepare_request', args)).public_result).toEqual({ status: 'source_unconfirmed' })
  expect(runtime.propose).not.toHaveBeenCalled()
})
it('same input owns all sequential calls and each real completed action binds the next substep', async () => {
  const { ledger, runtime } = await ready()
  vi.mocked(runtime.propose).mockResolvedValueOnce(completed('first-action')).mockResolvedValueOnce(completed('second-action'))
  await Promise.all([ledger.propose('native-response', 'call-1', 'learning_prepare_request', args),
    ledger.propose('native-response', 'call-2', 'learning_prepare_request', args)])
  expect(runtime.propose).toHaveBeenCalledTimes(2)
  expect(vi.mocked(runtime.propose).mock.calls[0][0]).toMatchObject({ voice_input_id: 'canonical-voice', call_id: 'call-1', input_item_id: 'input-item' })
  expect(vi.mocked(runtime.propose).mock.calls[1][0]).toMatchObject({ voice_input_id: 'canonical-voice', call_id: 'call-2', previous_action_id: 'first-action' })
})
it('uncertain call is queried by its original identity rather than reexecuted; other calls wait', async () => {
  const { ledger, runtime } = await ready()
  vi.mocked(runtime.propose).mockRejectedValueOnce(Error('synthetic timeout'))
  const unknown = await ledger.propose('native-response', 'call-1', 'learning_prepare_request', args)
  expect(unknown.execution.status).toBe('unknown')
  expect((await ledger.propose('native-response', 'call-2', 'learning_prepare_request', args)).public_result).toEqual({ status: 'previous_execution_unconfirmed' })
  await ledger.propose('native-response', 'call-1', 'learning_prepare_request', args)
  const cached = await ledger.propose('native-response', 'call-1', 'learning_prepare_request', args)
  expect(cached.execution.status).toBe('completed')
  expect(runtime.propose).toHaveBeenCalledTimes(1)
  expect(runtime.readReceipt).toHaveBeenCalledExactlyOnceWith({ version: 1,host_connection_id:'host-connection', supplier_session_id: 'supplier-session',
    input_item_id: 'input-item', response_id: 'native-response', voice_input_id: 'canonical-voice', call_id: 'call-1' })
})
it('a tool issued before provider generation done survives slow admission without borrowing a later source', async () => {
  const { ledger, runtime } = fixture()
  let admit!: (value: { voice_input_id: string }) => void
  vi.mocked(runtime.admit).mockImplementation(() => new Promise(resolve => { admit = resolve }))
  const admission = ledger.completeInput('input-item', '帮我复习初三地理第一课')!
  expect(ledger.bindResponse('native-response', 'input-item')).toBe(true)
  const tool = ledger.propose('native-response', 'call', 'learning_prepare_request', args)
  await Promise.resolve()
  expect(await ledger.finishDialogue('native-response', 'completed')).toBe(true)
  admit({ voice_input_id: 'canonical-voice' })
  await admission
  expect((await tool).execution.status).toBe('completed')
  expect(runtime.propose).toHaveBeenCalledTimes(1)
  expect((await ledger.propose('native-response', 'late-new-call', 'learning_prepare_request', args)).public_result).toEqual({ status: 'source_unconfirmed' })
})
it('interruption before a slow admission retires its tool while preserving original admission', async () => {
  const { ledger, runtime } = fixture()
  let admit!: (value: { voice_input_id: string }) => void
  vi.mocked(runtime.admit).mockImplementation(() => new Promise(resolve => { admit = resolve }))
  const admission = ledger.completeInput('input-item', '帮我复习初三地理第一课')!
  ledger.bindResponse('native-response', 'input-item')
  const tool = ledger.propose('native-response', 'call', 'learning_prepare_request', args)
  await Promise.resolve()
  ledger.retireResponse('native-response')
  admit({ voice_input_id: 'canonical-voice' }); await admission
  expect((await tool).public_result).toEqual({ status: 'source_unconfirmed' })
  expect(runtime.propose).not.toHaveBeenCalled()
})
it('reused call with altered arguments cannot execute again and private result/host identity never enters public output', async () => {
  const { ledger, runtime } = await ready()
  const result = await ledger.propose('native-response', 'call-1', 'learning_prepare_request', args)
  expect((await ledger.propose('native-response', 'call-1', 'learning_prepare_request', JSON.stringify({ args: { subject: 'math' } }))).public_result).toEqual({ status: 'call_identity_conflict' })
  expect(runtime.propose).toHaveBeenCalledTimes(1)
  const output = qwenNativePublicToolOutput({ ...result, local_result: { answer: 'private synthetic answer' } } as QwenNativeToolReceipt)
  expect(output).not.toContain('private synthetic answer')
  expect(output).not.toContain('real-action')
  expect(output).not.toContain('server-turn')
  expect(JSON.parse(output).public_result).toEqual(result.public_result)
})
it('changed owner and disposed session cannot admit a new tool or native dialogue', async () => {
  const f = await ready(); f.retire()
  expect((await f.ledger.propose('native-response', 'call', 'learning_prepare_request', args)).execution.status).toBe('failed')
  expect(f.ledger.appendDialogue('native-response', 'late stale text')).toBe(false)
  expect(f.runtime.propose).not.toHaveBeenCalled()
  const g = await ready(); g.ledger.close()
  expect((await g.ledger.propose('native-response', 'call', 'learning_prepare_request', args)).execution.status).toBe('failed')
  expect(g.runtime.propose).not.toHaveBeenCalled()
})
it('an explicit not-admitted rejection permits a corrected new call without locking the input as unknown', async () => {
  const f = await ready()
  const rejected = { version: 1, proposal_status: 'rejected', not_admitted: true,
    execution: { status: 'failed', location: 'server' }, public_result: { status: 'not_executed', reason_code: 'ARGUMENTS_INVALID' } } as const
  vi.mocked(f.runtime.propose).mockResolvedValueOnce(rejected).mockResolvedValueOnce(completed('corrected-action'))
  expect((await f.ledger.propose('native-response', 'bad-call', 'learning_prepare_request', args)).not_admitted).toBe(true)
  expect((await f.ledger.propose('native-response', 'corrected-call', 'learning_prepare_request', args)).execution.status).toBe('completed')
  expect(f.runtime.propose).toHaveBeenCalledTimes(2); expect(f.runtime.readReceipt).not.toHaveBeenCalled()
  expect(vi.mocked(f.runtime.propose).mock.calls[1][0].previous_action_id).toBeUndefined()
})
it('a persisted native original binds its canonical voice without a fabricated live onset, new timestamp or another admission', async () => {
  const f = fixture()
  const original = Object.freeze({ voice_input_id: 'persisted-original-voice', client_action_id: 'persisted-original-input',
    created_at: '2026-10-28T10:00:00Z', text: '帮我复习地理第一课' })
  expect(f.ledger.restoreInput('restored-provider-item', original, () => true)).toBe(true)
  expect(f.ledger.completeInput('restored-provider-item', original.text)).toBeNull()
  expect(f.ledger.bindResponse('restored-response', 'restored-provider-item')).toBe(true)
  await f.ledger.propose('restored-response', 'restored-call', 'learning_prepare_request', args)
  expect(f.runtime.admit).not.toHaveBeenCalled()
  expect(vi.mocked(f.runtime.propose).mock.calls[0][0]).toMatchObject({ voice_input_id: original.voice_input_id,
    input_item_id: 'restored-provider-item', response_id: 'restored-response', host_connection_id: 'host-connection',input_source:'persisted_native' })
  expect(original.created_at).toBe('2026-10-28T10:00:00Z')
  expect(f.ledger.restoreInput('another-provider-item', original, () => true)).toBe(false)
})
