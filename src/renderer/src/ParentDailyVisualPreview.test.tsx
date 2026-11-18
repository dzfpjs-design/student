import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as api from './api'
import { ParentDailyVisualPreview } from './ParentDailyVisualPreview'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })
const payload = (child: string, day: string, pages: unknown[] = []) => ({
  child_id: child, learning_date: day, local_only: true, is_formal_report: false,
  document_observation: { pages }, page_observation_time: { status: 'no_segments', pages: [] }
})

it('shows OCR and low confidence with truthful time and email boundaries', async () => {
  vi.spyOn(api, 'requestParentLocal').mockImplementation(async (path) => {
    if (path.endsWith('/session')) return { expires_at: new Date(Date.now()+60000).toISOString() } as never
    const day = path.split('/').pop()!
    return payload('child-a', day, [{ text: '1. 求 2+2', observations: [{ revision_id: 'r', confidence: .42, review_required: true }] }]) as never
  })
  render(<ParentDailyVisualPreview childId="child-a" />)
  fireEvent.click(screen.getByRole('button', { name: '查看记录' }))
  expect(await screen.findByText('1. 求 2+2')).toBeTruthy()
  expect(screen.getByText(/置信度较低/)).toBeTruthy()
  expect(screen.getByText('识别置信度：42%。')).toBeTruthy()
  expect(screen.getByText(/没有发送邮件/)).toBeTruthy()
  expect(screen.getByText(/没有足够的页面可见时间记录/)).toBeTruthy()
})

it('labels cloud page text as an unconfirmed candidate without assigning local OCR confidence to it', async () => {
  vi.spyOn(api, 'requestParentLocal').mockImplementation(async path => path.endsWith('/session')
    ? { expires_at: new Date(Date.now()+60000).toISOString() } as never
    : payload('child-a', path.split('/').pop()!, [{ text: '世界的气候', source: 'cloud_page', observations: [{ revision_id: 'r', confidence: .1, review_required: true }] }]) as never)
  render(<ParentDailyVisualPreview childId="child-a" />)
  fireEvent.click(screen.getByRole('button', { name: '查看记录' }))
  expect(await screen.findByText('世界的气候')).toBeTruthy()
  expect(screen.getByText(/云端页面识别候选，尚未核对/)).toBeTruthy()
  expect(screen.queryByText(/识别置信度：/)).toBeNull()
})

it('shows recorded task time separately from reported minutes and attention', async () => {
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => path.endsWith('/session')
    ? {expires_at:new Date(Date.now()+60000).toISOString()} as never
    : {...payload('child-a',path.split('/').pop()!), recorded_task_time: {tasks: [{subject:'数学',title:'分数练习',recorded_active_seconds:120,reported_minutes:5}],omitted_count:1}} as never)
  render(<ParentDailyVisualPreview childId="child-a" />)
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  expect(await screen.findByText(/数学：分数练习；计时 2.0 分钟；填写 5 分钟/)).toBeTruthy()
  expect(screen.getByText(/1 项时间跨日或记录不完整/)).toBeTruthy()
  expect(screen.getByText('任务计时和填写时长不等于实际专注或学习时长。')).toBeTruthy()
})

it('describes camera samples cautiously without claiming a continuous behavior', async () => {
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => path.endsWith('/session')
    ? {expires_at:new Date(Date.now()+60000).toISOString()} as never
    : {...payload('child-a',path.split('/').pop()!),camera_and_document_samples:{sources:{camera:{eligible_states:{writing_like:2,away:1}}}}} as never)
  render(<ParentDailyVisualPreview childId="child-a" />)
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  expect(await screen.findByText('画面出现变化，原因不确定：2 次')).toBeTruthy()
  expect(screen.getByText('未检测到正面人脸，也可能是遮挡：1 次')).toBeTruthy()
  expect(screen.getByText('这些样本不能判断是否做题、背书或持续专注。')).toBeTruthy()
})

