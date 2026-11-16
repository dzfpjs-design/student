import { CalendarDays, Clock3, ListTree, RefreshCw, ShieldQuestion } from 'lucide-react'
import type { GoalBaselineStatus, GoalRoadmap, GoalRoadmapActionKind, GoalRoadmapMilestone, GoalRoadmapPanelProps } from './GoalRoadmapPanel.types'
import { GoalExamRules } from './GoalExamRules'
import { GoalSectionEvidence } from './GoalSectionEvidence'
import { GoalSectionEvidenceRead } from './GoalSectionEvidenceRead'

const baselineLabels: Record<GoalBaselineStatus, string> = {
  needs_baseline: '基线未知', stale_recheck: '证据待更新', recheck_needed: '需要重新核对',
  supported_only: '目前仅有辅助作答', independent_observed: '已有独立作答证据', retention_observed: '已有延迟复测证据',
}
const actionLabels: Record<GoalRoadmapActionKind, string> = {
  establish_baseline: '建立基线', refresh_baseline: '重新核对基线', teach_then_check: '讲解后核对',
  independent_check: '独立作答核对', delayed_check: '间隔后复测', transfer_check: '迁移核对',
  clarify_then_recheck: '澄清后复测', fade_hint_then_check: '减少提示后核对', different_item_check: '换题核对',
}
const statusLabels: Record<GoalRoadmap['status'], string> = {
  paused: '路线已暂停', needs_clarification: '条件待确认', blocked: '路线待核实',
  needs_negotiation: '需要协商', estimated: '估算路线',
}
const feasibilityLabels: Record<GoalRoadmap['burden']['feasibility'], string> = {
  unknown: '可达性尚不能判断', deadline_passed: '目标日期已过，需要重新协商',
  budget_unavailable: '当前没有可用预算', horizon_unverified: '远期安排尚待核实',
  estimated_over_capacity: '估算超出预算，需要协商', estimated_at_risk: '估算存在超出预算的风险',
  provisional_not_guaranteed: '估算范围可容纳，不保证达标',
}

function validDate(value: string | null): string | null {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const parsed = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : null
}

function WorkWindow({ milestone, label, workLabels }: {
  milestone: GoalRoadmapMilestone; label: string; workLabels: Map<string, string>
}): React.JSX.Element {
  return <li className="goal-roadmap-window">
    <div><strong>{label}</strong><span>约 {milestone.estimated_minutes} 分钟</span></div>
    <ul>{milestone.work_ids.map((id) => <li key={id}>{workLabels.get(id) ?? '待核对的能力步骤'}</li>)}</ul>
  </li>
}

