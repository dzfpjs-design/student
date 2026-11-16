export type SupplyObligation = {
  obligation_id: string
  title: string
  status: 'missing_source' | 'ready' | 'waiting_constraints' | 'scheduled' | 'active' | 'paused' | 'practice_completed' | 'source_changed' | 'needs_replan' | 'needs_plan' | 'cancelled'
  message: string
  plan_item_id: string | null
  task_status: string | null
  local_only: true
  cloud_transmission_allowed: false
  mastery_claim_allowed: false
}

const labels: Record<SupplyObligation['status'], string> = {
  missing_source: '待补材料', ready: '题目已备好', waiting_constraints: '暂缓安排',
  scheduled: '已安排', active: '进行中', paused: '进度已保留', practice_completed: '练习已完成',
  source_changed: '材料需重核', needs_replan: '安排需重核', needs_plan: '待重新安排', cancelled: '已取消续行',
}

export function SupplyObligationStatus({ obligations }: { obligations?: SupplyObligation[] }): React.JSX.Element | null {
  if (!obligations?.length) return null
  return (
    <section aria-label="原练习安排" className="supply-obligation-status">
      <h3>原练习安排</h3>
      <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
        {obligations.map((item) => (
          <li key={item.obligation_id} style={{ borderBottom: '1px solid #d8dce0', padding: '10px 0', overflowWrap: 'anywhere' }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', justifyContent: 'space-between' }}>
              <strong>{item.title}</strong><span>{labels[item.status]}</span>
            </div>
            <p style={{ margin: '6px 0 0' }}>{item.message}</p>
          </li>
        ))}
      </ul>
    </section>
  )
}
