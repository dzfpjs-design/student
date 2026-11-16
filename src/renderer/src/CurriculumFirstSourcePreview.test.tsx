import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { CurriculumFirstSourcePreview, parentMaterialsReady } from './CurriculumFirstSourcePreview'
import type { ParentCurrentMaterials } from './curriculumSupplyTypes'

afterEach(cleanup)
const source = { schema: 'reviewed_open_response_source_v1', subject: '数学', grade: '初三',
  knowledge_node_id: 'node-j3-math-discriminant', answer_type: 'open_response',
  assessment_mode: 'open_response_rubric', task_family: 'mathematical_process', response_mode: 'written',
  context_requirement: 'self_contained', prompt: '说明这个方程的实根情况，并给出判别依据。',
  task_points: ['写出判别式并代入题中条件。', '说明符号与实根情况的关系。'] }

describe('parent readable self-contained open original', () => {
  it('shows the complete original and task points without JSON or internal identifiers', () => {
    const text = JSON.stringify(source)
    const view = render(<CurriculumFirstSourcePreview text={text} childId="child" />)
    expect(screen.getByText(source.prompt)).toBeTruthy()
    for (const point of source.task_points) expect(screen.getByText(point)).toBeTruthy()
    expect(view.container.textContent).not.toContain('knowledge_node_id')
    expect(view.container.textContent).not.toContain('mathematical_process')
    expect(view.container.textContent).not.toContain(text)
    expect(screen.getByText(/不代表孩子已经理解或掌握/)).toBeTruthy()
  })
  it.each([{ points: [] }, { points: [''] }, { points: [42] }])('explains incomplete task points rather than showing raw source: %j', ({ points }) => {
    const text = JSON.stringify({ ...source, task_points: points })
    const view = render(<CurriculumFirstSourcePreview text={text} childId="child" />)
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(view.container.textContent).not.toContain(text)
  })
  it('does not invent an external material for the self-contained schema', () => {
    render(<CurriculumFirstSourcePreview text={JSON.stringify({ ...source, context_requirement: 'external_material' })} childId="child" />)
    expect(screen.getByRole('alert')).toBeTruthy()
  })
})

describe('parent current bound text materials', () => {
  const text = JSON.stringify({ ...source, schema: 'reviewed_open_response_source_v2', context_requirement: 'external_material',
    material_semantics: 'text_only', material_bindings: [{ material_key: 'given', source_id: 'hidden-source-id' }, { material_key: 'comparison' }] })
  const ready: ParentCurrentMaterials = { status: 'ready', reason: null, local_only: true, cloud_transmission_allowed: false,
    mastery_claim_allowed: false, materials: [
      { material_key: 'given', title: '本题给定条件', version_label: '第一版', heading: '完整条件', text: 'a=1，b=2，c=5；仅讨论实数范围。' },
      { material_key: 'comparison', title: '对照记录', version_label: '第二版', heading: null, text: '甲为十欧姆，乙为二十欧姆。这是题目给定记录。' },
    ] }
  it('shows every complete current material beside the original, without internal references or JSON', () => {
    const view = render(<CurriculumFirstSourcePreview text={text} materials={ready} childId="child" />)
    expect(parentMaterialsReady(text, ready)).toBe(true)
    expect(screen.getByRole('region', { name: '原题材料 1' })).toHaveTextContent(ready.materials[0].text)
    expect(screen.getByRole('region', { name: '原题材料 2' })).toHaveTextContent(ready.materials[1].text)
    expect(screen.getByText('本题给定条件 · 第一版')).toBeTruthy()
    expect(view.container.textContent).not.toContain('hidden-source-id')
    expect(view.container.textContent).not.toContain(text)
  })
  it.each(['missing', 'blocked', 'reordered', 'partial'])('does not present incomplete/stale text as ready: %s', fault => {
    let value: ParentCurrentMaterials | undefined = structuredClone(ready)
    if (fault === 'missing') value = undefined
    else if (fault === 'blocked') value.status = 'blocked'
    else if (fault === 'reordered') value.materials.reverse()
    else value.materials.pop()
    expect(parentMaterialsReady(text, value)).toBe(false)
    render(<CurriculumFirstSourcePreview text={text} materials={value} childId="child" />)
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.queryByText(ready.materials[0].text)).toBeNull()
  })
  it('removes the previous child/material text immediately on changed projection', () => {
    const view = render(<CurriculumFirstSourcePreview text={text} materials={ready} childId="child" />)
    view.rerender(<CurriculumFirstSourcePreview text={text} materials={{ ...ready, status: 'blocked', materials: [] }} childId="other" />)
    expect(screen.queryByText(ready.materials[0].text)).toBeNull()
    expect(screen.getByRole('alert')).toBeTruthy()
  })
})
