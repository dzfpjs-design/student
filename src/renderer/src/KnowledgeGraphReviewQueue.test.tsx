import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { KnowledgeGraphReviewQueue } from './KnowledgeGraphReviewQueue'

const queueItem = {
  resource_id: 'internal-resource-uuid', node_id: 'internal-node-uuid', relation_type: 'explains' as const,
  max_confidence: 0.84, evidence_segment_count: 3, knowledge_point_title: '一元二次方程求根公式',
  canonical_key: 'starter:J3.MATH.QUADRATIC.FORMULA', grade_scope: '初三', track: 'school', resource_title: '本地课程 · 求根公式',
  resource_source_ref: 'local-course', mapping_sources: ['keyword-candidate', 'title-match'], mapping_source_count: 2, license_boundary: 'local_only'
}
const recentItem = { ...queueItem, review_key: 'internal-history-key', reviewer: '家长', decision: 'approved' as const, decision_values: ['approved' as const], decided_at: '2026-08-28T00:00:00Z', affected_segment_count: 3 }

describe('KnowledgeGraphReviewQueue', () => {
  afterEach(() => { cleanup(); window.localStorage.clear() })

  it('keeps internal IDs out, shows bounded local evidence, and restores durable undo history', async () => {
    const api = vi.fn(async (path: string) => {
      if (path.includes('resource-review-evidence')) return { items: [{ sequence: 2, source: 'title-match', preview: '求根公式的本机截断证据。' }] }
      if (path.includes('resource-review-history')) return { items: [recentItem] }
      return { items: [queueItem], summary: { pending_resource_review_items: 7, pending_resources: 4, pending_evidence_segments: 12, review_items_per_resource_p95: 3 } }
    })
    render(<KnowledgeGraphReviewQueue reviewer="家长" api={api} />)
    expect((await screen.findAllByText('一元二次方程求根公式')).length).toBe(2)
    expect(screen.getByText('候选来源（2 类）：keyword-candidate、title-match；素材来源：local-course；使用边界：local_only')).toBeInTheDocument()
    expect(screen.getByText('待审核 7 项资源级映射，覆盖 4 份资源、12 个证据片段。本页最多显示 50 项；审核后会继续加载。')).toBeInTheDocument()
    expect(screen.queryByText('internal-resource-uuid')).not.toBeInTheDocument()
    expect(screen.queryByText('internal-node-uuid')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看本机证据' }))
    expect(await screen.findByText('求根公式的本机截断证据。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '撤销此决定' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '撤销此决定' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/v1/parent/knowledge-graph/resource-mapping-reviews/undo', expect.objectContaining({ method: 'POST' })))
  })

  it('reuses a persisted command id after a lost response and only clears it after success', async () => {
    let postAttempts = 0
    const actionIds: string[] = []
    const api = vi.fn(async (path: string, init?: RequestInit) => {
      if (path === '/v1/parent/knowledge-graph/resource-review-queue') return { items: [queueItem] }
      if (path === '/v1/parent/knowledge-graph/resource-review-history') return { items: [] }
      if (path === '/v1/parent/knowledge-graph/resource-mapping-reviews') {
        actionIds.push(JSON.parse(String(init?.body)).client_action_id); postAttempts += 1
        if (postAttempts === 1) throw new Error('response lost')
        return { review: { statuses: { approved: 3 } } }
      }
      return { items: [] }
    })
    const first = render(<KnowledgeGraphReviewQueue reviewer="家长" api={api} />)
    await screen.findByText('一元二次方程求根公式')
    fireEvent.click(screen.getByRole('button', { name: '接受' }))
    await screen.findByText('保存未确认；会保留本次命令，重试不会重复写入。')
    first.unmount()
    render(<KnowledgeGraphReviewQueue reviewer="家长" api={api} />)
    await screen.findByText('一元二次方程求根公式')
    fireEvent.click(screen.getByRole('button', { name: '接受' }))
    await waitFor(() => expect(actionIds).toHaveLength(2))
    expect(actionIds[1]).toBe(actionIds[0])
    await waitFor(() => expect(screen.queryByText('保存未确认；会保留本次命令，重试不会重复写入。')).not.toBeInTheDocument())
  })

  it('keeps evidence local, bounded, and retryable after a preview load failure', async () => {
    let evidenceAttempts = 0
    const api = vi.fn(async (path: string) => {
      if (path.includes('resource-review-history')) return { items: [] }
      if (path.includes('resource-review-evidence')) {
        evidenceAttempts += 1
        if (evidenceAttempts === 1) throw new Error('temporary local API failure')
        return { items: [{ sequence: 1, source: '本机候选映射', preview: '已截断的本机证据。' }] }
      }
      return { items: [queueItem] }
    })
    render(<KnowledgeGraphReviewQueue reviewer="家长" api={api} />)
    await screen.findByText('一元二次方程求根公式')
    fireEvent.click(screen.getByRole('button', { name: '查看本机证据' }))
    await screen.findByText('本机证据暂时无法加载，可稍后重试。')
    fireEvent.click(screen.getByRole('button', { name: '查看本机证据' }))
    expect(await screen.findByText('已截断的本机证据。')).toBeInTheDocument()
    expect(evidenceAttempts).toBe(2)
  })

  it('labels legacy mixed segment decisions as one destructive resource-level undo', async () => {
    const api = vi.fn(async (path: string) => {
      if (path.includes('resource-review-history')) return { items: [{ ...recentItem, decision: 'mixed_legacy', decision_values: ['approved', 'rejected'] }] }
      return { items: [], summary: { pending_resource_review_items: 0, pending_resources: 0, pending_evidence_segments: 0, review_items_per_resource_p95: 0 } }
    })
    render(<KnowledgeGraphReviewQueue reviewer="家长" api={api} />)
    expect(await screen.findByText(/混合旧决定（已接受、已拒绝）/)).toBeInTheDocument()
    expect(screen.getByText('撤销会移除该资源全部旧片段决定，并恢复为待审核。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '撤销全部旧决定' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '撤销此决定' })).not.toBeInTheDocument()
  })
})
