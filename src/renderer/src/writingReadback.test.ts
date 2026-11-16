import { expect, it, vi } from 'vitest'
import { playWritingReadback, type WritingConfirmation } from './writingReadback'

const packet = (): WritingConfirmation => ({ confirmation_id:'confirm-1', binding_version:'a'.repeat(64),
  child_id:'child-1', session_id:'session-1', prompt_id:'prompt-1', item_version:'v1', artifact_id:'page-1', revision_id:'rev-1',
  response_text:'My answer', readback_text:'Please check this original answer.', readback_sha256:'b'.repeat(64),
  expires_at:new Date(Date.now()+60_000).toISOString(),local_only:true,cloud_transmission_allowed:false })
const setup = () => ({ childId:'child-1',sessionId:'session-1',confirmationId:'confirm-1',bindingVersion:'a'.repeat(64),
  isCurrent:()=>true,read:vi.fn().mockResolvedValue(packet()),record:vi.fn().mockResolvedValue({}),
  play:vi.fn((_text: string, callbacks: {started:()=>void;ended:()=>void;unavailable:()=>void}) => { callbacks.started(); callbacks.ended() }) })

it('waits for actual start persistence before writing the ended receipt', async () => {
  const input = setup()
  let finishStart!: () => void
  input.record.mockReturnValueOnce(new Promise<void>(resolve => { finishStart=resolve }))
  const pending = playWritingReadback(input)
  await vi.waitFor(() => expect(input.record).toHaveBeenCalledOnce())
  expect(input.record.mock.calls[0][0].event).toBe('started')
  finishStart()
  const receipt = await pending
  expect(receipt.status).toBe('readback_completed')
  expect(input.record.mock.calls.map(call=>call[0].event)).toEqual(['started','ended'])
  expect(input.record.mock.calls[0][0].playback_id).toBe(input.record.mock.calls[1][0].playback_id)
})

it.each(['foreign','expired','cloud','stale'] as const)('rejects %s source without playing or recording', async fault => {
  const input=setup(), value=packet()
  if (fault==='foreign') value.child_id='other-child'
  if (fault==='expired') value.expires_at='2000-01-01T00:00:00Z'
  if (fault==='cloud') Object.assign(value,{cloud_transmission_allowed:true})
  if (fault==='stale') input.isCurrent=()=>false
  input.read.mockResolvedValue(value)
  await expect(playWritingReadback(input)).rejects.toThrow()
  expect(input.play).not.toHaveBeenCalled()
  expect(input.record).not.toHaveBeenCalled()
})

it.each(['not_played','missing_start','failed_start','cancelled'] as const)('does not acknowledge incomplete %s playback', async fault => {
  const input=setup()
  if (fault==='failed_start') input.record.mockRejectedValueOnce(new Error('offline'))
  input.play.mockImplementation((_text,callbacks)=> {
    if (fault==='not_played') callbacks.unavailable()
    else if (fault==='missing_start') callbacks.ended()
    else {
      callbacks.started()
      if (fault==='cancelled') input.isCurrent=()=>false
      callbacks.ended()
    }
  })
  await expect(playWritingReadback(input)).rejects.toThrow('尚未提交')
  expect(input.record.mock.calls.some(call=>call[0].event==='ended')).toBe(false)
})
