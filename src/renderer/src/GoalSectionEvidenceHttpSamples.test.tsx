import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { GoalRoadmapPanel } from './GoalRoadmapPanel'
import type { GoalRoadmap } from './GoalRoadmapPanel.types'
import { localApi, resetRuntimeCache } from './api'
import published from './test/fixtures/goal-section-http/published_reference.json'
import reviewed from './test/fixtures/goal-section-http/reviewed.json'
import restarted from './test/fixtures/goal-section-http/restarted.json'
import withdrawn from './test/fixtures/goal-section-http/withdrawn.json'
import answerPending from './test/fixtures/goal-section-http/answer_pending_review.json'
import reviewRetracted from './test/fixtures/goal-section-http/review_retracted.json'
import retractionRestarted from './test/fixtures/goal-section-http/review_retracted_restart.json'

// Original safe HTTP bodies from real create_app / SQLite / original-audio review.
// Transport is stubbed here; this file verifies their actual renderer consumption.
type Sample = typeof reviewed
const child = published.child_id
let current: Sample = published
let status = 200
const requests: Array<{ url: string; init?: RequestInit }> = []

function routeBase(sample: Sample = published): GoalRoadmap {
  return {
    ...sample, section_evidence: undefined, title: 'PET 能力路线', target_date: null, status: 'needs_clarification',
    prerequisite_path: [], risks: [], clarifications: [], next_daily_action: null,
    burden: { configured_budget_valid: false, feasibility: 'unknown', estimated_minutes: { min: 0, max: 0 }, unallocated_minutes: 0 },
    milestones: { monthly: [], weekly: [] },
  } as unknown as GoalRoadmap
}
function speaking(): HTMLElement {
  return screen.getByRole('heading', { name: '口语' }).closest('li')!
}
function expectCount(label: string, value: number, unit = '次') {
  const metric = within(speaking()).getByText(label).closest('div')!
  expect(metric.querySelector('dd')?.textContent).toMatch(new RegExp(`^${value} ${unit}`))
}

beforeEach(async () => {
  current = published; status = 200; requests.length = 0; resetRuntimeCache()
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    requests.push({ url: String(input), init })
    if (String(input).endsWith('/v1/parent/unlock')) return new Response(JSON.stringify({ parent_token: 'synthetic-parent-token' }))
    return new Response(JSON.stringify(status === 200 ? current : { detail: 'Not available' }), { status })
  }))
  await localApi.parentUnlock('7391', false)
  requests.length = 0
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); resetRuntimeCache() })

