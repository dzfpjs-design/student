import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import { ParentPaperObjectiveDraft } from './ParentPaperObjectiveDraft'
import type { PaperObjectiveDraft } from './parentTextbookDraftJournal'
import { ApiError } from './api'

const artifacts = ['paper', 'answer'].map((role, index) => ({capture_ref:role,source_version:String(index+1).repeat(64),
  status:'needs_review',extracted_text:'未核对的公开合成 OCR',confidence:.6,raw_image_retained:true,
  raw_image_media_type:'image/png',raw_image_byte_length:8,page_context:{title:index?'公开答案页':'公开试卷页',page_number:1}}))
function service() {
  let saved: Record<string, unknown>|null=null, revision=0, lose=false,generation='synthetic-generation-a'
  const writes: {client_action_id:string;payload:Record<string,unknown>|null}[]=[]
  const api=vi.fn(async(path:string,init?:RequestInit):Promise<unknown>=>{
    if(path.includes('/drafts/capture')) {
      if(init?.method==='PUT') {
        const body=JSON.parse(String(init.body));if(body.expected_generation!==generation)throw new ApiError('synthetic restore',409,'TEXTBOOK_DRAFT_CONFLICT')
        saved=structuredClone(body.payload);revision++;writes.push(body)
        const value=receipt(body.client_action_id,body.expected_revision)
        if(lose){lose=false;throw Error('synthetic lost response after commit')}
        return value
      }
      return receipt(writes.at(-1)?.client_action_id??null,revision?revision-1:null)
    }
    if(path.endsWith('/artifacts'))return {items:artifacts}
    if(path.endsWith('/textbook-pages'))return {items:[]}
    if(path.endsWith('/knowledge-graph'))return {nodes:[{knowledge_node_id:'public-math-node',title:'整数加法',subject:'数学',node_type:'skill'}]}
    throw Error('unexpected local route')
  })
  function receipt(action:string|null,expected:number|null){return {draft:{child_id:'child-4',scope:'capture',
    storage_generation:generation,revision,payload:structuredClone(saved),last_action_id:action,
    last_expected_revision:expected,updated_at:null,stale_refs:[],cloud_transmission_allowed:false}}}
  return {api,writes,get saved(){return saved},loseNext:()=>{lose=true},restoreGeneration:()=>{generation='restored-generation-b'}}
}
const source=vi.fn(async()=>({bytes:new ArrayBuffer(8),mediaType:'image/png' as const}))
beforeEach(()=>{
  Object.defineProperty(URL,'createObjectURL',{configurable:true,value:vi.fn(()=>`blob:synthetic-${Math.random()}`)})
  Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:vi.fn()})
})
afterEach(()=>{cleanup();vi.clearAllMocks()})
async function edit(){
  fireEvent.click(await screen.findByRole('button',{name:'整理单题审核草稿'}))
  fireEvent.change(screen.getByLabelText('试卷原页'),{target:{value:'paper'}})
  fireEvent.change(screen.getByLabelText('答案原页'),{target:{value:'answer'}})
  fireEvent.change(screen.getByLabelText('题号'),{target:{value:'6'}})
  fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'公开合成题：12+5=?'}})
  fireEvent.change(screen.getByLabelText('官方答案（待审核，每行一项）'),{target:{value:'17'}})
}
it('saves understandable paired originals and complete editing, then reopens without any approval call',async()=>{
  const store=service();const props={api:store.api,childId:'child-4',captureOnly:true,readSource:source}
  const first=render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} {...props}/>);await edit()
  await screen.findByRole('option',{name:'数学 · 整数加法'})
  fireEvent.change(screen.getByLabelText('知识点候选（可暂不选择）'),{target:{value:'public-math-node'}})
  await screen.findByAltText('试卷原页');await screen.findByAltText('答案原页')
  fireEvent.click(screen.getByRole('button',{name:'保存单题审核草稿'}))
  await screen.findByText('单题审核草稿已保存；尚未批准或生成复测。')
  const objective=store.saved?.objective_draft as Record<string,unknown>
  expect(objective).toMatchObject({question_number:'6',full_question:'公开合成题：12+5=?',official_answers:['17'],candidate_node_id:'public-math-node'})
  expect(objective.paper).toEqual({capture_ref:'paper',source_version:'1'.repeat(64)})
  expect(objective.answer_key).toEqual({capture_ref:'answer',source_version:'2'.repeat(64)})
  first.unmount();render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} {...props}/>)
  expect(await screen.findByLabelText('完整题干（待审核）')).toHaveValue('公开合成题：12+5=?')
  expect(screen.getByLabelText('官方答案（待审核，每行一项）')).toHaveValue('17')
  expect(store.api.mock.calls.every(([path])=>!/(publish|first-sources|review-task|knowledge-sources)/.test(path))).toBe(true)
  expect(URL.revokeObjectURL).toHaveBeenCalled()
})
it('a lost committed save reply reconciles from the same server receipt, without a second PUT',async()=>{
  const store=service();render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={store.api} childId="child-4" captureOnly readSource={source}/>)
  await edit();await waitFor(()=>expect(store.saved?.objective_draft).toBeTruthy())
  store.loseNext()
  fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'修改后的公开合成完整题干'}})
  await waitFor(()=>expect((store.saved?.objective_draft as Record<string,unknown>)?.full_question).toBe('修改后的公开合成完整题干'))
  await screen.findByText(/草稿已保存|已从本机恢复/)
  const matches=store.writes.filter(row=>(row.payload?.objective_draft as Record<string,unknown>)?.full_question==='修改后的公开合成完整题干')
  expect(matches).toHaveLength(1)
})
it('selecting the same original twice cannot silently save the previous pair under new labels',async()=>{
  const store=service();render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={store.api} childId="child-4" captureOnly readSource={source}/>)
  await edit();await waitFor(()=>expect(store.saved?.objective_draft).toBeTruthy())
  const previous=structuredClone(store.saved?.objective_draft)
  fireEvent.change(screen.getByLabelText('答案原页'),{target:{value:'paper'}})
  expect(screen.getByRole('button',{name:'保存单题审核草稿'})).toBeDisabled()
  expect(screen.getByLabelText('完整题干（待审核）')).toBeDisabled()
  expect(store.saved?.objective_draft).toEqual(previous)
})

