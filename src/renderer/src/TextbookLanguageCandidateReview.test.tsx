import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TextbookLanguageCandidateReview } from './TextbookLanguageCandidateReview'
import { candidatePayloadHash } from './textbookCandidateCommands'

function withDraftStore(api: (path:string,init?:RequestInit) => Promise<unknown>): typeof api {
  const drafts = new Map<string,Record<string,unknown>>()
  return async (path,init) => {
    const scope = path.match(/\/textbook-capture\/([^/]+)\/drafts\/(capture|language)$/)
    if(!scope) return api(path,init)
    let draft = drafts.get(path) ?? {child_id:scope[1],scope:scope[2],storage_generation:'synthetic-generation',revision:0,payload:null,
      last_action_id:null,last_expected_revision:null,updated_at:null,stale_refs:[],cloud_transmission_allowed:false}
    if(init) {
      const request = JSON.parse(String(init.body))
      if(request.expected_revision !== draft.revision) throw new Error('synthetic draft conflict')
      draft = {...draft,revision:Number(draft.revision)+1,payload:request.payload,last_action_id:request.client_action_id,last_expected_revision:request.expected_revision,updated_at:'now'}
      drafts.set(path,draft)
    }
    return {draft:structuredClone(draft)}
  }
}

describe('TextbookLanguageCandidateReview', () => {
  afterEach(() => { cleanup(); localStorage.clear() })
  beforeEach(() => {
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:parent-page')
      static revokeObjectURL = vi.fn()
    })
  })

  const extractCandidates = async (): Promise<void> => {
    const button = await screen.findByRole('button', { name: '抽取保守候选' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
  }

  it('keeps newly extracted candidates when an older list response arrives late', async () => {
    let resolveList!: (result: { items: [] }) => void
    const stale = new Promise<{ items: [] }>((resolve) => { resolveList = resolve })
    const api = vi.fn((path: string) => {
      if (path.endsWith('/textbook-pages')) return Promise.resolve({ items: [{ page_ref: 'delayed-page', title: '四上英语', page_number: 1 }] })
      if (path.endsWith('/extract')) return Promise.resolve({ items: [{ candidate_ref: 'fresh-apple', language: 'english', term: 'apple', focus_kind: 'word', status: 'pending' }] })
      return stale
    })
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} />)
    await waitFor(() => expect(api).toHaveBeenCalledWith('/v1/parent/knowledge-graph/textbook-pages/delayed-page/language-candidates'))
    await extractCandidates()
    expect(await screen.findByDisplayValue('apple')).toBeInTheDocument()
    await act(async () => { resolveList({ items: [] }); await stale })
    expect(screen.getByDisplayValue('apple')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '批准并用于学习' })).toBeEnabled()
  })

  it('persists a canonical candidate command through response loss and reconciles it after reload', async () => {
    const page = { page_ref: 'page-replay', title: '四上英语', textbook_version: '译林版', volume: '四上', page_number: 1 }
    let confirmed = false
    const api = vi.fn((path: string, init?: RequestInit) => {
      if (path.endsWith('/textbook-pages')) return Promise.resolve({ items: [page] })
      if (path.includes('/extract')) return Promise.resolve({ items: [{ candidate_ref: 'candidate-replay', language: 'english', term: 'apple', focus_kind: 'word', status: confirmed ? 'confirmed' : 'pending' }] })
      if (path.includes('/language-candidates/')) { confirmed = true; return Promise.reject(new Error('response lost after save')) }
      return Promise.resolve({ items: [] })
    })
    const first = render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} />)
    await extractCandidates()
    fireEvent.click(await screen.findByRole('button', { name: '批准并用于学习' }))
    await screen.findByText(/保存失败/)
    expect(localStorage.length).toBe(1)
    first.unmount()
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} />)
    await extractCandidates()
    expect(await screen.findByText(/已确认并可用于学生学习/)).toBeInTheDocument()
    expect(localStorage.length).toBe(0)
    expect(candidatePayloadHash({ b: 2, a: 1 })).toBe(candidatePayloadHash({ a: 1, b: 2 }))
  })

  it('keeps the persistent command and reports processing when another service owns the projection lease', async () => {
    const page = { page_ref: 'page-processing', title: '四上英语', textbook_version: '译林版', volume: '四上', page_number: 1 }
    const candidate = { candidate_ref: 'candidate-processing', language: 'english', term: 'apple', focus_kind: 'word', status: 'pending' }
    const api = vi.fn((path: string) => {
      if (path.endsWith('/textbook-pages')) return Promise.resolve({ items: [page] })
      if (path.includes('/extract')) return Promise.resolve({ items: [candidate] })
      return Promise.resolve({ item: { ...candidate, status: 'projecting' } })
    })
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} />)
    await extractCandidates()
    fireEvent.click(await screen.findByRole('button', { name: '批准并用于学习' }))
    expect(await screen.findByText(/确认处理中/)).toBeInTheDocument()
    expect(localStorage.length).toBe(1)
    expect(screen.queryByText(/已确认并投影为可诊断/)).not.toBeInTheDocument()
  })

  it('keeps opaque handles out of the parent-facing surface and only confirms an edited page term', async () => {
    const page = { page_ref: 'ocr-page-internal-handle', title: '四上英语 Unit 1', textbook_version: '译林版', volume: '四上', page_number: 6 }
    const candidate = { candidate_ref: 'candidate-internal-handle', language: 'chinese', term: '行', focus_kind: 'character', status: 'pending' }
    let savedInit: RequestInit | undefined
    const api = vi.fn((path: string, init?: RequestInit) => {
      if (path.includes('/language-candidates/') && !path.endsWith('/extract')) savedInit = init
      if (path.endsWith('/textbook-pages')) return Promise.resolve({ items: [page] })
      if (path.includes('/extract')) return Promise.resolve({ items: [candidate] })
      if (path.endsWith('/language-candidates')) return Promise.resolve({ items: [] })
      return Promise.resolve({ item: candidate })
    })
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} />)
    expect(await screen.findByText('四上英语 Unit 1 · 译林版 · 四上 · 第6页')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('ocr-page-internal-handle')
    await waitFor(() => expect(api).toHaveBeenCalledWith(expect.stringMatching(/\/language-candidates$/)))
    fireEvent.click(screen.getByRole('button', { name: '抽取保守候选' }))
    expect(await screen.findByText('语文 · 字')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('candidate-internal-handle')
    fireEvent.change(screen.getByLabelText('读音'), { target: { value: 'xíng, háng' } })
    fireEvent.click(screen.getByRole('button', { name: '批准并用于学习' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/v1/parent/knowledge-graph/language-candidates/candidate-internal-handle', expect.objectContaining({ method: 'PUT' })))
    expect(savedInit?.body).toContain('xíng')
  })

  it('does not offer edit/delete controls for a confirmed term but supports explicit withdrawal', async () => {
    const page = { page_ref: 'page-handle', title: '四上英语 Unit 1', textbook_version: '译林版', volume: '四上', page_number: 6 }
    const candidate = { candidate_ref: 'confirmed-handle', language: 'english', term: 'apple', focus_kind: 'word', status: 'confirmed' }
    const api = vi.fn((path: string) => Promise.resolve(path.endsWith('/textbook-pages') ? { items: [page] } : { items: [candidate] }))
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={api} />)
    await screen.findByText('四上英语 Unit 1 · 译林版 · 四上 · 第6页')
    fireEvent.click(screen.getByRole('button', { name: '抽取保守候选' }))
    expect(await screen.findByText(/已确认并可用于学生学习/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '批准并用于学习' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '不采用' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '撤销确认' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/v1/parent/knowledge-graph/language-candidates/confirmed-handle/withdraw', expect.objectContaining({ method: 'POST' })))
  })

  it('offers parent-owned document-camera capture while retaining the OCR paste fallback', async () => {
    const api = vi.fn((path: string) => Promise.resolve(path.endsWith('/textbook-pages') ? { items: [] } : { items: [] }))
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={withDraftStore(api)} childId="child-primary-4" />)
    fireEvent.click(screen.getByText('导入一页本机 OCR 文字'))
    expect(await screen.findByText('本机原页采集')).toBeInTheDocument()
    expect(screen.getByLabelText('教材页 OCR 文字')).toBeInTheDocument()
  })

  it('keeps subject-matter candidate approval out of the parent capture workflow', async () => {
    const api = vi.fn((path: string) => Promise.resolve(path.endsWith('/textbook-pages')
      ? { items: [{ page_ref: 'saved-page', title: '初三物理', page_number: 2 }] }
      : { items: [] }))
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={withDraftStore(api)} childId="child-3" captureOnly />)
    expect(await screen.findByText(/1 页资料/)).toBeInTheDocument()
    expect(screen.getByText(/这些页面的学科知识尚未核验/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '抽取保守候选' })).not.toBeInTheDocument()
    expect(screen.queryByText(/补入页面中可见/)).not.toBeInTheDocument()
    expect(api).not.toHaveBeenCalledWith(expect.stringContaining('/language-candidates'))
  })

  it('restores a ready OCR artifact after reload and lets the parent bring it into the textbook form', async () => {
    const ready = { capture_ref: 'opaque-ready-ref', source_version:'ready-source-version', child_id: 'child-4', status: 'ready', extracted_text: 'Words: apple', page_context: { title: '四上英语 Unit 1', page_number: 6, grade_scope: '四年级', subject_scope: '英语' }, details: { local_pipeline: { manual_review: { required: false } } } }
    const api = vi.fn((path: string) => {
      if (path.endsWith('/artifacts')) return Promise.resolve({ items: [ready] })
      if (path.endsWith('/textbook-pages')) return Promise.resolve({ items: [] })
      return Promise.resolve({ items: [] })
    })
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={withDraftStore(api)} childId="child-4" />)
    expect(await screen.findByText('已校对，可带入教材表单')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('opaque-ready-ref')
    await waitFor(() => expect(screen.getByRole('button', {name:'带入教材表单'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '带入教材表单' }))
    fireEvent.click(screen.getByText('导入一页本机 OCR 文字'))
    expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('Words: apple')
    expect(screen.getByLabelText('教材页标题')).toHaveValue('四上英语 Unit 1')
    await screen.findByText('草稿已保存到本机。')
  })

  it('keeps unsaved correction drafts scoped to their individual OCR artifact', async () => {
    const artifacts = [
      { capture_ref: 'opaque-a', source_version:'source-a', child_id: 'child-4', status: 'needs_review', extracted_text: 'first text', page_context: { title: 'A' }, details: { local_pipeline: { manual_review: { required: true } } } },
      { capture_ref: 'opaque-b', source_version:'source-b', child_id: 'child-4', status: 'needs_review', extracted_text: 'second text', page_context: { title: 'B' }, details: { local_pipeline: { manual_review: { required: true } } } },
    ]
    const saved: string[] = []
    const api = vi.fn((path: string, init?: RequestInit) => {
      if (path.endsWith('/artifacts')) return Promise.resolve({ items: artifacts })
      if (path.endsWith('/textbook-pages')) return Promise.resolve({ items: [] })
      if (path.includes('/review')) {
        const body = JSON.parse(String(init?.body)) as { extracted_text: string }
        saved.push(body.extracted_text)
        return Promise.resolve({ artifact: { ...artifacts[1], status: 'ready', extracted_text: body.extracted_text } })
      }
      return Promise.resolve({ items: [] })
    })
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={withDraftStore(api)} childId="child-4" />)
    const boxes = await screen.findAllByRole('textbox', { name: /校对后的教材 OCR 文字/ })
    await waitFor(() => expect(boxes[0]).toBeEnabled())
    fireEvent.change(boxes[0], { target: { value: 'edited first text' } })
    fireEvent.click(screen.getAllByRole('button', { name: '校对并带入表单' })[1])
    await waitFor(() => expect(saved).toEqual(['second text']))
    await waitFor(() => expect(screen.getByLabelText('教材页 OCR 文字')).toHaveValue('second text'))
  })

  it('shows the retained source beside OCR text and releases it when the review closes', async () => {
    const artifact = { capture_ref: 'opaque-source', status: 'needs_review', extracted_text: '第一题 速度是多少', page_context: { title: '初三物理' }, details: { local_pipeline: { manual_review: { required: true } } }, raw_image_retained: true, raw_image_media_type: 'image/png', raw_image_byte_length: 128 }
    const api = vi.fn((path: string) => Promise.resolve(path.endsWith('/artifacts') ? { items: [artifact] } : { items: [] }))
    const readSource = vi.fn(async () => ({ bytes: new Uint8Array([137, 80, 78, 71]).buffer, mediaType: 'image/png' as const }))
    const view = render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={withDraftStore(api)} childId="child-4" readSource={readSource} />)
    await waitFor(() => expect(screen.getByLabelText('教材页标题')).toBeEnabled())
    fireEvent.click(await screen.findByRole('button', { name: '查看原页' }))
    expect(await screen.findByRole('img', { name: '待校对教材原页' })).toHaveAttribute('src', 'blob:parent-page')
    expect(readSource).toHaveBeenCalledWith('child-4', 'opaque-source', expect.any(AbortSignal))
    expect(screen.getByLabelText('校对后的教材 OCR 文字（第 1 项）')).toHaveValue('第一题 速度是多少')
    fireEvent.click(screen.getByRole('button', { name: '关闭原页' }))
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:parent-page')
    view.unmount()
  })

  it('does not offer a source preview when the retained original has been deleted', async () => {
    const artifact = { capture_ref: 'opaque-deleted', status: 'failed', extracted_text: '', page_context: {}, details: {}, raw_image_retained: false, raw_image_byte_length: 0 }
    const api = vi.fn((path: string) => Promise.resolve(path.endsWith('/artifacts') ? { items: [artifact] } : { items: [] }))
    render(<TextbookLanguageCandidateReview recoveryFactory={()=>'memory-only'} api={withDraftStore(api)} childId="child-4" readSource={vi.fn()} />)
    expect(await screen.findByText('原页不可用，需重新采集。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '查看原页' })).not.toBeInTheDocument()
  })
})