describe('actual HTTP section samples through the existing parent API adapter', () => {
  it('re-reads qualification when the parent refreshes the same source and rule version', async () => {
    current = reviewed
    const original = routeBase(reviewed)
    const mounted = render(<GoalRoadmapPanel childId={child} roadmap={original} />)
    await waitFor(() => expectCount('可靠评分', 1))
    mounted.rerender(<GoalRoadmapPanel childId={child} roadmap={original} />)
    expect(requests).toHaveLength(1)
    expect(reviewRetracted.source_version).toBe(reviewed.source_version)
    current = reviewRetracted
    // Actual cycles GET returns a new roadmap object, while source/rule identity stays fixed.
    mounted.rerender(<GoalRoadmapPanel childId={child} roadmap={routeBase(reviewed)} />)
    await waitFor(() => expectCount('可靠评分', 0))
    expectCount('实际作答', 1); expectCount('独立正确', 0)
    expect(requests).toHaveLength(2)
    expect(requests[0].init?.signal?.aborted).toBe(true)
  })

  it('hides old counts during same-version parent refresh and rejects its late response', async () => {
    current = reviewed
    const mounted = render(<GoalRoadmapPanel childId={child} roadmap={routeBase(reviewed)} />)
    await waitFor(() => expectCount('可靠评分', 1))
    let release!: (response: Response) => void
    vi.mocked(fetch).mockImplementationOnce(() => new Promise<Response>(resolve => { release = resolve }))
    mounted.rerender(<GoalRoadmapPanel childId={child} roadmap={routeBase(reviewed)} />)
    expect(screen.queryByRole('heading', { name: '口语' })).not.toBeInTheDocument()
    expect(screen.getByText('正在读取当前练习证据')).toBeInTheDocument()
    // Establish an in-flight old read before replacing its owner. A cancelled
    // pre-send request correctly cannot consume the deferred transport fixture.
    await waitFor(() => expect(release).toBeTypeOf('function'))
    current = reviewRetracted
    mounted.rerender(<GoalRoadmapPanel childId={child} roadmap={routeBase(reviewed)} />)
    await waitFor(() => expectCount('可靠评分', 0))
    await act(async () => { release(new Response(JSON.stringify(reviewed))) })
    expectCount('可靠评分', 0); expectCount('独立正确', 0)
  })

  it('shows source publication, actual answer, human review, retraction and both same-database restarts without creating any write request', async () => {
    let mounted = render(<GoalRoadmapPanel childId={child} roadmap={routeBase()} />)
    await screen.findByRole('heading', { name: '口语' })
    expectCount('可用练习题源', 8, '道'); expectCount('实际作答', 0); expectCount('可靠评分', 0)
    current = answerPending
    fireEvent.click(screen.getByRole('button', { name: '刷新练习证据' }))
    expect(await screen.findByText('路线已更新，需重新读取完整路线。')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: '口语' })).not.toBeInTheDocument()
    // This actual first answer changed the original roadmap source version.
    // The parent's fresh roadmap must arrive before showing evidence under it.
    mounted.rerender(<GoalRoadmapPanel childId={child} roadmap={routeBase(answerPending)} />)
    await waitFor(() => expectCount('实际作答', 1))
    expectCount('可靠评分', 0); expectCount('独立正确', 0)
    current = reviewed
    fireEvent.click(screen.getByRole('button', { name: '刷新练习证据' }))
    await waitFor(() => expectCount('可靠评分', 1))
    expectCount('独立正确', 1)
    mounted.unmount()
    current = restarted
    mounted = render(<GoalRoadmapPanel childId={child} roadmap={routeBase(restarted)} />)
    await waitFor(() => expectCount('可靠评分', 1))
    current = reviewRetracted
    fireEvent.click(screen.getByRole('button', { name: '刷新练习证据' }))
    await waitFor(() => expectCount('可靠评分', 0))
    expectCount('实际作答', 1); expectCount('独立正确', 0); expectCount('可用练习题源', 8, '道')
    mounted.unmount()
    current = retractionRestarted
    mounted = render(<GoalRoadmapPanel childId={child} roadmap={routeBase(retractionRestarted)} />)
    await waitFor(() => expectCount('实际作答', 1))
    expectCount('可靠评分', 0); expectCount('独立正确', 0)
    current = withdrawn
    fireEvent.click(screen.getByRole('button', { name: '刷新练习证据' }))
    await waitFor(() => expectCount('可靠评分', 0))
    expectCount('实际作答', 0); expectCount('可用练习题源', 7, '道')
    expect(screen.getByText(/另有 1 次历史作答/)).toBeInTheDocument()
    for (const request of requests) {
      expect(request.url).toBe(`http://127.0.0.1:8765/v1/parent/children/${child}/goal-programs/pet_path/roadmap`)
      expect(request.init?.method ?? 'GET').toBe('GET')
      expect(request.init?.cache).toBe('no-store')
      expect(request.init?.headers).toMatchObject({ 'X-HSC-Parent': 'synthetic-parent-token' })
    }
    expect(requests).toHaveLength(8)
  })

  it('never renders private source/answer identities from the real sample', async () => {
    current = reviewed
    render(<GoalRoadmapPanel childId={child} roadmap={routeBase(current)} />)
    await screen.findByRole('heading', { name: '口语' })
    const visible = screen.getByRole('region', { name: '练习与评分证据' }).cloneNode(true) as HTMLElement
    visible.querySelectorAll('style').forEach((style) => style.remove())
    for (const section of reviewed.section_evidence.sections) {
      for (const source of section.sources) {
        expect(visible.textContent).not.toContain(source.diagnostic_item_id)
        expect(visible.textContent).not.toContain(source.source_version)
      }
      for (const receipt of section.qualified_evidence) {
        expect(visible.textContent).not.toContain(receipt.question_identity)
        expect(visible.textContent).not.toContain(receipt.source_evidence_id)
      }
    }
    expect(within(visible).getAllByText('官方题型覆盖尚未核验')).toHaveLength(4)
  })

  it.each([401, 404, 500])('failed current read %i removes old counts without an automatic write or retry', async (code) => {
    current = reviewed
    render(<GoalRoadmapPanel childId={child} roadmap={routeBase(current)} />)
    await waitFor(() => expectCount('可靠评分', 1))
    status = code
    fireEvent.click(screen.getByRole('button', { name: '刷新练习证据' }))
    await act(async () => {})
    expect(screen.queryByRole('heading', { name: '口语' })).not.toBeInTheDocument()
    expect(screen.getByText('练习证据尚未核实，请重新读取路线。')).toBeInTheDocument()
    expect(requests).toHaveLength(2)
  })

  it('hides the same sample immediately on another child and rejects a misrouted same-child goal', async () => {
    current = reviewed
    const { rerender } = render(<GoalRoadmapPanel childId={child} roadmap={routeBase(current)} />)
    await screen.findByRole('heading', { name: '口语' })
    rerender(<GoalRoadmapPanel childId="child-junior-9" roadmap={{ ...routeBase(), child_id: 'child-junior-9' }} />)
    expect(screen.queryByRole('heading', { name: '口语' })).not.toBeInTheDocument()
    await act(async () => {})
    expect(screen.queryByRole('heading', { name: '口语' })).not.toBeInTheDocument()
    rerender(<GoalRoadmapPanel childId={child} roadmap={{ ...routeBase(), program_id: 'different-goal' }} />)
    await act(async () => {})
    expect(screen.queryByRole('heading', { name: '口语' })).not.toBeInTheDocument()
  })
})
