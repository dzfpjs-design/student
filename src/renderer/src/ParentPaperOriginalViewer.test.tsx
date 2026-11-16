import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import {ParentPaperObjectiveDraft} from './ParentPaperObjectiveDraft'
const pages=['paper','answer_key'].map((role,i)=>({capture_ref:role,source_version:String(i+1).repeat(64),status:'needs_review',raw_image_retained:true,page_context:{title:role,page_number:1}}))
const value={question_number:'6',full_question:'公开合成原题',official_answers:['17'],candidate_node_id:null,
  paper:{capture_ref:pages[0].capture_ref,source_version:pages[0].source_version},answer_key:{capture_ref:pages[1].capture_ref,source_version:pages[1].source_version}}
let revoke:ReturnType<typeof vi.fn>,count=0
beforeEach(()=>{count=0;Object.defineProperty(URL,'createObjectURL',{configurable:true,value:vi.fn(()=>`blob:public-original-${++count}`)})
  revoke=vi.fn();Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:revoke})})
afterEach(()=>{cleanup();vi.restoreAllMocks()})
const api=async(path:string)=>path.endsWith('/submissions')?{items:[]}:{nodes:[]}
const source=async(child:string)=>{if(child!=='public-a')throw Error('wrong synthetic owner');return {bytes:new ArrayBuffer(8),mediaType:'image/png' as const}}
const props={childId:'public-a',api,artifacts:pages,value,onChange:vi.fn(),onSave:async()=>undefined,readSource:source,blocked:false}
it('both originals open in the same App with zoom, Escape and keyboard focus return; no external blob window',async()=>{
  const external=vi.spyOn(window,'open');render(<ParentPaperObjectiveDraft {...props}/>)
  const paper=await screen.findByRole('button',{name:'在本页放大核对试卷原页'})
  paper.focus();fireEvent.click(paper)
  const dialog=screen.getByRole('dialog',{name:'试卷原页放大核对'})
  expect(screen.getByRole('button',{name:'关闭原页放大'})).toHaveFocus()
  expect(screen.getByAltText('放大的试卷原页')).toHaveAttribute('src','blob:public-original-1')
  fireEvent.click(screen.getByRole('button',{name:'放大'}))
  expect(screen.getByAltText('放大的试卷原页')).toHaveStyle({width:'200%'})
  const scroll=screen.getByLabelText('原页放大滚动区域');scroll.focus();fireEvent.keyDown(scroll,{key:'Tab'})
  expect(screen.getByRole('button',{name:'关闭原页放大'})).toHaveFocus()
  fireEvent.keyDown(dialog,{key:'Escape'})
  expect(screen.queryByRole('dialog')).toBeNull();expect(paper).toHaveFocus()
  fireEvent.click(screen.getByRole('button',{name:'在本页放大核对答案原页'}))
  expect(screen.getByRole('dialog',{name:'答案原页放大核对'})).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button',{name:'关闭原页放大'}))
  expect(external).not.toHaveBeenCalled();expect(document.querySelector('a[target="_blank"]')).toBeNull()
})
it('a lock immediately removes enlarged and inline images and revokes the original URLs',async()=>{
  const view=render(<ParentPaperObjectiveDraft {...props}/>);fireEvent.click(await screen.findByRole('button',{name:'在本页放大核对试卷原页'}))
  view.rerender(<ParentPaperObjectiveDraft {...props} blocked/>)
  expect(screen.queryByRole('dialog')).toBeNull();expect(screen.queryAllByRole('img')).toHaveLength(0)
  await waitFor(()=>expect(revoke).toHaveBeenCalledTimes(2))
})
it('switching child removes the old viewer before new reads and does not expose the old original URL',async()=>{
  const view=render(<ParentPaperObjectiveDraft {...props}/>);fireEvent.click(await screen.findByRole('button',{name:'在本页放大核对答案原页'}))
  view.rerender(<ParentPaperObjectiveDraft {...props} childId="public-b"/>)
  expect(screen.queryByRole('dialog')).toBeNull();expect(screen.queryAllByRole('img')).toHaveLength(0)
  await waitFor(()=>expect(revoke).toHaveBeenCalledTimes(2))
})
