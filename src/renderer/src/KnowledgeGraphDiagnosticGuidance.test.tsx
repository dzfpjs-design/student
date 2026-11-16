import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import { KnowledgeGraphDiagnosticGuidance } from './KnowledgeGraphDiagnosticGuidance'

const approvedPath = {
  child_id: 'child', node_id: 'hidden-node', mastery: { mastery_probability: 0.32, evidence_count: 1 }, next_action: 'explain' as const,
  material_availability: 'available' as const,
  truthfulness: { mapping_status: 'approved_parent_or_product_evidence_only', unreviewed_candidates_hidden: true, media_is_local_only: true },
  materials: [{ title: '判别式本机讲解', preview: '先计算 b²-4ac，再判断根的情况。', license_boundary: 'parent_owned', review_status: 'approved' as const }],
  diagnosis: { prerequisites: [], weaknesses: [], retest_items: [{ title: '判别式独立复测', basis: 'same_knowledge_point' }] }
}

describe('KnowledgeGraphDiagnosticGuidance', () => {
  afterEach(cleanup)

  it('renders only server-approved material and retest suggestions', async () => {
    const load = vi.fn().mockResolvedValue(approvedPath)
    render(<KnowledgeGraphDiagnosticGuidance childId="child" nodeId="hidden-node" load={load} />)
    expect(await screen.findByText('判别式本机讲解')).toBeInTheDocument()
    expect(screen.getByText('判别式独立复测')).toBeInTheDocument()
    expect(screen.getByText(/只显示家长或产品已批准/)).toBeInTheDocument()
    expect(screen.queryByText('hidden-node')).not.toBeInTheDocument()
  })

  it('never falls back to candidates when approval is withdrawn and can refresh after a durable decision', async () => {
    const load = vi.fn()
      .mockRejectedValueOnce(new ApiError('knowledge point is unavailable', 422, 'KNOWLEDGE_POINT_UNAVAILABLE'))
      .mockResolvedValueOnce(approvedPath)
    render(<KnowledgeGraphDiagnosticGuidance childId="child" nodeId="node" load={load} />)
    expect(await screen.findByText(/暂时没有可用的已批准讲解或复测/)).toBeInTheDocument()
    expect(screen.queryByText('判别式本机讲解')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新已批准建议' }))
    await waitFor(() => expect(screen.getByText('判别式本机讲解')).toBeInTheDocument())
    expect(load).toHaveBeenCalledTimes(2)
  })
})
