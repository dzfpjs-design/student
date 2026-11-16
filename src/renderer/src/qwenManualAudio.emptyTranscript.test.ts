// @vitest-environment node
import {afterEach,expect,it,vi} from 'vitest'
import {QwenManualAudio} from './qwenManualAudio'
import {classifyQwenRealtimeError} from './QwenRealtimeVoiceInput'
import {mistakeWorkspaceVoiceCommand} from './mistakeWorkspaceVoice'
import type {QwenRealtimeInputCapture} from './qwenRealtimeVoice'
import type {VoiceQuestionReference} from './voiceCaptureContext'
const pending:any[]=[]
function fixture(onWorkspaceInput?: (text:string, capture:unknown, voiceInputId:string)=>boolean,
  diagnostic?: { question: VoiceQuestionReference; currentAnswerTool: (capture: QwenRealtimeInputCapture) => string | undefined }){
 vi.useFakeTimers()
 let current=true
 let appendAllowed=true
 let respondAllowed=true
 let replyAudioScheduled=false
 const sent:any[]=[]
 const status=vi.fn(),failed=vi.fn(),inputCompleted=vi.fn(),responseBound=vi.fn(),interruptReply=vi.fn()
 const ledger={startInput:vi.fn(()=>true),completeInput:vi.fn(async()=>({voice_input_id:'public-canonical'})),bindResponse:vi.fn(()=>true),retireResponse:vi.fn()}
 const manual=new QwenManualAudio({canAppend:()=>appendAllowed,canRespond:()=>respondAllowed,current:()=>current,
  capture:()=>({sessionId:'synthetic-session',reference:{child_id:'synthetic-child',...(diagnostic ? {question_reference:diagnostic.question} : {})},carryoverConfirmationId:null,source:'accepted_onset',capturedAt:Date.now()} as any),
  ledger:()=>ledger as any,send:e=>sent.push(e),speechStarted:vi.fn(),replyAudioScheduled:()=>replyAudioScheduled,interruptReply,
  inputCompleted,onWorkspaceInput,currentDiagnosticAnswerTool:diagnostic?.currentAnswerTool,responseBound,failed,status})
 const frames=(level:number, count:number, mode:'auto'|'explicit'='auto')=>{
  for(let i=0;i<count;i++)manual.processFrame(new Float32Array(1024).fill(level),16000,mode)
 }
 const speech=()=>{frames(.04,6);frames(0,13)}
 pending.push(manual)
 return{manual,sent,status,failed,inputCompleted,responseBound,interruptReply,ledger,speech,frames,
  scheduleAudio:()=>{replyAudioScheduled=true},current:(v:boolean)=>{current=v},
  setRespondAllowed:(allowed:boolean)=>{respondAllowed=allowed;manual.gateChanged()},
  setAppendAllowed:(allowed:boolean)=>{appendAllowed=allowed;manual.gateChanged()}}
}
afterEach(()=>{for(const p of pending.splice(0))p.dispose();vi.useRealTimers();vi.restoreAllMocks()})

it('asks the native model to use the diagnostic tool for a natural answer after canonical audio admission', async () => {
  const binding = { prompt_id: 'private-prompt-id', item_version: 'private-item-version' }
  const currentAnswerTool = vi.fn(() => 'runtime_diagnostic_submit')
  const f = fixture(undefined, { question: { kind: 'diagnostic', binding }, currentAnswerTool })
  f.speech()
  expect(f.manual.inputCommitted('school-input')).toBe(true)
  expect(f.sent.some(event => event.type === 'response.create')).toBe(false)
  expect(await f.manual.inputCompleted('school-input', '我认为要用控制变量法')).toBe(true)
  const request = f.sent.find(event => event.type === 'response.create')
  expect(currentAnswerTool).toHaveBeenCalledWith(expect.objectContaining({ reference: expect.objectContaining({
    question_reference: { kind: 'diagnostic', binding },
  }) }))
  expect(request.response.instructions).toContain('runtime_diagnostic_submit')
  expect(request.response.instructions).toContain('不提交评分')
  expect(JSON.stringify(request)).not.toMatch(/private-prompt-id|private-item-version|控制变量法/)
})

