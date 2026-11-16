import { describe, expect, it } from 'vitest'
import { appendLocalDiagnosticFollowUp, hasProtectedCourseContent } from './localAtomicTutorContext'
import type { TutorTurn } from './types'

const baseTurn = (): TutorTurn => ({
  turn_id: 'tutor-1', session_id: 'session-1', child_id: 'child-1', plan_item_id: null,
  subject: '数学', question: '小数是什么？', answer: '先看小数的位值。', provider: 'cloud', model: 'tutor',
  status: 'completed', hint_level: 1, parent_turn_id: null, response_kind: 'hint',
  verification_status: 'not_applicable_hint', safety_status: 'passed',
  strategy_experiment_id: null, strategy_version: null, created_at: '2026-08-31T00:00:00Z',
  output_envelope: {
    request_id: 'request-1', output_type: 'tutoring', result: { blackboard: {
      version: 'tutor_blackboard_v1', source: 'model_structured', sync_mode: 'segment_estimate',
      subject: '数学', title: '小数的意义', segments: [{
        segment_id: 's1', narration: '先看小数的位值。', duration_hint_ms: 2000,
        blocks: [{ kind: 'text', text: '小数的位值', tone: 'cyan' }],
      }],
    } }, reason_summary: '提示', confidence: .7, evidence_refs: [], model_provider: 'cloud',
    model_version: 'tutor', prompt_version: 'v1', knowledge_version: null, policy_version: 'v1',
    verification_status: 'partially_verified', safety_status: 'passed', fallback_used: false,
  },
})

describe('local atomic tutoring follow-up', () => {
  it('retains the exact visual lesson and its own check question across local enrichment', () => {
    const turn = baseTurn()
    ;(turn.output_envelope!.result.blackboard as { source: string }).source = 'visual_teaching_v1'
    turn.output_envelope!.result.local_visual = { version: 'visual_teaching_v1' }
    expect(appendLocalDiagnosticFollowUp(turn, {
      status: 'unavailable', cloud_transmission_allowed: false, mastery_update_allowed: false,
      audit_event_id: 'other-topic', matched_node_count: 0, approved_node_count: 0,
      hypothesis_count: 0, diagnostic_prompt: '讲另一道题。',
    })).toBe(turn)
  })
  it('does not force local speech for empty or unavailable course context, including recovery', () => {
    expect(hasProtectedCourseContent(undefined)).toBe(false)
    for (const status of ['no_match', 'unavailable'] as const) {
      const course = { status, cloud_transmission_allowed: false as const, mastery_update_allowed: false as const,
        diagnostic_prompt: null, evidence: [] }
      expect(hasProtectedCourseContent(course)).toBe(false)
      expect(hasProtectedCourseContent(JSON.parse(JSON.stringify(course)))).toBe(false)
      expect(hasProtectedCourseContent({ ...course, diagnostic_prompt: '本机诊断追问' })).toBe(true)
    }
  })
  it('adds the local-only diagnostic to voice and blackboard without changing the tutor request', () => {
    const prompt = '讲完后我再确认一个小点：你能用自己的话说说“十进制位值”吗？'
    const augmented = appendLocalDiagnosticFollowUp(baseTurn(), {
      status: 'matched', cloud_transmission_allowed: false, mastery_update_allowed: false,
      audit_event_id: 'atomic-context-1', matched_node_count: 2, approved_node_count: 0,
      hypothesis_count: 1, diagnostic_prompt: prompt,
    })
    expect(augmented.answer).toContain(prompt)
    const board = augmented.output_envelope?.result.blackboard as { source: string; segments: Array<{ narration: string }> }
    expect(board.source).toBe('model_plus_local_diagnostic')
    expect(board.segments.at(-1)?.narration).toBe(prompt)
    expect(augmented.question).toBe('小数是什么？')
  })

  it('does nothing when no deterministic prompt exists', () => {
    const turn = baseTurn()
    expect(appendLocalDiagnosticFollowUp(turn, undefined)).toBe(turn)
  })

  it('keeps curriculum quotes on the local board and out of the cloud tutor answer', () => {
    const turn = baseTurn()
    const quote = '这是一段仅限本机的课程逐字证据。'
    const result = appendLocalDiagnosticFollowUp(turn, {
      status: 'unavailable', cloud_transmission_allowed: false, mastery_update_allowed: false,
      audit_event_id: null, matched_node_count: 0, approved_node_count: 0,
      hypothesis_count: 0, diagnostic_prompt: null,
      course_context: {
        status: 'matched', cloud_transmission_allowed: false, mastery_update_allowed: false,
        diagnostic_prompt: '这和你今天学习的内容一致吗？', audit_event_id: 'course-event',
        evidence: [{ segment_id: 'segment', resource_id: 'resource', title: '课程', subject: '数学',
          grade: '四年级', page_number: null, source_ref: 'local:course', excerpt: quote,
          excerpt_start_char: 0, review_status: 'restricted', usage: 'source_comparison_not_verified_curriculum_alignment' }],
      },
    })
    expect(JSON.stringify(result.output_envelope?.result.blackboard)).toContain(quote)
    expect(result.answer).not.toContain(quote)
    expect(result.question).toBe(turn.question)
    expect(result.answer).not.toBe(turn.answer)
    expect(JSON.stringify(turn)).not.toContain(quote)
    expect(hasProtectedCourseContent({ status: 'matched', cloud_transmission_allowed: false,
      mastery_update_allowed: false, diagnostic_prompt: null, audit_event_id: 'event',
      evidence: [{ segment_id: 'segment', resource_id: 'resource', title: '课程', subject: '数学',
        grade: '四年级', page_number: null, source_ref: 'local:course', excerpt: quote,
        excerpt_start_char: 0, review_status: 'restricted', usage: 'source_comparison_not_verified_curriculum_alignment' }],
    })).toBe(true)
  })
})