it('does not display a stale result after changing date', async () => {
  let resolve!: (value: unknown) => void
  vi.spyOn(api, 'requestParentLocal').mockImplementation(() => new Promise(done => { resolve = done }) as never)
  render(<ParentDailyVisualPreview childId="child-a" />)
  const input = screen.getByLabelText('学习记录日期') as HTMLInputElement
  const oldDay = input.value
  fireEvent.click(screen.getByRole('button', { name: '查看记录' }))
  fireEvent.change(input, { target: { value: '2026-01-01' } })
  resolve(payload('child-a', oldDay, [{ text: '旧页面', observations: [] }]))
  await waitFor(() => expect(screen.queryByText('旧页面')).toBeNull())
})

it('shows empty and error states and rejects a different child', async () => {
  const request = vi.spyOn(api, 'requestParentLocal')
  request.mockImplementationOnce(async (path) => payload('child-a', path.split('/').pop()!) as never)
  request.mockResolvedValueOnce({expires_at:new Date(Date.now()+60000).toISOString()} as never)
  request.mockRejectedValueOnce(new Error('offline'))
  request.mockImplementationOnce(async (path) => payload('child-b', path.split('/').pop()!) as never)
  render(<ParentDailyVisualPreview childId="child-a" />)
  const button = screen.getByRole('button', { name: '查看记录' })
  fireEvent.click(button)
  expect(await screen.findByText(/没有可核实的高拍页面/)).toBeTruthy()
  fireEvent.click(button)
  expect(await screen.findByText(/记录暂时无法读取/)).toBeTruthy()
  fireEvent.click(button)
  expect(await screen.findByText(/记录暂时无法读取/)).toBeTruthy()
})

it('clears prior child data when selecting a different child', async () => {
  vi.spyOn(api, 'requestParentLocal').mockImplementation(async (path) => path.endsWith('/session') ? {expires_at:new Date(Date.now()+60000).toISOString()} as never : payload('child-a', path.split('/').pop()!, [{ text: '只属于甲的页面', observations: [] }]) as never)
  const view = render(<ParentDailyVisualPreview childId="child-a" />)
  fireEvent.click(screen.getByRole('button', { name: '查看记录' }))
  expect(await screen.findByText('只属于甲的页面')).toBeTruthy()
  view.rerender(<ParentDailyVisualPreview childId="child-b" />)
  expect(screen.queryByText('只属于甲的页面')).toBeNull()
})

it('removes rendered data at the server session expiry without extending it', async () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-11-04T04:00:00Z'))
  const expiry = new Date(Date.now()+12000).toISOString()
  vi.spyOn(api,'requestParentLocal').mockImplementation(async path => path.endsWith('/session') ? {expires_at:expiry} as never : payload('child-a',path.split('/').pop()!, [{text:'页面',observations:[]}]) as never)
  render(<ParentDailyVisualPreview childId="child-a" />)
  await act(async()=>{fireEvent.click(screen.getByRole('button',{name:'查看记录'})); await Promise.resolve(); await Promise.resolve()})
  expect(screen.getByText('页面')).toBeTruthy()
  await act(async()=>{await vi.advanceTimersByTimeAsync(12000)})
  expect(screen.queryByText('页面')).toBeNull()
})

it('explains an unconfirmed task source without exposing internal codes', async () => {
  vi.spyOn(api, 'requestParentLocal').mockRejectedValue(new api.ApiError('REPORT_TASK_SOURCE_UNCONFIRMED',409))
  render(<ParentDailyVisualPreview childId="child-a" />)
  fireEvent.click(screen.getByRole('button',{name:'查看记录'}))
  expect(await screen.findByText(/作业或完成记录的来源尚未核对/)).toBeTruthy()
  expect(screen.queryByText(/REPORT_TASK_SOURCE_UNCONFIRMED/)).toBeNull()
})