export function GoalRoadmapPanel({ childId, roadmap, loading = false, error, onRetry }: GoalRoadmapPanelProps): React.JSX.Element {
  const retry = onRetry && <button type="button" title="重新读取路线" aria-label="重新读取路线" disabled={loading} onClick={onRetry}>
    <RefreshCw size={17} aria-hidden="true" />
  </button>
  // Never retain another child's snapshot or present a failed refresh as a current action.
  if (loading || error || !roadmap || roadmap.child_id !== childId || roadmap.version !== 'goal_roadmap_v1') {
    return <section className="goal-roadmap-empty" aria-label="长期能力路线" aria-busy={loading}>
      <p role={error ? 'alert' : 'status'}>{loading ? '正在核对能力路线' : error ? '路线暂未核实，请重新读取。'
        : roadmap ? '路线与当前孩子或版本不匹配，需要重新核对。' : '尚无可核对的能力路线'}</p>
      {retry}
    </section>
  }
  const deadline = validDate(roadmap.target_date)
  const burden = roadmap.burden
  const names = new Map(roadmap.prerequisite_path.map((node) => [node.knowledge_node_id, node.title]))
  const workLabels = new Map(roadmap.prerequisite_path.flatMap((node) => node.steps.map((step) =>
    [step.work_id, `${node.title} · ${actionLabels[step.action] ?? '步骤待核对'}`] as const)))
  const action = roadmap.next_daily_action
  const actionNode = roadmap.prerequisite_path.find((node) => node.knowledge_node_id === action?.knowledge_node_id)
  const actionBound = action && action.child_id === childId && action.program_id === roadmap.program_id
    && action.roadmap_source_version === roadmap.source_version && actionNode && !actionNode.blocked_by.length

  return <section className="goal-roadmap-panel" aria-label="长期能力路线">
    <style>{`
      .goal-roadmap-panel { min-width: 0; color: #253331; font-size: 14px; line-height: 1.6; letter-spacing: 0; overflow-wrap: anywhere; }
      .goal-roadmap-panel * { box-sizing: border-box; min-width: 0; }
      .goal-roadmap-panel h3 { font-size: 17px; margin: 0; }
      .goal-roadmap-panel h4 { display: flex; align-items: center; gap: 8px; font-size: 15px; margin: 0 0 10px; }
      .goal-roadmap-panel svg { flex-shrink: 0; }
      .goal-roadmap-panel p { margin: 6px 0; }
      .goal-roadmap-panel header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
      .goal-roadmap-panel header > div { flex: 1; }
      .goal-roadmap-panel button { display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px; flex-shrink: 0; border-radius: 4px; }
      .goal-roadmap-panel section { margin-top: 18px; padding-top: 16px; border-top: 1px solid #dce2df; }
      .goal-roadmap-panel .goal-roadmap-caption { color: #526362; font-size: 13px; }
      .goal-roadmap-panel .goal-roadmap-caution { color: #884347; }
      .goal-roadmap-panel dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 14px; margin: 10px 0; }
      .goal-roadmap-panel dt { color: #526362; font-size: 13px; }
      .goal-roadmap-panel dd { margin: 2px 0 0; }
      .goal-roadmap-panel ol, .goal-roadmap-panel ul { padding-left: 22px; margin: 6px 0; }
      .goal-roadmap-panel .goal-roadmap-path > li { padding: 10px 0; border-bottom: 1px solid #e8ecea; }
      .goal-roadmap-panel .goal-roadmap-node-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 16px; }
      .goal-roadmap-panel .goal-roadmap-node-head span { color: #32675c; }
      .goal-roadmap-panel details { padding: 8px 0; }
      .goal-roadmap-panel summary { cursor: pointer; font-weight: 600; color: #275f80; }
      .goal-roadmap-panel summary:focus-visible { outline: 2px solid #275f80; outline-offset: 3px; }
      .goal-roadmap-panel .goal-roadmap-windows { list-style: none; padding-left: 0; }
      .goal-roadmap-panel .goal-roadmap-window { padding: 10px 0; border-bottom: 1px solid #e8ecea; }
      .goal-roadmap-panel .goal-roadmap-window > div { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 16px; }
    `}</style>
    <header>
      <div><h3>{roadmap.title}</h3><p className="goal-roadmap-caption">{statusLabels[roadmap.status] ?? '路线待核实'} · 证据核对截至 {validDate(roadmap.as_of_date) ?? '日期待核实'}</p></div>
      {retry}
    </header>
    <dl>
      <div><dt>目标日期</dt><dd>{deadline ? <time dateTime={deadline}>{deadline}</time> : '目标日期待确认'}</dd></div>
      <div><dt>当前判断</dt><dd>{feasibilityLabels[burden.feasibility] ?? '可达性尚不能判断'}</dd></div>
    </dl>
    <p className="goal-roadmap-caption">目标达成尚未证实。参与时长不代表能力，讲解或一次作答也不代表长期掌握。</p>
    <GoalExamRules rules={roadmap.exam_rules} />
    {roadmap.section_evidence === undefined
      ? <GoalSectionEvidenceRead key={JSON.stringify([childId, roadmap.program_id, roadmap.track, roadmap.source_version,
        roadmap.exam_rules?.current_snapshot?.snapshot_hash, roadmap.exam_rules?.binding_status, roadmap.exam_rules?.public_rule_status])} childId={childId} roadmap={roadmap} />
      : <GoalSectionEvidence childId={childId} evidence={roadmap.section_evidence} />}

    <section aria-label="本周下一步">
      <h4><CalendarDays size={18} aria-hidden="true" />本周下一步</h4>
      {roadmap.status === 'paused' ? <p>路线已暂停，不自动安排任务。</p>
        : !action ? <p>当前没有可自动安排的动作</p>
          : !actionBound ? <p className="goal-roadmap-caution">动作绑定已变化，需要重新核对</p>
            : <>
              <p><strong>{action.title}</strong> · {actionLabels[action.action] ?? '动作待核实'} · 约 {action.minutes} 分钟</p>
              <p className={action.dispatchable ? 'goal-roadmap-caption' : 'goal-roadmap-caution'}>
                {action.dispatchable && action.status === 'suggested' ? '可供规划选择，尚未分配题目' : '缺合格题源，暂不安排诊断'}</p>
              <p className="goal-roadmap-caption">{action.completion_standard}</p>
            </>}
    </section>

    <section aria-label="时间预算与估算负担">
      <h4><Clock3 size={18} aria-hidden="true" />时间预算与估算负担</h4>
      {!burden.configured_budget_valid ? <p className="goal-roadmap-caution">预算配置待核实</p> : <>
        <p>每周上限 {burden.weekly_minutes} 分钟 · {burden.weekly_sessions} 次</p>
        <p>已占用 {burden.committed_weekly_minutes} 分钟 · 本周剩余 {burden.remaining_weekly_minutes} 分钟</p>
      </>}
      <p className="goal-roadmap-caption">占用包含已安排任务，不等于已经学习；预算也不等于作业、睡眠和休息之外的实际空闲时间。</p>
      <p>已知路径工作量约 {burden.estimated_minutes.min} 至 {burden.estimated_minutes.max} 分钟，只是规划估算，不是孩子的实测速度。</p>
      {!burden.estimate_complete && <p className="goal-roadmap-caution">目录或前置来源未核齐，以上不是完整工作量。</p>}
      {burden.unallocated_minutes > 0 && <p className="goal-roadmap-caution">{burden.unallocated_minutes} 分钟估算工作未排入当前窗口。</p>}
      <p className="goal-roadmap-caption">时间不足时先核实条件、协商日期或目标范围，不自动增加学习时间。</p>
    </section>

    <section aria-label="前置能力与基线">
      <h4><ListTree size={18} aria-hidden="true" />前置能力与基线</h4>
      {!roadmap.prerequisite_path.length ? <p>当前缺少可核对的能力路径。</p> : <ol className="goal-roadmap-path">
        {roadmap.prerequisite_path.map((node) => <li key={node.knowledge_node_id}>
          <div className="goal-roadmap-node-head"><strong>{node.title}</strong><span>{baselineLabels[node.baseline_status] ?? '基线待核实'}</span></div>
          <p className="goal-roadmap-caption">{node.subject}{node.latest_evidence_at && validDate(node.latest_evidence_at.slice(0, 10))
            ? ` · 最近有效记录 ${node.latest_evidence_at.slice(0, 10)}` : ' · 尚无当前有效作答记录'}</p>
          {node.blocked_by.length > 0 && <p className="goal-roadmap-caution">先核对：{node.blocked_by.map((id) => names.get(id) ?? '来源待核实的前置能力').join('、')}</p>}
          {!!node.steps.length && <details><summary>后续核对步骤</summary><ul>{node.steps.map((step) => <li key={step.work_id}>
            {actionLabels[step.action] ?? '步骤待核实'} · 估算 {step.estimated_minutes.min} 至 {step.estimated_minutes.max} 分钟
            {step.minimum_gap_hours > 0 && ` · 至少间隔 ${step.minimum_gap_hours} 小时，执行时再核对`}
          </li>)}</ul></details>}
        </li>)}
      </ol>}
    </section>

    <section aria-label="月周里程碑估算">
      <h4><CalendarDays size={18} aria-hidden="true" />月周里程碑估算</h4>
      <p className="goal-roadmap-caption">倒排窗口不是已分配任务，也不是达标承诺；不必等到窗口末尾才核对基线。</p>
      {!deadline ? <p>日期未确认，暂不列出月周里程碑。</p> : <>
        <details open><summary>每月估算</summary><div role="region" aria-label="每月里程碑">
          {roadmap.milestones.monthly.length ? <ul className="goal-roadmap-windows">{roadmap.milestones.monthly.map((milestone) =>
            <WorkWindow key={milestone.month} milestone={milestone} label={`${milestone.month} · 核对节点 ${validDate(milestone.target_date) ?? '日期待核实'}`} workLabels={workLabels} />)}</ul>
            : <p>当前日期和预算下暂无可列出的月里程碑。</p>}
        </div></details>
        <details><summary>每周估算</summary><div role="region" aria-label="每周里程碑">
          {roadmap.milestones.weekly.length ? <ul className="goal-roadmap-windows">{roadmap.milestones.weekly.map((milestone) =>
            <WorkWindow key={milestone.period_start} milestone={milestone} label={`${validDate(milestone.period_start) ?? '日期待核实'} 至 ${validDate(milestone.target_date) ?? '日期待核实'}`} workLabels={workLabels} />)}</ul>
            : <p>当前日期和预算下暂无可列出的周里程碑。</p>}
        </div></details>
      </>}
    </section>

    {(roadmap.risks.length > 0 || roadmap.clarifications.length > 0) && <section aria-label="待核实与协商">
      <h4><ShieldQuestion size={18} aria-hidden="true" />待核实与协商</h4>
      <ul>{roadmap.clarifications.map((item) => <li key={item.field}>{item.reason}</li>)}</ul>
      <ul>{roadmap.risks.map((item) => <li key={item.code}>{item.message}</li>)}</ul>
    </section>}
  </section>
}
