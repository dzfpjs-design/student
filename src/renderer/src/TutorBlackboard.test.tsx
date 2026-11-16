import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { TutorBlackboard, tutorBlackboardFromTurn, tutorExampleNotice, tutorSegmentStartDelays } from './TutorBlackboard'
import type { TutorBlackboardPresentation, TutorTurn } from './types'

const presentation: TutorBlackboardPresentation = {
  version: 'tutor_blackboard_v1', source: 'model_structured', sync_mode: 'segment_estimate', subject: '数学', title: '判别式',
  segments: [
    { segment_id: 's1', narration: '先看判别式。', duration_hint_ms: 2000, blocks: [{ kind: 'formula', latex: '\\Delta=b^2-4ac', text: '判别式', tone: 'cyan' }] },
    { segment_id: 's2', narration: '再判断符号。', duration_hint_ms: 3000, blocks: [{ kind: 'relation', nodes: ['大于0', '两个实数根'], tone: 'yellow' }] },
  ],
}

it('reveals only the board segments reached by the voice timeline', () => {
  render(<TutorBlackboard presentation={presentation} revealedSegments={1} />)
  expect(screen.getByLabelText('数学讲解黑板')).toBeInTheDocument()
  expect(screen.getAllByText('判别式')).toHaveLength(2)
  expect(screen.queryByText('两个实数根')).not.toBeInTheDocument()
})

it('builds stable cumulative segment start times', () => {
  expect(tutorSegmentStartDelays(presentation)).toEqual([0, 2000])
})

it('reads the persisted board from the tutor output envelope', () => {
  const turn = { output_envelope: { result: { blackboard: presentation } } } as unknown as TutorTurn
  expect(tutorBlackboardFromTurn(turn)?.title).toBe('判别式')
  expect(tutorBlackboardFromTurn({ output_envelope: { result: {} } } as unknown as TutorTurn)).toBeNull()
})

it('tells the child when a structured board was not available', () => {
  const { rerender } = render(<TutorBlackboard presentation={{ ...presentation, source: 'local_degraded', degraded_reason: 'invalid_schema' }} revealedSegments={1} />)
  expect(screen.getByRole('status')).toHaveTextContent('板书暂时没有整理好')
  rerender(<TutorBlackboard presentation={presentation} revealedSegments={1} />)
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
})

it('shows example status only for the board belonging to that turn', () => {
  const turn = { turn_id: 'turn-a', output_envelope: { example_quality: { status: 'missing' } } } as unknown as TutorTurn
  expect(tutorExampleNotice(turn, 'turn-a')).toContain('没有找到合适的例题')
  expect(tutorExampleNotice(turn, 'turn-b')).toBeNull()
  expect(tutorExampleNotice({ ...turn, output_envelope: { ...turn.output_envelope!, example_quality: { status: 'unavailable' } } }, 'turn-a')).toContain('没能核对')
})
