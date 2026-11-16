import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MistakeBookPrint } from './MistakeBookPrint'
import { buildMistakePrintDocument, defaultPrintOptions } from './MistakePrintDocument'
import { loadMistakePrintCrop, loadMistakePrintSnapshot, type MistakePrintItem, type MistakePrintSnapshot } from './mistakeBookPrintApi'
import type { ChildProfile } from './types'

vi.mock('./mistakeBookPrintApi', () => ({ loadMistakePrintSnapshot: vi.fn(), loadMistakePrintCrop: vi.fn() }))
const child: ChildProfile = { child_id: 'synthetic-child', nickname: '示例同学', grade: '四年级', curriculum_region: '', status: 'active' }
const print = vi.fn()
function item(id: string, subject = '数学', count = 0): MistakePrintItem {
  return {
    record: { version: 1, mistake_id: id, child_id: child.child_id, learning_session_id: null, revision: 0, source_revision: `revision-${id}`, source: { title: `${subject}题${id}`, subject, question: `${subject}题干 ${id}`, original_answer: '原来的作答', created_at: '2026-10-12T03:00:00Z' }, analysis: null, practice_items: [], turns: [], today: { date: '2026-10-12', target_count: 3, completed_count: 0, remaining_count: 3, status: 'not_started', stop_reason: null }, next_review_at: null, review_due: false, status: 'ready', error_code: null, focus: null },
    mastery: { status: 'unassessed', label: '尚未确认掌握', detail: '缺少独立作答证据。' }, review_count: count, review_counts: { original: count, practice: 0, formal: 0 }, last_review_at: count ? '2026-10-13T03:00:00Z' : null, diagnosis: null
  }
}
function snapshot(items = [item('A'), item('B', '英语', 3), item('C', '数学', 2)]): MistakePrintSnapshot { return { version: 1, child_id: child.child_id, generated_at: '2026-11-11T12:00:00Z', total: items.length, items } }
async function ready(): Promise<void> { await screen.findByText('匹配 3 题 · 已选 3 题') }
async function preview(): Promise<HTMLIFrameElement> { fireEvent.click(screen.getByRole('button', { name: '生成打印预览' }));return await screen.findByTitle('错题本打印预览') }
beforeEach(() => {
  vi.mocked(loadMistakePrintSnapshot).mockResolvedValue(snapshot())
  print.mockReset().mockResolvedValue({ status: 'saved' })
  Object.defineProperty(window, 'desktop', { configurable: true, value: { printMistakeBook: print } })
})
afterEach(() => { cleanup();vi.clearAllMocks() })

it('prints only checked matching items, excludes hidden selections, and uses the exact preview HTML', async () => {
  render(<MistakeBookPrint child={child} onClose={() => {}}/>);await ready()
  fireEvent.change(screen.getByLabelText('学科'), { target: { value: '数学' } })
  fireEvent.click(screen.getByRole('checkbox', { name: '选择：数学题A' }))
  expect(screen.getByText('匹配 2 题 · 已选 1 题')).toBeInTheDocument()
  const frame = await preview()
  expect(frame.srcdoc).toContain('数学题C');expect(frame.srcdoc).not.toContain('数学题A');expect(frame.srcdoc).not.toContain('英语题B')
  expect(frame.srcdoc).toContain('复习作答 2 次');expect(frame.srcdoc).not.toContain('原来的作答')
  fireEvent.click(screen.getByRole('button', { name: '保存 PDF' }))
  await screen.findByText('PDF 已保存。')
  expect(print).toHaveBeenCalledWith({ action: 'pdf', title: '示例同学的错题本', html: frame.srcdoc })
})

it('filters mastery, dates and review ranges, invalidates previews after changes, and rejects invalid ranges', async () => {
  const data = snapshot();data.items[2].mastery = { status: 'retest_pending', label: '待复测', detail: '一次答对尚待复测' }
  vi.mocked(loadMistakePrintSnapshot).mockResolvedValue(data)
  render(<MistakeBookPrint child={child} onClose={() => {}}/>);await ready()
  fireEvent.change(screen.getByLabelText('掌握情况'), { target: { value: 'retest_pending' } })
  fireEvent.change(screen.getByLabelText('最少复习次数'), { target: { value: '2' } })
  expect(screen.getByText('匹配 1 题 · 已选 1 题')).toBeInTheDocument();await preview()
  fireEvent.change(screen.getByLabelText('最多复习次数'), { target: { value: '1' } })
  expect(screen.queryByTitle('错题本打印预览')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '生成打印预览' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '重置全部筛选' }))
  fireEvent.change(screen.getByLabelText('日期'), { target: { value: 'custom' } })
  fireEvent.change(screen.getByLabelText('起始日期'), { target: { value: '2026-11-01' } })
  expect(screen.getByText('匹配 0 题 · 已选 0 题')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('结束日期'), { target: { value: '2026-10-01' } })
  expect(screen.getByRole('alert')).toHaveTextContent('起始日期不能晚于结束日期')
})

