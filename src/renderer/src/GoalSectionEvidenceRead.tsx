import { useEffect, useRef, useState } from 'react'
import { ClipboardCheck, RefreshCw } from 'lucide-react'
import { requestParentLocal } from './api'
import { GoalSectionEvidence } from './GoalSectionEvidence'
import type { GoalRoadmap } from './GoalRoadmapPanel.types'

export function GoalSectionEvidenceRead({ childId, roadmap }: { childId: string; roadmap: GoalRoadmap }): React.JSX.Element {
  const ruleHash = roadmap.exam_rules?.current_snapshot?.snapshot_hash
  const ruleBinding = roadmap.exam_rules?.binding_status
  const ruleStatus = roadmap.exam_rules?.public_rule_status
  const scope = JSON.stringify([childId, roadmap.program_id, roadmap.track, roadmap.source_version, ruleHash, ruleBinding, ruleStatus])
  const [retry, setRetry] = useState(0)
  const [result, setResult] = useState<{ scope: string; retry: number; roadmap: GoalRoadmap; evidence: unknown; changed?: boolean } | null>(null)
  const generation = useRef(0)
  useEffect(() => {
    const owner = ++generation.current
    const controller = new AbortController()
    const path = `/v1/parent/children/${encodeURIComponent(childId)}/goal-programs/${encodeURIComponent(roadmap.track)}/roadmap`
    void requestParentLocal<GoalRoadmap>(path, { cache: 'no-store', signal: controller.signal }).then((current) => {
      if (owner !== generation.current || controller.signal.aborted) return
      const bound = current?.version === 'goal_roadmap_v1' && current.child_id === childId
        && current.program_id === roadmap.program_id && current.track === roadmap.track
      const sameSource = typeof roadmap.source_version === 'string' && !!roadmap.source_version
        && current?.source_version === roadmap.source_version
      const sameRules = (ruleHash === undefined || current?.section_evidence?.rule_snapshot_hash === ruleHash)
        && (ruleBinding === undefined || current?.section_evidence?.rule_binding_status === ruleBinding)
        && (ruleStatus === undefined || current?.section_evidence?.public_rule_status === ruleStatus)
      setResult({ scope, retry, roadmap, evidence: bound && sameSource && sameRules ? current.section_evidence : null,
        changed: bound && (!sameSource || !sameRules) })
    }).catch(() => {
      if (owner === generation.current && !controller.signal.aborted) setResult({ scope, retry, roadmap, evidence: null })
    })
    return () => { generation.current += 1; controller.abort() }
  }, [childId, roadmap, ruleHash, ruleBinding, ruleStatus, scope, retry])
  const current = result?.scope === scope && result.retry === retry && result.roadmap === roadmap ? result : null
  return <div className="goal-section-read" aria-busy={!current}>
    {!current || current.changed ? <section aria-label="练习与评分证据">
      <h4><ClipboardCheck size={18} aria-hidden="true" />练习与评分证据</h4>
      <p role="status" className={current?.changed ? 'goal-roadmap-caution' : undefined}>
        {current?.changed ? '路线已更新，需重新读取完整路线。' : '正在读取当前练习证据'}
      </p>
    </section> : <GoalSectionEvidence childId={childId} evidence={current.evidence} />}
    <button type="button" aria-label="刷新练习证据" title="刷新练习证据" onClick={() => setRetry((value) => value + 1)}>
      <RefreshCw size={17} aria-hidden="true" />
    </button>
  </div>
}