it('a restored generation blocks writes while comparison shows complete raw text on both sides',async()=>{
  const store=service();render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={store.api} childId="child-4" captureOnly readSource={source}/>)
  await edit();fireEvent.change(screen.getByLabelText('官方答案（待审核，每行一项）'),{target:{value:'测'.repeat(201)}})
  await waitFor(()=>expect((store.saved?.objective_draft as PaperObjectiveDraft)?.raw_editing?.official_answers_text).toBe('测'.repeat(201)))
  store.restoreGeneration();fireEvent.change(screen.getByLabelText('完整题干（待审核）'),{target:{value:'本窗口未保存的原始题干'}})
  fireEvent.click(await screen.findByRole('button',{name:'对照两份草稿'}))
  await screen.findByText('本机数据已删除或恢复换代，旧窗口内容不能自动恢复。')
  expect(screen.getAllByText('测'.repeat(201),{selector:'p'})).toHaveLength(2)
  expect(screen.getByLabelText('完整题干（待审核）')).toBeDisabled()
  expect((store.saved?.objective_draft as PaperObjectiveDraft).raw_editing?.full_question).not.toBe('本窗口未保存的原始题干')
})

const boundDraft = ():PaperObjectiveDraft => ({question_number:'6',full_question:'保留的公开原题',official_answers:['17'],candidate_node_id:null,
  paper:{capture_ref:'paper',source_version:'1'.repeat(64)},answer_key:{capture_ref:'answer',source_version:'2'.repeat(64)}})

it('either original version changing keeps the text visible and prevents a stale save',()=>{
  const draft=boundDraft(),onSave=vi.fn(async()=>undefined)
  const view=render(<ParentPaperObjectiveDraft childId="child-4" api={async()=>({nodes:[]})} artifacts={artifacts}
    value={draft} onChange={vi.fn()} onSave={onSave} blocked={false}/>)
  view.rerender(<ParentPaperObjectiveDraft childId="child-4" api={async()=>({nodes:[]})}
    artifacts={artifacts.map(page=>page.capture_ref==='answer'?{...page,source_version:'3'.repeat(64)}:page)}
    value={draft} onChange={vi.fn()} onSave={onSave} blocked={false}/>)
  expect(screen.getByLabelText('完整题干（待审核）')).toHaveValue('保留的公开原题')
  expect(screen.getByRole('button',{name:'保存单题审核草稿'})).toBeDisabled()
  expect(screen.getByRole('alert')).toHaveTextContent('原页版本已变化')
  expect(onSave).not.toHaveBeenCalled()
})

it('switching child aborts old original reads and never displays a late response from the old child',async()=>{
  const pending:((value:{bytes:ArrayBuffer;mediaType:'image/png'})=>void)[]=[]
  const signals:AbortSignal[]=[]
  const slow=vi.fn(async(_child:string,_ref:string,signal:AbortSignal)=>{
    signals.push(signal);return await new Promise<{bytes:ArrayBuffer;mediaType:'image/png'}>(resolve=>pending.push(resolve))
  })
  const draft=boundDraft(),props={api:async()=>({nodes:[]}),artifacts,value:draft,onChange:vi.fn(),onSave:vi.fn(async()=>undefined),readSource:slow,blocked:false}
  const view=render(<ParentPaperObjectiveDraft {...props} childId="child-4"/>)
  await waitFor(()=>expect(pending).toHaveLength(2))
  view.rerender(<ParentPaperObjectiveDraft {...props} value={null} artifacts={[]} childId="child-9"/>)
  expect(signals.every(signal=>signal.aborted)).toBe(true)
  for(const resolve of pending)resolve({bytes:new ArrayBuffer(8),mediaType:'image/png'})
  await waitFor(()=>expect(screen.queryByAltText('试卷原页')).not.toBeInTheDocument())
  expect(URL.createObjectURL).not.toHaveBeenCalled()
})
