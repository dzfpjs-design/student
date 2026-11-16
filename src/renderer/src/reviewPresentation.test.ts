import { describe, expect, it } from 'vitest'
import { planItemTitle, reviewLabel } from './reviewPresentation'

describe('school review presentation', () => {
  const item = { title: '数量关系 · school_open_near复测', source_type: 'due_review' as const, source_ref: 'school-review:a' }
  it.each([
    ['school_open_near', '换一道类似题'], ['school_open_far', '换个情境试一试'], ['school_open_7d', '一周后再检查'],
    ['next_day', '次日复测'], ['7d', '7天复测'], ['unknown', '到期复测'], ['constructor', '到期复测'],
  ])('names %s without inferring mastery', (interval, label) => expect(reviewLabel(interval)).toBe(label))
  it('does not rewrite homework, unrelated reviews, or user-authored content', () => {
    expect(planItemTitle({ ...item, source_type: 'school_homework' })).toBe(item.title)
    expect(planItemTitle({ ...item, source_ref: 'another-review' })).toBe(item.title)
    expect(planItemTitle({ ...item, source_ref: null })).toBe(item.title)
    expect(planItemTitle({ ...item, title: '解释school_open_near的含义' })).toBe('解释school_open_near的含义')
    expect(planItemTitle({ ...item, title: '原稿 · school_open_unknown复测' })).toBe('原稿 · school_open_unknown复测')
  })
  it('keeps the original question name and does not mutate persisted state', () => {
    expect(planItemTitle(Object.freeze(item))).toBe('数量关系 · 换一道类似题')
    expect(item.title).toBe('数量关系 · school_open_near复测')
  })
})
