import { act,cleanup,fireEvent,render,screen,waitFor } from '@testing-library/react'
import { useLayoutEffect,useState } from 'react'
import { afterEach,expect,it,vi } from 'vitest'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import { ParentTextbookDraftHost } from './parentTextbookDraftHost'
import { changeParentDraftAuthority } from './parentDraftAuthority'
import type { TextbookEditingPayload } from './parentTextbookDraftJournal'
afterEach(()=>cleanup())
const isMutation=(init?:RequestInit):boolean=>!['GET','HEAD','OPTIONS'].includes((init?.method??'GET').toUpperCase())
it('treats signal-only requests as reads while retaining every mutating method in the offline fixture',()=>{
 const signal=new AbortController().signal
 for(const init of [undefined,{signal},{method:'GET',signal},{method:'head',signal},{method:'OPTIONS',signal}])expect(isMutation(init)).toBe(false)
 for(const method of ['POST','PUT','PATCH','DELETE','post'])expect(isMutation({method,signal})).toBe(true)
})
const raw:TextbookEditingPayload={page:{title:'private retained'},capture_ref:'',capture_version:'',review_drafts:{},action_ids:{}}
const receipt=(payload:TextbookEditingPayload|null)=>({draft:{child_id:'a',scope:'capture',storage_generation:'generation-a',revision:1,payload,last_action_id:null,last_expected_revision:null,updated_at:null,stale_refs:[],cloud_transmission_allowed:false}})
it('cannot overwrite retained raw with blank initial refs when a parent locks in the first layout frame',async()=>{
 const api=vi.fn(async(path:string)=>path.includes('/drafts/')?receipt(raw):{items:[]}),host=new ParentTextbookDraftHost(()=>'memory-only'),detach=host.attach()
 const entry=host.entry(api,'a','capture');await entry.journal.load();entry.journal.keepEditing(raw)
 function LockAtLayout(){const[open,setOpen]=useState(true);useLayoutEffect(()=>{changeParentDraftAuthority('locked');setOpen(false)},[]);return open?<TextbookLanguageCandidateReview draftHost={host}api={api}childId="a"captureOnly/>:<p>locked</p>}
 try{render(<LockAtLayout/>);expect(screen.getByText('locked')).toBeInTheDocument();expect(entry.journal.state.payload).toEqual(raw);expect(api.mock.calls.filter(([p])=>p.includes('/drafts/'))).toHaveLength(1)}finally{detach()}
})
it('keeps the last synchronous input, removes original-image URLs on lock, and requires explicit readback before rendering again',async()=>{
 const objective={question_number:'6',full_question:'public question',official_answers:['17'],candidate_node_id:null,
  paper:{capture_ref:'paper',source_version:'paper-v1'},answer_key:{capture_ref:'answer',source_version:'answer-v1'}}
 const saved={...raw,objective_draft:objective}
 const artifacts=['paper','answer'].map(ref=>({capture_ref:ref,source_version:ref+'-v1',status:'needs_review',extracted_text:'public OCR',confidence:.6,raw_image_retained:true,raw_image_media_type:'image/png',raw_image_byte_length:8,page_context:{title:ref,page_number:1}}))
 const api=vi.fn(async(path:string,init?:RequestInit)=>{if(isMutation(init))throw Error('synthetic offline');return path.includes('/drafts/')?receipt(saved):path.endsWith('/artifacts')?{items:artifacts}:{items:[],nodes:[]}})
 const source=vi.fn(async()=>({bytes:new ArrayBuffer(8),mediaType:'image/png'as const})),host=new ParentTextbookDraftHost(()=>'memory-only'),detach=host.attach()
 const create=Object.getOwnPropertyDescriptor(URL,'createObjectURL'),revoke=Object.getOwnPropertyDescriptor(URL,'revokeObjectURL')
 Object.defineProperty(URL,'createObjectURL',{configurable:true,value:vi.fn(()=>`blob:public-${Math.random()}`)});Object.defineProperty(URL,'revokeObjectURL',{configurable:true,value:vi.fn()})
 try{
  render(<TextbookLanguageCandidateReview draftHost={host}api={api}childId="a"captureOnly readSource={source}/>)
  await screen.findByAltText('试卷原页');await screen.findByAltText('答案原页')
  const writeCount=api.mock.calls.filter(([,init])=>isMutation(init)).length
  act(()=>{fireEvent.change(screen.getByLabelText('教材页标题'),{target:{value:'last exact character🙂'}});changeParentDraftAuthority('locked')})
  expect(screen.queryByLabelText('教材页标题')).not.toBeInTheDocument();expect(screen.queryByAltText('试卷原页')).not.toBeInTheDocument();expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
  expect(host.entry(api,'a','capture').journal.state.payload).toMatchObject({page:{title:'last exact character🙂'}})
  const before=api.mock.calls.length,sourceBefore=source.mock.calls.length
  act(()=>changeParentDraftAuthority('unlocked'));expect(screen.queryByDisplayValue('last exact character🙂')).not.toBeInTheDocument();expect(api).toHaveBeenCalledTimes(before);expect(source).toHaveBeenCalledTimes(sourceBefore)
  fireEvent.click(screen.getByRole('button',{name:'核对并恢复本窗口草稿'}))
  await waitFor(()=>expect(screen.getByLabelText('教材页标题')).toHaveValue('last exact character🙂'))
  expect(api.mock.calls.filter(([,init])=>isMutation(init))).toHaveLength(writeCount)
 }finally{cleanup();detach();if(create)Object.defineProperty(URL,'createObjectURL',create);else delete(URL as unknown as Record<string,unknown>).createObjectURL;if(revoke)Object.defineProperty(URL,'revokeObjectURL',revoke);else delete(URL as unknown as Record<string,unknown>).revokeObjectURL}
})