it('omits the pending-diagnostic hint if the question changes before the response is requested', async () => {
  const currentAnswerTool = vi.fn<() => string | undefined>(() => 'runtime_diagnostic_submit')
  const f = fixture(undefined, { question: { kind: 'diagnostic', binding: { prompt_id: 'old-prompt', item_version: 'old-version' } }, currentAnswerTool })
  f.setRespondAllowed(false)
  f.speech()
  expect(f.manual.inputCommitted('school-input')).toBe(true)
  await f.manual.inputCompleted('school-input', '我认为要用控制变量法')
  expect(currentAnswerTool).not.toHaveBeenCalled()
  currentAnswerTool.mockReturnValue(undefined)
  f.setRespondAllowed(true)
  expect(f.sent.find(event => event.type === 'response.create')).toEqual({ type: 'response.create' })
})

for(const text of ['', ' \n\t', '.', '。？！……', '\u200b…\ufeff'])it(`explicit no-words completion ${JSON.stringify(text)} retires only its commit instead of spending a reconnect`,async()=>{
 const f=fixture();f.speech();expect(f.manual.inputCommitted('empty-item')).toBe(true)
 expect(await f.manual.inputCompleted('empty-item',text)).toBe(false)
 expect(f.ledger.completeInput).not.toHaveBeenCalled();expect(f.inputCompleted).not.toHaveBeenCalled()
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(0)
 expect(f.manual.pendingCount()).toBe(0)
 await vi.advanceTimersByTimeAsync(30001)
 expect(f.failed).not.toHaveBeenCalled()
})
for(const text of ['三','是','对','小知','3','−','✓','-','/','*','%',':','**'])it(`retains legitimate short source ${text}`,async()=>{
 const f=fixture();f.speech();expect(f.manual.inputCommitted('valid-item')).toBe(true)
 expect(await f.manual.inputCompleted('valid-item',text)).toBe(true)
 expect(f.ledger.completeInput).toHaveBeenCalledExactlyOnceWith('valid-item',text)
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
 expect(f.manual.responseCreated('valid-response')).toBe(true)
 await vi.advanceTimersByTimeAsync(30001);expect(f.failed).not.toHaveBeenCalled()
})
it('does not relabel a genuinely missing completion as silence',async()=>{
 const f=fixture();f.speech();f.manual.inputCommitted('missing-item')
 await vi.advanceTimersByTimeAsync(30001)
 expect(f.failed).toHaveBeenCalledExactlyOnceWith('刚才的语音没有取得完整转写和保存回执，正在恢复连接；未确认的原话请重说。')
})
it('hands a saved voice capture command to the workspace and lets ordinary speech continue to the provider',async()=>{
 const workspace=vi.fn((text:string)=>mistakeWorkspaceVoiceCommand(text,'小知')==='capture')
 const f=fixture(workspace)
 f.speech();expect(f.manual.inputCommitted('capture-item')).toBe(true)
 await f.manual.inputCompleted('capture-item','小知，帮我录入这道错题。')
 expect(f.ledger.completeInput).toHaveBeenCalledExactlyOnceWith('capture-item','小知，帮我录入这道错题。')
 expect(workspace).toHaveBeenCalledWith('小知，帮我录入这道错题。',expect.objectContaining({source:'accepted_onset'}),'public-canonical')
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(0)
 f.speech();expect(f.manual.inputCommitted('ordinary-item')).toBe(true)
 await f.manual.inputCompleted('ordinary-item','我想复习地理第一课。')
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
})
it('a wrong provider item cannot clear the outstanding source or deadline',async()=>{
 const f=fixture();f.speech();f.manual.inputCommitted('current-item')
 expect(await f.manual.inputCompleted('wrong-item','')).toBe(false)
 expect(f.manual.pendingCount()).toBe(1)
 await vi.advanceTimersByTimeAsync(30001);expect(f.failed).toHaveBeenCalledTimes(1)
})
it('does not erase a newer queued onset when discarding an older empty completion',async()=>{
 const f=fixture();f.speech();f.manual.inputCommitted('first-item');f.speech()
 expect(f.manual.pendingCount()).toBe(2)
 await f.manual.inputCompleted('first-item','。')
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.commit')).toHaveLength(2)
 expect(f.manual.inputCommitted('second-item')).toBe(true)
 expect(await f.manual.inputCompleted('second-item','三')).toBe(true)
 expect(f.ledger.completeInput).toHaveBeenCalledExactlyOnceWith('second-item','三')
 expect(f.manual.responseCreated('second-response')).toBe(true)
 await vi.advanceTimersByTimeAsync(30001);expect(f.failed).not.toHaveBeenCalled()
})
for (const onset of ['while saving the first input', 'before response creation', 'after response creation']) {
 it(`preserves an unheard reply when new noise arrives ${onset}`,async()=>{
  const f=fixture()
  f.speech()
  expect(f.manual.inputCommitted('first-item')).toBe(true)
  const first=f.manual.inputCompleted('first-item','把化学这部分展开讲讲')
  if (onset==='while saving the first input') f.speech()
  expect(await first).toBe(true)
  expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
  f.interruptReply.mockClear()
  if (onset==='before response creation') f.speech()
  expect(f.manual.responseCreated('first-reply')).toBe(true)
  if (onset==='after response creation') f.speech()
  expect(f.manual.inputCommitted('noise-item')).toBe(true)
  expect(await f.manual.inputCompleted('noise-item','。')).toBe(false)
  expect(f.responseBound).toHaveBeenCalledExactlyOnceWith('first-reply',true)
  expect(f.interruptReply).not.toHaveBeenCalled()
  expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(1)
 })
}
it('replaces an unheard reply only after the new utterance is admitted',async()=>{
 const f=fixture()
 f.speech();f.manual.inputCommitted('first-item')
 await f.manual.inputCompleted('first-item','我想学初三化学')
 f.interruptReply.mockClear()
 f.speech()
 expect(f.manual.responseCreated('first-reply')).toBe(true)
 expect(f.interruptReply).not.toHaveBeenCalled()
 expect(f.manual.inputCommitted('second-item')).toBe(true)
 expect(await f.manual.inputCompleted('second-item','可以深入一点')).toBe(true)
 expect(f.interruptReply).toHaveBeenCalledExactlyOnceWith('confirmed_input')
 expect(f.sent.filter(e=>e.type==='response.create')).toHaveLength(2)
 expect(f.manual.responseCreated('second-reply')).toBe(true)
})
it('interrupts immediately when response PCM has already been scheduled',async()=>{
 const f=fixture()
 f.speech();f.manual.inputCommitted('first-item')
 await f.manual.inputCompleted('first-item','开始讲')
 expect(f.manual.responseCreated('first-reply')).toBe(true)
 f.interruptReply.mockClear();f.scheduleAudio();f.speech()
 expect(f.interruptReply).toHaveBeenCalledExactlyOnceWith('speech_onset')
 f.manual.inputCommitted('second-item')
 expect(await f.manual.inputCompleted('second-item','停一下')).toBe(true)
 expect(f.interruptReply).toHaveBeenCalledTimes(1)
})
it('keeps a long explicit pause and commits both spoken parts exactly once on release',async()=>{
 const f=fixture()
 expect(f.manual.beginExplicitTurn()).toBe(true)
 f.frames(.04,6,'explicit')
 f.frames(0,20,'explicit')
 f.frames(.09,6,'explicit')
 expect(f.sent.some(e=>e.type==='input_audio_buffer.append')).toBe(true)
 expect(f.sent.some(e=>e.type==='input_audio_buffer.commit'||e.type==='response.create')).toBe(false)
 f.manual.finishExplicitTurn()
 f.manual.finishExplicitTurn()
 const commits=f.sent.filter(e=>e.type==='input_audio_buffer.commit')
 const packetStarts=f.sent.filter(e=>e.type==='input_audio_buffer.append')
  .map(e=>Buffer.from(e.audio,'base64').readInt16LE(0))
 expect(commits).toHaveLength(1)
 const pause=packetStarts.indexOf(0)
 expect(pause).toBeGreaterThan(0)
 expect(packetStarts[0]).toBe(Math.round(.04*32767))
 expect(packetStarts.slice(pause)).toContain(Math.round(.09*32767))
 expect(f.manual.inputCommitted('explicit-item')).toBe(true)
 expect(await f.manual.inputCompleted('explicit-item','继续讲这道题')).toBe(true)
 expect(f.ledger.completeInput).toHaveBeenCalledExactlyOnceWith('explicit-item','继续讲这道题')
 expect(f.manual.responseCreated('explicit-reply')).toBe(true)
})
it('an explicit long answer passes the automatic thirty-second limit without committing',()=>{
 const f=fixture()
 const packet=new Float32Array(8192).fill(.04)
 expect(f.manual.beginExplicitTurn()).toBe(true)
 for(let i=0;i<62;i++) f.manual.processFrame(packet,16000,'explicit')
 expect(f.sent.some(e=>e.type==='input_audio_buffer.commit')).toBe(false)
 f.manual.finishExplicitTurn()
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.commit')).toHaveLength(1)
})
it('does not upload a silent press or accept automatic frames during an explicit turn',()=>{
 const f=fixture()
 expect(f.manual.beginExplicitTurn()).toBe(true)
 f.frames(0,20,'explicit')
 f.speech()
 f.manual.finishExplicitTurn()
 expect(f.sent).toHaveLength(0)
 expect(f.manual.pendingCount()).toBe(0)
})
it('discards unsubmitted explicit audio after cancel, lost ownership or late release',()=>{
 const f=fixture()
 expect(f.manual.beginExplicitTurn()).toBe(true)
 f.frames(.04,6,'explicit')
 f.manual.cancelExplicitTurn()
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.clear')).toHaveLength(1)
 const canceled=f.sent.length
 f.frames(.04,6,'explicit')
 f.manual.finishExplicitTurn()
 expect(f.sent).toHaveLength(canceled)
 expect(f.manual.beginExplicitTurn()).toBe(true)
 f.frames(.04,6,'explicit')
 f.setAppendAllowed(false)
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.clear')).toHaveLength(2)
 const revoked=f.sent.length
 f.setAppendAllowed(true)
 f.frames(.04,6,'explicit')
 f.manual.finishExplicitTurn()
 expect(f.sent).toHaveLength(revoked)
 expect(f.sent.some(e=>e.type==='input_audio_buffer.commit'||e.type==='response.create')).toBe(false)
 expect(f.manual.pendingCount()).toBe(0)
})
it('discards an unsubmitted local automatic segment without sending any of its audio',()=>{
 const f=fixture()
 f.frames(.04,6)
 expect(f.sent.some(e=>e.type==='input_audio_buffer.append')).toBe(false)
 expect(f.manual.beginExplicitTurn()).toBe(true)
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.clear')).toHaveLength(0)
 f.frames(.09,6,'explicit')
 f.manual.finishExplicitTurn()
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.commit')).toHaveLength(1)
 const packetStarts=f.sent.filter(e=>e.type==='input_audio_buffer.append')
  .map(e=>Buffer.from(e.audio,'base64').readInt16LE(0))
 expect(packetStarts.length).toBeGreaterThan(0)
 expect(packetStarts.every(level=>level===Math.round(.09*32767))).toBe(true)
})
it('an explicit gesture preserves the previous unsounded reply and the admitted follow-up',async()=>{
 const f=fixture()
 f.speech()
 expect(f.manual.inputCommitted('automatic-item')).toBe(true)
 expect(await f.manual.inputCompleted('automatic-item','我想学化学')).toBe(true)
 f.interruptReply.mockClear()
 f.manual.interruptPlayingReply()
 expect(f.manual.beginExplicitTurn()).toBe(true)
 f.frames(.04,6,'explicit')
 f.manual.finishExplicitTurn()
 expect(f.manual.responseCreated('automatic-reply')).toBe(true)
 expect(f.interruptReply).not.toHaveBeenCalled()
 expect(f.manual.inputCommitted('followup-item')).toBe(true)
 expect(await f.manual.inputCompleted('followup-item','能用例子说明吗')).toBe(true)
 expect(f.interruptReply).toHaveBeenCalledExactlyOnceWith('confirmed_input')
 expect(f.manual.responseCreated('followup-reply')).toBe(true)
})
it('automatic capture can take a fresh turn after the explicit turn is submitted',async()=>{
 const f=fixture()
 expect(f.manual.beginExplicitTurn()).toBe(true)
 f.frames(.04,6,'explicit')
 f.manual.finishExplicitTurn()
 expect(f.manual.inputCommitted('explicit-item')).toBe(true)
 expect(await f.manual.inputCompleted('explicit-item','为什么要先算括号')).toBe(true)
 expect(f.manual.responseCreated('explicit-reply')).toBe(true)
 f.speech()
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.commit')).toHaveLength(2)
 expect(f.manual.inputCommitted('automatic-item')).toBe(true)
 expect(await f.manual.inputCompleted('automatic-item','如果有两层括号呢')).toBe(true)
 expect(f.manual.responseCreated('automatic-reply')).toBe(true)
 expect(f.ledger.completeInput.mock.calls).toEqual([
  ['explicit-item','为什么要先算括号'],['automatic-item','如果有两层括号呢'],
 ])
})
it('a manual takeover can stop a playing reply before any new microphone frames arrive',async()=>{
 const f=fixture()
 f.speech()
 expect(f.manual.inputCommitted('previous-item')).toBe(true)
 expect(await f.manual.inputCompleted('previous-item','先介绍一下化学')).toBe(true)
 expect(f.manual.responseCreated('previous-reply')).toBe(true)
 f.interruptReply.mockClear()
 f.scheduleAudio()
 f.manual.interruptPlayingReply()
 expect(f.interruptReply).toHaveBeenCalledExactlyOnceWith('speech_onset')
 expect(f.sent.filter(e=>e.type==='input_audio_buffer.commit')).toHaveLength(1)
})
it('does not create a response after a synchronous input notification retires the owner',async()=>{
 const workspace=vi.fn(()=>true),f=fixture(workspace)
 f.inputCompleted.mockImplementationOnce(()=>f.current(false))
 f.speech();expect(f.manual.inputCommitted('first-item')).toBe(true)
 expect(await f.manual.inputCompleted('first-item','继续讲')).toBe(false)
 expect(f.interruptReply).not.toHaveBeenCalled()
 expect(workspace).not.toHaveBeenCalled()
 expect(f.sent.some(e=>e.type==='response.create')).toBe(false)
})
it('cleans the expired owner without status emission or appending its queued audio',async()=>{
 const f=fixture();f.speech();f.manual.inputCommitted('owner-item');f.current(false)
 const statuses=f.status.mock.calls.length,sends=f.sent.length
 await f.manual.inputCompleted('owner-item','')
 expect(f.status).toHaveBeenCalledTimes(statuses);expect(f.sent).toHaveLength(sends)
 await vi.advanceTimersByTimeAsync(30001);expect(f.failed).not.toHaveBeenCalled()
})
it('classifies the exact real manual completion timeout using the existing closed reason',()=>{
 expect(classifyQwenRealtimeError('刚才的语音没有取得完整转写和保存回执，正在恢复连接；未确认的原话请重说。')).toBe('QWEN_REALTIME_INPUT_ACTIVITY_UNCONFIRMED')
 expect(classifyQwenRealtimeError('其他服务没有保存回执。')).toBe('QWEN_REALTIME_UNKNOWN')
})
