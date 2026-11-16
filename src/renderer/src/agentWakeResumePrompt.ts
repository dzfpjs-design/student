import type { AgentRuntimeStatus, SessionWorkspaceState } from './types'

export function agentContinuationRecoveryMessage(runtime: AgentRuntimeStatus | null, childId?: string): string | null {
  const receipt = runtime?.continuation
  if (!receipt || receipt.session_id !== runtime?.session_id || (childId !== undefined && receipt.child_id !== childId)
      || !receipt.next_turn_id || receipt.replay_allowed !== false || receipt.local_only !== true
      || receipt.cloud_transmission_allowed !== false) return null
  if (receipt.status === 'awaiting_receipt') return '刚才的后续操作结果还没有确认。我不会重复执行，可以先核对当前设备或学习任务的状态。'
  if (receipt.status === 'failed' || receipt.status === 'blocked') return '刚才的后续操作没有完成，原记录还在。我们先核对原因，不会自动重试。'
  return null
}

export function agentWakeResumePrompt(
  workspace: SessionWorkspaceState,
  runtime: AgentRuntimeStatus | null,
  childName: string,
): string {
  runtime = runtime?.session_id === workspace.session.session_id ? runtime : null
  const recovery = agentContinuationRecoveryMessage(runtime, workspace.session.child_id)
  if (recovery) return recovery
  const objective = runtime?.active_objective?.status === 'active' ? runtime.active_objective : null
  if (objective) {
    const waiting = runtime?.current_step?.plan_status === 'waiting_child'
    return waiting
      ? `我在，${childName}。我们继续“${objective.summary}”。刚才在等你回答，你接着说就好。`
      : `我在，${childName}。我们继续“${objective.summary}”。我会从保存的位置接着来。`
  }

  const active = workspace.plan_items.find((item) => item.status === 'active')
  const paused = workspace.plan_items.find((item) => item.status === 'paused')
  const pending = workspace.plan_items.find((item) => item.status === 'pending')
  if (workspace.session.state === 'study' && active) {
    return `我在，${childName}。现在正在做${active.subject}${active.title}。遇到问题直接说，做完了也告诉我。`
  }
  if (workspace.session.state === 'study' && paused) {
    return `我在，${childName}。${paused.subject}${paused.title}还暂停着。你可以说“继续这项”，也可以告诉我先处理什么。`
  }
  if (workspace.session.state === 'study' && pending) {
    return `我在，${childName}。下一项是${pending.subject}${pending.title}。准备好就说“开始任务”。`
  }

  const prompts: Record<string, string> = {
    check_in: '我们先用半分钟看一下今天的精力和安排。告诉我今天感觉怎么样，晚上有没有固定事情。',
    classroom_recall: '我们继续回想今天课堂。按学科说老师讲了什么，想不完整也没关系，我会逐项核对。',
    homework_capture: '我们继续录今天的作业。按学科说内容和大概需要多久，全部说完后告诉我。',
    plan_review: '今天的计划已经保存。你可以问我为什么这样排，也可以说“按这个开始”。',
    break: '休息还在继续。准备好了就说“继续学习”。',
    reflection: '只剩今天的收尾。告诉我学会了什么、哪里还不确定、明天先做什么。',
    completed: '今天的学习已经保存完成。现在可以休息；有新问题也可以继续问我。',
  }
  return `我在，${childName}。${prompts[workspace.session.state] ?? '你接着说，我会根据今天保存的进度继续。'}`
}
