import type { AgentTurn, TutorBlackboardPresentation, TutorTurn } from './types'

export type LocalLearningContext = NonNullable<AgentTurn['action']['local_learning_context']>

export function hasProtectedCourseContent(course: LocalLearningContext['course_context']): boolean {
  return course?.cloud_transmission_allowed === false
    && (Boolean(course.diagnostic_prompt?.trim()) || Boolean(course.evidence?.length))
}

export function appendLocalDiagnosticFollowUp(
  turn: TutorTurn,
  context: LocalLearningContext | undefined,
): TutorTurn {
  // This lesson already owns an exact image-bound follow-up and replay marker.
  const originalBoard = turn.output_envelope?.result?.blackboard
  if (isBlackboard(originalBoard) && originalBoard.source === 'visual_teaching_v1') return turn
  if (!context) return turn
  const course = context.course_context?.cloud_transmission_allowed === false
    ? context.course_context : undefined
  const prompt = [context.diagnostic_prompt?.trim(), course?.diagnostic_prompt?.trim()].filter(Boolean).join('\n')
  if (!prompt || context.cloud_transmission_allowed !== false) return turn

  const answer = `${turn.answer.trim()}\n${prompt}`
  const candidate = turn.output_envelope?.result?.blackboard
  if (!isBlackboard(candidate)) return { ...turn, answer }

  const presentation: TutorBlackboardPresentation = {
    ...candidate,
    source: 'model_plus_local_diagnostic',
    segments: [
      ...candidate.segments,
      ...(course?.evidence ?? []).map((evidence) => ({
        segment_id: `local-course-${evidence.segment_id}`,
        narration: '这里是本机课程中的相关片段，我们先核对它和你正在学习的内容是否一致。',
        duration_hint_ms: 9_000,
        blocks: [
          { kind: 'heading' as const, text: evidence.title, tone: 'green' as const },
          { kind: 'text' as const, text: evidence.excerpt, tone: 'chalk' as const },
          { kind: 'text' as const, text: '课程原文参考，匹配关系待核对', tone: 'yellow' as const },
        ],
      })),
      {
        segment_id: `local-diagnostic-${context.audit_event_id ?? turn.turn_id}`,
        narration: prompt,
        duration_hint_ms: Math.max(1_800, Math.min(12_000, prompt.length * 235)),
        blocks: [
          { kind: 'heading', text: '理解检查', tone: 'green' },
          { kind: 'question', text: prompt, tone: 'yellow' },
        ],
      },
    ],
  }
  return {
    ...turn,
    answer,
    output_envelope: {
      ...turn.output_envelope!,
      result: { ...turn.output_envelope!.result, blackboard: presentation },
    },
  }
}

function isBlackboard(value: unknown): value is TutorBlackboardPresentation {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<TutorBlackboardPresentation>
  return candidate.version === 'tutor_blackboard_v1' && Array.isArray(candidate.segments)
}
