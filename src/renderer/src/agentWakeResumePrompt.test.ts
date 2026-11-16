import { describe, expect, it } from 'vitest'
import { agentWakeResumePrompt } from './agentWakeResumePrompt'
import type { AgentRuntimeStatus, SessionWorkspaceState } from './types'

const workspace = (state: string, planItems: SessionWorkspaceState['plan_items'] = []): SessionWorkspaceState => ({
  session: { session_id: 'session-1', child_id: 'child-1', state, status: 'active' },
  plan_items: planItems,
} as SessionWorkspaceState)

describe('agent wake resume prompt', () => {
  it('resumes a persisted learning objective without inventing a new lesson', () => {
    const runtime = {
      session_id: 'session-1',
      active_objective: {
        objective_id: 'objective-1',
        objective_type: 'learning',
        summary: '理解分数通分',
        success_criteria: [],
        status: 'active',
      },
      current_step: {
        step_id: 'step-1',
        capability_name: 'learning.request_response',
        status: 'waiting_child',
        plan_status: 'waiting_child',
        expected_evidence: [],
      },
    } satisfies AgentRuntimeStatus

    expect(agentWakeResumePrompt(workspace('study'), runtime, '小明')).toContain('刚才在等你回答')
    expect(agentWakeResumePrompt(workspace('study'), runtime, '小明')).toContain('理解分数通分')
  })

  it('uses the durable active task when no learning objective is open', () => {
    const prompt = agentWakeResumePrompt(workspace('study', [{
      plan_item_id: 'item-1', subject: '数学', title: '练习册第20页', status: 'active',
    } as SessionWorkspaceState['plan_items'][number]]), null, '小明')

    expect(prompt).toContain('数学练习册第20页')
    expect(prompt).toContain('做完了也告诉我')
  })

  it('guides the current daily stage when there is no active task', () => {
    expect(agentWakeResumePrompt(workspace('classroom_recall'), null, '小明')).toContain('今天课堂')
    expect(agentWakeResumePrompt(workspace('homework_capture'), null, '小明')).toContain('录今天的作业')
    expect(agentWakeResumePrompt(workspace('reflection'), null, '小明')).toContain('今天的收尾')
  })
})