it('preserves the preview through cancelled, failed and unconfirmed printing and supports retry', async () => {
  render(<MistakeBookPrint child={child} onClose={() => {}}/>);await ready();await preview()
  const send = screen.getByRole('button', { name: '发送到默认打印机' })
  print.mockResolvedValueOnce({ status: 'cancelled' })
  fireEvent.click(send);await screen.findByText('已取消，打印预览仍保留。')
  print.mockResolvedValueOnce({ status: 'failed', error: '打印机离线' })
  fireEvent.click(send);await screen.findByText('打印机离线')
  print.mockResolvedValueOnce({ status: 'unconfirmed' })
  fireEvent.click(send);await screen.findByText('系统尚未确认收到打印任务，请先检查默认打印机的队列，再决定是否重试。')
  const frozenPreview = (screen.getByTitle('错题本打印预览') as HTMLIFrameElement).srcdoc
  expect(screen.getByRole('button', { name: '保存 PDF' })).toBeEnabled()
  print.mockResolvedValueOnce({ status: 'submitted' })
  fireEvent.click(send);await screen.findByText('已提交到默认打印机的队列，请查看出纸情况。')
  expect(print).toHaveBeenLastCalledWith(expect.objectContaining({ action: 'print', html: frozenPreview }))
})

it('filters legacy diagnosis labels and explanations without fabricating a cloud analysis', async () => {
  const data = snapshot();data.items[0].diagnosis = { label: '审题理解', explanation: '遗漏长度单位换算', status: 'candidate' }
  vi.mocked(loadMistakePrintSnapshot).mockResolvedValue(data)
  render(<MistakeBookPrint child={child} onClose={() => {}}/>);await ready()
  fireEvent.change(screen.getByLabelText('错因'), { target: { value: '审题理解' } })
  expect(screen.getByText('匹配 1 题 · 已选 1 题')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox', { name: '搜索错题' }), { target: { value: '长度单位' } })
  fireEvent.click(screen.getByRole('button', { name: '查找' }))
  const frame = await preview();expect(frame.srcdoc).toContain('审题理解（待验证）');expect(frame.srcdoc).toContain('遗漏长度单位换算')
  expect(data.items[0].record.analysis).toBeNull()
})

it('blocks incomplete crops, allows retry, and aborts pending preparation on cancel', async () => {
  const data = snapshot();data.items[0].record.source.has_question_crop = true
  vi.mocked(loadMistakePrintSnapshot).mockResolvedValue(data)
  vi.mocked(loadMistakePrintCrop).mockRejectedValueOnce(Error('crop unavailable'))
  render(<MistakeBookPrint child={child} onClose={() => {}}/>);await ready()
  fireEvent.click(screen.getByRole('button', { name: '生成打印预览' }))
  await screen.findByText(/有题目或原图未能完整读取/)
  expect(screen.queryByTitle('错题本打印预览')).not.toBeInTheDocument();expect(print).not.toHaveBeenCalled()
  let finish: (crop: string) => void = () => {}
  vi.mocked(loadMistakePrintCrop).mockImplementationOnce((_item, signal) => new Promise(resolve => { finish = resolve;expect(signal.aborted).toBe(false) }))
  fireEvent.click(screen.getByRole('button', { name: '生成打印预览' }));fireEvent.click(await screen.findByRole('button', { name: '取消整理' }))
  expect(vi.mocked(loadMistakePrintCrop).mock.calls.at(-1)?.[1].aborted).toBe(true)
  finish('data:image/jpeg;base64,/9j/AAAA');await waitFor(() => expect(screen.queryByTitle('错题本打印预览')).not.toBeInTheDocument())
})

it('renders an honest empty state, no desktop capability, and recoverable load failure', async () => {
  Object.defineProperty(window, 'desktop', { configurable: true, value: undefined })
  vi.mocked(loadMistakePrintSnapshot).mockRejectedValueOnce(Error('offline')).mockResolvedValueOnce(snapshot([]))
  render(<MistakeBookPrint child={child} onClose={() => {}}/>);
  fireEvent.click(await screen.findByRole('button', { name: '重试读取' }))
  await screen.findByText('还没有保存的错题，可以返回错题本录入。')
  expect(screen.getByRole('button', { name: '生成打印预览' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '保存 PDF' })).toBeDisabled()
})

it('preserves the chosen scope on reload, removes deleted records, and does not silently add new questions', async () => {
  render(<MistakeBookPrint child={child} onClose={() => {}}/>);await ready()
  fireEvent.click(screen.getByRole('checkbox', { name: '选择：数学题A' }))
  vi.mocked(loadMistakePrintSnapshot).mockResolvedValueOnce(snapshot([item('C', '数学', 2), item('D')]))
  fireEvent.click(screen.getByRole('button', { name: '重新读取资料' }))
  await screen.findByText('匹配 2 题 · 已选 1 题')
  const frame = await preview();expect(frame.srcdoc).toContain('数学题C');expect(frame.srcdoc).not.toContain('数学题D')
})

it('escapes private source markup, keeps long questions, and refuses cross-child or missing images', () => {
  const entry = item('long');entry.record.source.question = '<script>evil()</script>\n' + '长题内容'.repeat(3000)
  entry.record.analysis = { summary: '', wrong_causes: [{ label: '审题遗漏', explanation: '<img src=x onerror=evil()>' }], knowledge_points: [], correction_strategy: [] }
  const input = { child, items: [entry], generatedAt: snapshot().generated_at, filterSummary: '全部学科', options: { ...defaultPrintOptions, onePerPage: true }, crops: {} }
  const html = buildMistakePrintDocument(input)
  expect(html).not.toContain('<script>');expect(html).toContain('&lt;script&gt;');expect(html).toContain('长题内容'.repeat(3000));expect(html).toContain('single-page');expect(html).toContain('审题遗漏')
  entry.record.source.has_question_crop = true;expect(() => buildMistakePrintDocument(input)).toThrow('图片')
  entry.record.child_id = 'other-child';expect(() => buildMistakePrintDocument(input)).toThrow('当前孩子')
})
