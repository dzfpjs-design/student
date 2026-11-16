import type { PetBaseline } from './types'

export function PetBaselineProgress({ baseline, compact = false }: { baseline: PetBaseline; compact?: boolean }): React.JSX.Element {
  return (
    <section className={`pet-baseline${compact ? ' is-compact' : ''}`} aria-label="PET四项基线进度">
      <div className="pet-baseline-heading">
        <div><strong>PET 四项基线</strong><span>{baseline.completed_tasks}/{baseline.required_tasks}项已有证据 · 参考评估</span></div>
        <b>{baseline.status === 'completed' ? '基线已完成' : baseline.status === 'in_progress' ? '评估中' : '尚未开始'}</b>
      </div>
      <div className="pet-baseline-dimensions">
        {baseline.dimensions.map((dimension) => (
          <div key={dimension.key}>
            <span>{dimension.label}</span>
            <strong>{dimension.completed_tasks}/{dimension.required_tasks}</strong>
            <small>{dimension.reference_score === null ? '待取证' : `${dimension.reference_score}分参考`}</small>
          </div>
        ))}
      </div>
      {!compact && <small className="pet-baseline-boundary">{baseline.boundary}</small>}
    </section>
  )
}
