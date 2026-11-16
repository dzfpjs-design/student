import type { PlanItem } from './types'

const intervalLabels: Record<string, string> = {
  immediate: '即时变式', next_day: '次日复测', '48h': '48小时复测', '7d': '7天复测',
  school_open_near: '换一道类似题', school_open_far: '换个情境试一试', school_open_7d: '一周后再检查',
}

export function reviewLabel(interval: string): string {
  return Object.hasOwn(intervalLabels, interval) ? intervalLabels[interval] : '到期复测'
}

export function planItemTitle(item: Pick<PlanItem, 'title' | 'source_type' | 'source_ref'>): string {
  if (item.source_type !== 'due_review' || !item.source_ref?.startsWith('school-review:')) return item.title
  // Translate only the generated suffix; the stored title and binding stay unchanged.
  return item.title.replace(/ · (school_open_near|school_open_far|school_open_7d)复测$/, (_, interval: string) => ` · ${reviewLabel(interval)}`)
}
