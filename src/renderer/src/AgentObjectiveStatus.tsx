import type { AgentRuntimeStatus } from './types'
import { agentContinuationRecoveryMessage } from './agentWakeResumePrompt'

const learningTitles: Readonly<Record<string, string>> = {
  check: '正在核对的学习目标',
  remediate: '还需巩固的目标',
  transfer_check: '换个情境再检查',
  retention_due: '留待之后复查的目标',
  retention_check: '检查是否还记得',
}

export function AgentObjectiveStatus({ runtime }: { runtime: AgentRuntimeStatus }) {
  const recovery = agentContinuationRecoveryMessage(runtime)
  if (recovery) return <div className="agent-objective" aria-live="polite"><strong>待核对的操作</strong><span>{recovery}</span></div>
  const objective = runtime.active_objective
  if (!objective || objective.status !== 'active') return null
  const learning = objective.objective_type === 'learning'
  const title = learning ? learningTitles[objective.phase ?? ''] ?? '学习目标' : '现在一起完成'
  return <div className="agent-objective" aria-live="polite">
    <strong>{title}</strong>
    <span>{objective.summary}</span>
    {objective.success_criteria.length > 0 && <small>
      {learning ? '目标标准' : '完成标准'}：{objective.success_criteria.join('；')}
    </small>}
    {runtime.current_step?.plan_status === 'waiting_child' && <small className="agent-objective-waiting">等你回答后继续</small>}
  </div>
}
