import type { GoalRoadmap } from './GoalRoadmapPanel.types'
import { formatExamSectionDuration } from './examRuleDisplay'

function officialLink(value: string): string | null {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password && !url.port
      && ['www.cambridgeenglish.org', 'edu.nanjing.gov.cn'].includes(url.hostname) ? url.href : null
  } catch { return null }
}

export function GoalExamRules({ rules }: { rules: GoalRoadmap['exam_rules'] }): React.JSX.Element | null {
  if (!rules || rules.version !== 'official_goal_rules_v1') return null
  const reference = rules.current_snapshot?.reference
  const link = reference && officialLink(reference.url)
  const sectionNames: Record<string, string> = { reading: '阅读', writing: '写作', listening: '听力', speaking: '口语' }
  const status = rules.public_rule_status === 'verified_reference_snapshot' ? '公开规则参考已核实'
    : rules.public_rule_status === 'target_year_unverified' && rules.target_year && rules.reference_year
      ? `${rules.target_year} 年规则尚未核实；已有参考仅适用 ${rules.reference_year} 年。`
      : rules.public_rule_status === 'event_rules_unverified' ? '具体赛事规则尚未核实。'
        : rules.public_rule_status === 'not_verified_at_reference_date' ? '当时尚无已核实的规则参考。' : '目标规则尚待核实。'
  return <section aria-label="考试规则依据">
    <h4>考试规则依据</h4>
    <p>{status}</p>
    {link && reference && <p><a href={link} target="_blank" rel="noreferrer">{reference.authority} · {reference.verified_on} 核对</a></p>}
    {rules.binding_status === 'source_changed' && <p className="goal-roadmap-caution">原绑定规则已变化，需要重新核对；日常学习可以继续。</p>}
    <p>本人报名场次尚未核实。家庭目标日期不等于考试日期。</p>
    {rules.required_sections.length > 0 && <details><summary>参考考查部分</summary>
      <ul>{rules.required_sections.map((item) => <li key={`${item.blueprint_id}:${item.section_id}`}>
        {sectionNames[item.section_id] ?? item.title} · {formatExamSectionDuration(item)}
      </li>)}</ul>
    </details>}
    <p className="goal-roadmap-caption">正式要求与当前题源的完整覆盖尚未核对；不据此判断已具备考试通过或获奖能力。</p>
  </section>
}
