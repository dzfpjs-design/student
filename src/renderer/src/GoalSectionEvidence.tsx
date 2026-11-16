import { ClipboardCheck } from 'lucide-react'
import type { GoalSection, GoalSectionEvidence as Evidence } from './GoalSectionEvidence.types'

const sectionLabels: Record<string, string> = {
  reading: '阅读', writing: '写作', listening: '听力', speaking: '口语',
  语文: '语文', 数学: '数学', 英语: '英语', 物理: '物理', 化学: '化学',
  历史: '历史', 政治: '政治', 道德与法治: '道德与法治', 生物: '生物', 地理: '地理', 体育: '体育',
}
const sourceLabels = {
  parent_reviewed_source: '家长审核题源',
  deterministically_validated_source: '规则校验题源',
  shipped_reference_source: '内置参考题源',
}

function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}
function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}
function date(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const time = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(time.getTime()) && time.toISOString().slice(0, 10) === value
}
function section(value: unknown): value is GoalSection {
  if (!object(value) || typeof value.section_id !== 'string' || !value.section_id
    || value.official_part_mapping !== 'missing' || value.coverage_status !== 'missing_reviewed_part_mapping'
    || value.mastery_claim_allowed !== false || !['practice_scope_available', 'missing'].includes(String(value.scope_status))
    || !Array.isArray(value.sources) || !Array.isArray(value.opportunities) || !Array.isArray(value.qualified_evidence)) return false
  const fields = ['available_source_count', 'reviewed_source_count', 'opportunity_count', 'qualified_evidence_count',
    'independent_correct_count', 'distinct_qualified_questions', 'distinct_independent_correct_questions']
  if (!fields.every((key) => count(value[key]))) return false
  if (!value.sources.every((source) => object(source) && typeof source.source_qualification === 'string'
    && Object.hasOwn(sourceLabels, source.source_qualification))) return false
  const s = value as unknown as GoalSection
  return s.available_source_count === s.sources.length && s.opportunity_count === s.opportunities.length
    && s.qualified_evidence_count === s.qualified_evidence.length
    && s.reviewed_source_count === s.sources.filter((source) => source.source_qualification === 'parent_reviewed_source').length
    && s.qualified_evidence_count <= s.opportunity_count && s.independent_correct_count <= s.qualified_evidence_count
    && s.distinct_qualified_questions <= s.qualified_evidence_count
    && s.distinct_independent_correct_questions <= s.independent_correct_count
    && s.distinct_independent_correct_questions <= s.distinct_qualified_questions
    && (s.scope_status === 'missing') === (s.available_source_count === 0)
}
function valid(value: unknown, childId: string): value is Evidence {
  if (!object(value) || value.version !== 'goal_section_evidence_v1' || value.child_id !== childId
    || typeof value.context_version !== 'string' || !value.context_version || !date(value.as_of_date)
    || value.time_basis !== 'current_source_and_receipt_at_read_not_end_of_day'
    || !['scope_inventory_not_official_coverage', 'rule_applicability_unverified'].includes(String(value.status))
    || value.exam_readiness_claim_allowed !== false || value.local_only !== true || value.cloud_transmission_allowed !== false
    || !count(value.unmapped_or_unavailable_opportunity_count)
    || !Array.isArray(value.unavailable_item_ids) || !Array.isArray(value.unmapped_item_ids)
    || !Array.isArray(value.sections) || !value.sections.every(section)) return false
  return new Set(value.sections.map((s) => s.section_id)).size === value.sections.length
    && (value.status === 'rule_applicability_unverified') === (value.sections.length === 0)
}

export function GoalSectionEvidence({ childId, evidence }: { childId: string; evidence: unknown }): React.JSX.Element {
  if (!valid(evidence, childId)) return <section aria-label="练习与评分证据">
    <h4><ClipboardCheck size={18} aria-hidden="true" />练习与评分证据</h4>
    <p className="goal-roadmap-caution">练习证据尚未核实，请重新读取路线。</p>
  </section>
  return <section aria-label="练习与评分证据" className="goal-section-evidence">
    <style>{`
      .goal-section-evidence .goal-section-list { list-style: none; padding: 0; margin: 0; }
      .goal-section-evidence .goal-section-list > li { padding: 14px 0; border-bottom: 1px solid #dce2df; }
      .goal-section-evidence h5 { margin: 0; font-size: 15px; }
      .goal-section-evidence .goal-section-counts { grid-template-columns: repeat(auto-fit, minmax(min(100%, 130px), 1fr)); }
      .goal-section-evidence .goal-section-counts dd { font-weight: 600; }
      .goal-section-evidence .goal-section-counts small { display: block; font-weight: 400; color: #526362; }
      .goal-section-evidence .goal-section-sources { display: flex; flex-wrap: wrap; gap: 4px 16px; list-style: none; padding: 0; }
    `}</style>
    <h4><ClipboardCheck size={18} aria-hidden="true" />练习与评分证据</h4>
    <p className="goal-roadmap-caption">截至 <time dateTime={evidence.as_of_date}>{evidence.as_of_date}</time> 的当前有效来源与评分；不是该日结束时的历史快照。</p>
    <p>这里反映技能或学科练习，尚不能确认官方题型覆盖，也不能据此判断已具备应考能力。</p>
    {!evidence.sections.length ? <p className="goal-roadmap-caution">考试规则适用范围尚未核实，暂不能按分项列出练习证据。</p>
      : <ul className="goal-section-list">{evidence.sections.map((s) => <li key={s.section_id}>
        <h5>{sectionLabels[s.section_id] ?? '待核对的学科或技能'}</h5>
        <p className="goal-roadmap-caution">官方题型覆盖尚未核验</p>
        <dl className="goal-section-counts">
          <div><dt>可用练习题源</dt><dd>{s.available_source_count} 道</dd></div>
          <div><dt>实际作答</dt><dd>{s.opportunity_count} 次</dd></div>
          <div><dt>可靠评分</dt><dd>{s.qualified_evidence_count} 次<small>涉及 {s.distinct_qualified_questions} 道不同题目</small></dd></div>
          <div><dt>独立正确</dt><dd>{s.independent_correct_count} 次<small>涉及 {s.distinct_independent_correct_questions} 道不同题目</small></dd></div>
        </dl>
        {!s.available_source_count ? <p>暂无当前可用题源</p> : <ul className="goal-section-sources">
          {Object.entries(sourceLabels).map(([kind, label]) => {
            const amount = s.sources.filter((source) => source.source_qualification === kind).length
            return amount ? <li key={kind}>{label} {amount} 道</li> : null
          })}
        </ul>}
      </li>)}</ul>}
    <p className="goal-roadmap-caption">题源可用不等于现在可以启动任务；实际作答包括尚无可靠评分的回答。可靠评分可能是低分或在帮助下完成，独立正确也不等于长期掌握。</p>
    {evidence.unmapped_or_unavailable_opportunity_count > 0 && <p className="goal-roadmap-caption">另有 {evidence.unmapped_or_unavailable_opportunity_count} 次历史作答因来源失效或无法归入当前范围，未计入以上分项；原作答记录仍保留。</p>}
    {(evidence.unavailable_item_ids.length > 0 || evidence.unmapped_item_ids.length > 0) && <p className="goal-roadmap-caption">
      当前有 {evidence.unavailable_item_ids.length} 道题源不可用、{evidence.unmapped_item_ids.length} 道未归入上述技能或学科，不能视作已覆盖。
    </p>}
  </section>
}
