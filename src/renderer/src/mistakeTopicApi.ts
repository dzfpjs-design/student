import { ApiError, request } from './api'

export type MistakeTopicSource = { mistake_id: string; source_revision: string; graph_version?: string }
export type MistakeTopicScope = {
  session_id?: string
  subject: string
  knowledge_point: string
  knowledge_node_key?: string
  sources: MistakeTopicSource[]
}
export type MistakeTopicLesson = {
  summary: string
  patterns: { label: string; explanation: string; mistake_ids: string[] }[]
  learning_steps: string[]
  socratic_question: string
}
type MistakeTopicReceipt = {
  version: 1
  child_id: string
  subject: string
  knowledge_point: string
  knowledge_node_key?: string
  sources: MistakeTopicSource[]
} & (
  | { status: 'missing'; explanation: null; created_at: null }
  | { status: 'ready'; explanation: MistakeTopicLesson; created_at: string }
)

function matchesSources(returned: MistakeTopicSource[], expected: MistakeTopicSource[]): boolean {
  if (!Array.isArray(returned) || returned.length !== expected.length) return false
  const sourceById = new Map(expected.map(source => [source.mistake_id, source]))
  return returned.every(source => {
    const expectedSource = source && sourceById.get(source.mistake_id)
    if (!expectedSource || expectedSource.source_revision !== source.source_revision || expectedSource.graph_version !== source.graph_version) return false
    sourceById.delete(source.mistake_id)
    return true
  })
}

const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0

function validLesson(lesson: MistakeTopicLesson, sources: MistakeTopicSource[]): boolean {
  const sourceIds = new Set(sources.map(source => source.mistake_id))
  if (!lesson || !nonempty(lesson.summary) || !nonempty(lesson.socratic_question)
    || !Array.isArray(lesson.learning_steps) || !lesson.learning_steps.length || !lesson.learning_steps.every(nonempty)
    || !Array.isArray(lesson.patterns) || !lesson.patterns.length) return false
  const cited = new Set<string>()
  const validPatterns = lesson.patterns.every(pattern => {
    if (!pattern || !nonempty(pattern.label) || !nonempty(pattern.explanation) || !Array.isArray(pattern.mistake_ids)) return false
    const ids = new Set(pattern.mistake_ids)
    if (ids.size !== pattern.mistake_ids.length || ids.size < 2) return false
    return [...ids].every(id => {
      if (!sourceIds.has(id)) return false
      cited.add(id)
      return true
    })
  })
  return validPatterns && cited.size === sourceIds.size
}

export async function readMistakeTopic(
  childId: string, scope: MistakeTopicScope, reuseOnly: boolean, signal: AbortSignal,
): Promise<MistakeTopicReceipt> {
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(childId)) throw new ApiError('当前错题本归属尚未核对。')
  const value = await request<MistakeTopicReceipt>(`/v1/children/${encodeURIComponent(childId)}/mistake-learning/topics/explain`, {
    method: 'POST', body: JSON.stringify({ ...scope, reuse_only: reuseOnly }), signal, cache: 'no-store',
  }, false, true)
  if (!value || value.version !== 1 || value.child_id !== childId || value.subject !== scope.subject
    || value.knowledge_point !== scope.knowledge_point || value.knowledge_node_key !== scope.knowledge_node_key
    || !matchesSources(value.sources, scope.sources)) {
    throw new ApiError('这份专题的题目范围已有变化，请刷新错题本。', undefined, 'MISTAKE_TOPIC_SOURCE_CHANGED')
  }
  if (value.status === 'ready' && nonempty(value.created_at) && validLesson(value.explanation, scope.sources)) return value
  if (reuseOnly && value.status === 'missing' && value.explanation === null && value.created_at === null) return value
  throw new ApiError('这份专题暂时无法核对，请再试一次。', undefined, 'MISTAKE_TOPIC_MODEL_OUTPUT_INVALID')
}
