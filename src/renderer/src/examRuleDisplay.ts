export interface ExamDurationVariant {
  candidate_count: number
  min_minutes: number
  max_minutes: number
  label: string
}

export interface ExamSectionTiming {
  duration_minutes: number
  duration_variants?: ExamDurationVariant[]
}

export function formatExamSectionDuration(section: ExamSectionTiming): string {
  if (section.duration_variants?.length) {
    return section.duration_variants
      .map(item => `${item.label} ${item.min_minutes}–${item.max_minutes} 分钟`)
      .join('；')
  }
  return `${section.duration_minutes} 分钟`
}

export function formatExamItemTarget(itemTarget?: number, unit?: string): string {
  if (itemTarget == null) return ''
  const labels: Record<string, string> = { questions: '题', parts: '部分' }
  return `${itemTarget} ${labels[unit ?? ''] ?? '项'}`
}
