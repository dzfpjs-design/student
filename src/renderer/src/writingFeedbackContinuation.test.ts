import {describe,expect,it,vi} from 'vitest'
import {continueWritingFeedback} from './writingFeedbackContinuation'
import type {AgentTurn,WritingFeedbackNotice} from './types'

function setup(local=false) {
  const turn={agent_turn_id:'feedback',child_id:'child',session_id:'session'} as AgentTurn
  const envelope={turn,execution:{capability:local?'conversation.respond':'learning.evaluate_response',
    response_text:'针对原稿的合成反馈。',result:{},evidence:[]}}
  return { notice:{status:local?'local_feedback_required':'feedback_required', feedback_turn_id:'cloud-feedback',
      ...(local?{local_feedback_turn_id:'feedback'}:{feedback_turn_id:'feedback'}),local_only:true,cloud_transmission_allowed:false} as WritingFeedbackNotice,
    sourceTurnId:'source',childId:'child',sessionId:'session',isCurrent:()=>true,
    execute:vi.fn(async (_id:string)=>envelope),beforeEvaluation:vi.fn(),finishEvaluation:vi.fn() }
}

describe('writing feedback follows a real result once',()=>{
  it('journals queued audio for read-only recovery without another execution',async()=>{
    const input=setup();input.notice.status='audio_feedback_pending'
    const recover=vi.fn()
    expect(await continueWritingFeedback({...input,recover})).toBeNull()
    expect(input.beforeEvaluation).toHaveBeenCalledExactlyOnceWith('feedback')
    expect(input.execute).not.toHaveBeenCalled()
    expect(recover).not.toHaveBeenCalled()
    expect(input.finishEvaluation).not.toHaveBeenCalled()
  })
  it('does not journal pending audio after a privacy interruption',async()=>{
    const input=setup();input.notice.status='audio_feedback_pending';input.isCurrent=()=>false
    await expect(continueWritingFeedback(input)).rejects.toThrow('暂停')
    expect(input.beforeEvaluation).not.toHaveBeenCalled()
  })
  it.each([false,true])('dispatches the exact %s path, not the local envelope to a model',async local=>{
    const input=setup(local)
    const result=await continueWritingFeedback(input)
    expect(input.execute).toHaveBeenCalledExactlyOnceWith('feedback')
    expect(result?.execution.response_text).toContain('原稿')
    expect(input.beforeEvaluation).toHaveBeenCalledTimes(local?0:1)
    expect(input.finishEvaluation).toHaveBeenCalledTimes(local?0:1)
  })
  it('never uses the cloud evaluation ID when local reference is unavailable',async()=>{
    const input=setup(true);delete input.notice.local_feedback_turn_id
    expect(await continueWritingFeedback(input)).toBeNull()
    expect(input.execute).not.toHaveBeenCalled()
  })
  it.each(['feedback_ready','blocked'])('does not repeat %s',async status=>{
    const input=setup();input.notice.status=status
    expect(await continueWritingFeedback(input)).toBeNull()
    expect(input.execute).not.toHaveBeenCalled()
  })
  it('persists recovery before dispatch and does not retry an uncertain request',async()=>{
    const input=setup()
    input.execute.mockRejectedValue(new Error('unknown result'))
    await expect(continueWritingFeedback(input)).rejects.toThrow('unknown result')
    expect(input.beforeEvaluation.mock.invocationCallOrder[0]).toBeLessThan(input.execute.mock.invocationCallOrder[0])
    expect(input.execute).toHaveBeenCalledTimes(1)
    expect(input.finishEvaluation).not.toHaveBeenCalled()
  })
  it('cannot dispatch after interruption',async()=>{
    const input=setup();input.isCurrent=()=>false
    await expect(continueWritingFeedback(input)).rejects.toThrow('暂停')
    expect(input.execute).not.toHaveBeenCalled()
  })
  it('recovers completed feedback by reading its durable result, not another model call',async()=>{
    const input=setup();input.notice.status='feedback_ready'
    const saved=await input.execute('fixture');input.execute.mockClear()
    const recover=vi.fn(async()=>saved)
    expect(await continueWritingFeedback({...input,recover})).toBe(saved)
    expect(recover).toHaveBeenCalledExactlyOnceWith('feedback')
    expect(input.execute).not.toHaveBeenCalled()
    expect(input.beforeEvaluation).not.toHaveBeenCalled()
  })
  it('does not present another child result',async()=>{
    const input=setup()
    const result=await input.execute('fixture-only');input.execute.mockClear()
    result.turn.child_id='other-child'
    await expect(continueWritingFeedback(input)).rejects.toThrow('不匹配')
    expect(input.finishEvaluation).not.toHaveBeenCalled()
  })
})
